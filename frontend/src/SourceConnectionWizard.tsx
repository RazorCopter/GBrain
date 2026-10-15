import { FormEvent, useEffect, useState } from 'react'
import {
    ApiError,
    ImportJob,
    SourceCatalogItem,
    SourcePreviewResponse,
    SourceStatusResponse,
    SourceSyncPayload,
    SourceTestConnectionResponse,
    api,
    isConnectorAvailable,
} from './api'

export interface SourceConnectionWizardProps {
    catalogItems: SourceCatalogItem[]
    initialSourceId?: string
    initialStep?: WizardStep
    onClose: () => void
    onSuccess: () => void
}

type WizardStep = 1 | 2 | 3 | 4 | 5 | 6

const STEP_LABELS: Record<WizardStep, string> = {
    1: 'Selezione Fonte',
    2: 'Credenziali Client',
    3: 'Autorizzazione Account',
    4: 'Verifica Connessione',
    5: 'Anteprima Dati',
    6: 'Conferma & Sincronizzazione',
}

function formatDateForInput(date: Date): string {
    const pad = (num: number) => String(num).padStart(2, '0')
    const yyyy = date.getFullYear()
    const mm = pad(date.getMonth() + 1)
    const dd = pad(date.getDate())
    const hh = pad(date.getHours())
    const min = pad(date.getMinutes())
    return `${yyyy}-${mm}-${dd}T${hh}:${min}`
}

function parseJsonConfig(content: string): { ok: boolean; data?: Record<string, unknown>; error?: string } {
    try {
        const parsed = JSON.parse(content)
        if (!parsed || typeof parsed !== 'object') {
            return { ok: false, error: 'Il file JSON deve contenere un oggetto valido.' }
        }
        const clientConfig = parsed.web || parsed.installed || parsed
        if (!clientConfig.client_id || !clientConfig.client_secret) {
            return {
                ok: false,
                error: 'Il file JSON non contiene le chiavi client_id e client_secret richieste da Google Cloud Console.',
            }
        }
        return { ok: true, data: parsed }
    } catch {
        return { ok: false, error: 'File JSON non valido o sintatticamente malformato.' }
    }
}

export function SourceConnectionWizard({
    catalogItems,
    initialSourceId = 'gmail',
    initialStep,
    onClose,
    onSuccess,
}: SourceConnectionWizardProps) {
    const [currentStep, setCurrentStep] = useState<WizardStep>(initialStep ?? 1)
    const [selectedSourceId, setSelectedSourceId] = useState<string>(initialSourceId)
    const [sourceStatus, setSourceStatus] = useState<SourceStatusResponse | null>(null)
    const [loadingStatus, setLoadingStatus] = useState(false)
    const [statusError, setStatusError] = useState('')

    // Gestione Alias Autobiografici (Gmail)
    const [confirmedAliases, setConfirmedAliases] = useState<string[]>([])
    const [aliasesReady, setAliasesReady] = useState(false)
    const [proposedAliases, setProposedAliases] = useState<string[]>([])
    const [selectedAliases, setSelectedAliases] = useState<string[]>([])
    const [newAliasInput, setNewAliasInput] = useState('')
    const [confirmingAliases, setConfirmingAliases] = useState(false)
    const [aliasError, setAliasError] = useState('')
    const [aliasSuccessMessage, setAliasSuccessMessage] = useState('')
    const [isEditingAliases, setIsEditingAliases] = useState(false)

    // Step 2: Credenziali (Gmail e Immich)
    const [credMode, setCredMode] = useState<'upload' | 'manual'>('upload')
    const [uploadedJsonContent, setUploadedJsonContent] = useState<string | null>(null)
    const [uploadedFileName, setUploadedFileName] = useState<string>('')
    const [clientId, setClientId] = useState('')
    const [clientSecret, setClientSecret] = useState('')
    const [immichBaseUrl, setImmichBaseUrl] = useState('http://127.0.0.1:2283')
    const [immichApiKey, setImmichApiKey] = useState('')
    const [immichSelectionMode, setImmichSelectionMode] = useState<'period' | 'album'>('period')
    const [immichAlbumId, setImmichAlbumId] = useState('')
    const [showSecret, setShowSecret] = useState(false)
    const [redirectUriOverride, setRedirectUriOverride] = useState('')
    const [copySuccess, setCopySuccess] = useState(false)
    const [copiedKey, setCopiedKey] = useState<string | null>(null)
    const [credSaving, setCredSaving] = useState(false)
    const [credError, setCredError] = useState('')
    const [credSuccessMessage, setCredSuccessMessage] = useState('')

    // Step 3: OAuth
    const [oauthStarting, setOauthStarting] = useState(false)
    const [oauthError, setOauthError] = useState('')

    // Step 4: Test Connection
    const [testingConnection, setTestingConnection] = useState(false)
    const [testResult, setTestResult] = useState<SourceTestConnectionResponse | null>(null)
    const [testError, setTestError] = useState('')

    // Step 5: Preview
    const [previewLoading, setPreviewLoading] = useState(false)
    const [previewResult, setPreviewResult] = useState<SourcePreviewResponse | null>(null)
    const [previewError, setPreviewError] = useState('')
    const [periodAfter, setPeriodAfter] = useState(() => {
        const d = new Date()
        d.setDate(d.getDate() - 30)
        return formatDateForInput(d)
    })
    const [periodBefore, setPeriodBefore] = useState(() => formatDateForInput(new Date()))
    const [itemLimit, setItemLimit] = useState(500)
    const [requestBudget, setRequestBudget] = useState(200)

    // Step 6: Sync
    const [syncStarting, setSyncStarting] = useState(false)
    const [activeJob, setActiveJob] = useState<ImportJob | null>(null)
    const [syncCancelling, setSyncCancelling] = useState(false)
    const [syncError, setSyncError] = useState('')

    // Carica stato della sorgente selezionata
    async function refreshStatus(sourceId: string) {
        setLoadingStatus(true)
        setStatusError('')
        try {
            const res = await api.fetchSourceStatus(sourceId)
            setSourceStatus(res)
            if (sourceId === 'gmail') {
                const conf = res.confirmed_aliases || []
                const isReady = Boolean(res.aliases_ready && conf.length > 0)
                setConfirmedAliases(conf)
                setAliasesReady(isReady)

                setProposedAliases((prev) => {
                    const set = new Set(prev)
                    if (res.primary_email) set.add(res.primary_email)
                    conf.forEach((a) => set.add(a))
                    return Array.from(set)
                })

                if (conf.length > 0) {
                    setSelectedAliases((prev) => (prev.length > 0 ? prev : conf))
                } else if (res.primary_email) {
                    const primary = res.primary_email
                    setSelectedAliases((prev) => (prev.length > 0 ? prev : [primary]))
                }
            }
            return res
        } catch (err) {
            const msg = err instanceof ApiError ? err.message : 'Errore nel caricamento dello stato della fonte.'
            setStatusError(msg)
            return null
        } finally {
            setLoadingStatus(false)
        }
    }

    // Conferma esplicita degli alias del proprietario tramite PUT /api/gmail/aliases
    async function handleConfirmAliases(aliasesToConfirm?: string[]): Promise<boolean> {
        const listToConfirm = aliasesToConfirm ?? selectedAliases
        if (listToConfirm.length === 0) {
            setAliasError('Seleziona o specifica almeno un indirizzo email da confermare.')
            return false
        }
        setConfirmingAliases(true)
        setAliasError('')
        setAliasSuccessMessage('')
        try {
            const updatedAccount = await api.gmailConfirmAliases({ confirmed_addresses: listToConfirm })
            const freshStatus = await api.fetchSourceStatus('gmail')
            setSourceStatus(freshStatus)

            const confirmed = updatedAccount.confirmed_aliases || freshStatus.confirmed_aliases || listToConfirm
            const isReady = updatedAccount.aliases_ready ?? freshStatus.aliases_ready ?? (confirmed.length > 0)

            setConfirmedAliases(confirmed)
            setAliasesReady(isReady)
            setSelectedAliases(confirmed)
            setIsEditingAliases(false)
            setAliasSuccessMessage(
                `Identità proprietario confermata: ${confirmed.length === 1 ? '1 indirizzo confermato' : `${confirmed.length} indirizzi confermati`
                } per l'attribuzione SENT.`
            )
            return true
        } catch (err) {
            const msg = err instanceof ApiError ? err.message : 'Errore durante la conferma degli alias.'
            setAliasError(msg)
            return false
        } finally {
            setConfirmingAliases(false)
        }
    }

    // Aggiunta manuale di un nuovo alias proposto
    function handleAddCustomAlias(e?: FormEvent) {
        if (e) e.preventDefault()
        const trimmed = newAliasInput.trim().toLowerCase()
        if (!trimmed) return
        if (!trimmed.includes('@') || !trimmed.includes('.')) {
            setAliasError('Inserisci un indirizzo email valido (es. nome@dominio.com).')
            return
        }
        setAliasError('')
        if (!proposedAliases.includes(trimmed)) {
            setProposedAliases((prev) => [...prev, trimmed])
        }
        if (!selectedAliases.includes(trimmed)) {
            setSelectedAliases((prev) => [...prev, trimmed])
        }
        setNewAliasInput('')
    }

    // Toggle selezione checkbox alias
    function toggleAliasSelection(email: string) {
        setAliasError('')
        setSelectedAliases((prev) => {
            if (prev.includes(email)) {
                return prev.filter((item) => item !== email)
            } else {
                return [...prev, email]
            }
        })
    }

    // Effetto iniziale per caricare lo stato o rilevare il callback OAuth
    useEffect(() => {
        const urlParams = new URLSearchParams(window.location.search)
        const connectedParam = urlParams.get('connected')
        const gmailConnectedParam = urlParams.get('gmail_connected')
        const gmailParam = urlParams.get('gmail')
        const errorParam = urlParams.get('error')

        const isGmailConnected =
            connectedParam === 'gmail' ||
            gmailConnectedParam === '1' ||
            gmailParam === 'connected'

        const isGmailError =
            errorParam ||
            (gmailParam === 'oauth_error' ? 'Errore durante autorizzazione OAuth' : null) ||
            (gmailParam === 'consent_denied' ? 'Consenso negato da Google' : null)

        if (isGmailConnected) {
            setSelectedSourceId('gmail')
            refreshStatus('gmail').then((st) => {
                if (st?.account_connected) {
                    setCurrentStep(4)
                    handleTestConnection('gmail')
                }
            })
            // Rimuovi parametri dalla query senza ricaricare la pagina preservando il path
            window.history.replaceState({}, '', window.location.pathname)
        } else if (isGmailError) {
            setOauthError(decodeURIComponent(isGmailError))
            refreshStatus('gmail')
            window.history.replaceState({}, '', window.location.pathname)
        } else {
            refreshStatus(selectedSourceId)
        }
    }, [])

    // Supporto tastiera (Escape per chiudere)
    useEffect(() => {
        function handleKeyDown(e: KeyboardEvent) {
            if (e.key === 'Escape') {
                onClose()
            }
        }
        window.addEventListener('keydown', handleKeyDown)
        return () => window.removeEventListener('keydown', handleKeyDown)
    }, [onClose])

    // Polling per il job di sincronizzazione attivo nello step 6
    useEffect(() => {
        if (!activeJob || !['pending', 'running'].includes(activeJob.status)) return
        const intervalId = window.setInterval(async () => {
            try {
                const st = await api.fetchSourceStatus(selectedSourceId)
                if (st.active_job) {
                    setActiveJob(st.active_job)
                } else if (st.latest_job) {
                    setActiveJob(st.latest_job)
                }
            } catch {
                // Ignore transient polling failure
            }
        }, 2000)
        return () => window.clearInterval(intervalId)
    }, [activeJob, selectedSourceId])

    async function handleCancelActiveJob() {
        if (!activeJob || selectedSourceId !== 'immich' || !['pending', 'running'].includes(activeJob.status)) return
        setSyncCancelling(true)
        setSyncError('')
        try {
            setActiveJob(await api.cancelSourceJob('immich', activeJob.id))
        } catch (err) {
            setSyncError(err instanceof ApiError ? err.message : 'Impossibile annullare il job Immich.')
        } finally {
            setSyncCancelling(false)
        }
    }

    // Funzione copia con fallback universale (supporta anche contesti HTTP non protetti su LAN)
    async function copyText(text: string, key = 'uri') {
        if (!text) return
        try {
            if (navigator.clipboard && navigator.clipboard.writeText) {
                await navigator.clipboard.writeText(text)
            } else {
                const textarea = document.createElement('textarea')
                textarea.value = text
                textarea.style.position = 'fixed'
                textarea.style.opacity = '0'
                document.body.appendChild(textarea)
                textarea.focus()
                textarea.select()
                document.execCommand('copy')
                document.body.removeChild(textarea)
            }
            if (key === 'uri') setCopySuccess(true)
            setCopiedKey(key)
            setTimeout(() => {
                if (key === 'uri') setCopySuccess(false)
                setCopiedKey(null)
            }, 2500)
        } catch {
            // fallback opaco
        }
    }

    async function copyRedirectUri() {
        if (!sourceStatus?.detected_callback_url) return
        await copyText(sourceStatus.detected_callback_url, 'uri')
    }

    // Gestione upload JSON (drag & drop o file input)
    function handleFileUpload(file: File) {
        setCredError('')
        setCredSuccessMessage('')
        const reader = new FileReader()
        reader.onload = (e) => {
            const content = e.target?.result as string
            const validation = parseJsonConfig(content)
            if (!validation.ok) {
                setCredError(validation.error || 'File JSON non valido.')
                setUploadedJsonContent(null)
                setUploadedFileName('')
                return
            }
            setUploadedJsonContent(content)
            setUploadedFileName(file.name)
            setCredSuccessMessage(`File "${file.name}" analizzato con successo. Pronto per il salvataggio crittografato.`)
        }
        reader.onerror = () => {
            setCredError('Impossibile leggere il file selezionato.')
        }
        reader.readAsText(file)
    }

    // Salvataggio credenziali nello Step 2
    async function handleSaveCredentials(e?: FormEvent) {
        if (e) e.preventDefault()
        setCredSaving(true)
        setCredError('')
        setCredSuccessMessage('')

        try {
            let payload: Record<string, unknown> = {}
            if (selectedSourceId === 'immich') {
                if (!immichBaseUrl.trim() || !immichApiKey.trim()) {
                    setCredError("Inserisci sia l'URL dell'istanza Immich che l'API Key.")
                    setCredSaving(false)
                    return
                }
                payload = {
                    base_url: immichBaseUrl.trim(),
                    api_key: immichApiKey.trim(),
                }
            } else {
                if (credMode === 'upload') {
                    if (!uploadedJsonContent) {
                        setCredError('Seleziona o trascina un file JSON di credenziali Google prima di salvare.')
                        setCredSaving(false)
                        return
                    }
                    const parsed = JSON.parse(uploadedJsonContent)
                    payload = {
                        raw_json: parsed,
                        redirect_uri_override: redirectUriOverride.trim() || undefined,
                    }
                } else {
                    if (!clientId.trim() || !clientSecret.trim()) {
                        setCredError('Inserisci sia il Client ID che il Client Secret.')
                        setCredSaving(false)
                        return
                    }
                    payload = {
                        client_id: clientId.trim(),
                        client_secret: clientSecret.trim(),
                        redirect_uri_override: redirectUriOverride.trim() || undefined,
                    }
                }
            }

            const res = await api.saveSourceCredentials(selectedSourceId, payload)
            setCredSuccessMessage(res.message || 'Credenziali client salvate con crittografia AES-256-GCM.')
            const updated = await refreshStatus(selectedSourceId)
            // Avanza automaticamente allo step successivo (step 4 per Immich perché non usa OAuth)
            if (updated?.configured) {
                setTimeout(() => {
                    if (selectedSourceId === 'immich') {
                        setCurrentStep(4)
                        handleTestConnection('immich')
                    } else {
                        setCurrentStep(3)
                    }
                }, 800)
            }
        } catch (err) {
            setCredError(err instanceof ApiError ? err.message : 'Errore nel salvataggio delle credenziali.')
        } finally {
            setCredSaving(false)
        }
    }

    // Avvio autorizzazione OAuth nello Step 3
    async function handleStartOAuth() {
        setOauthStarting(true)
        setOauthError('')
        try {
            const startRes = await api.startSourceOAuth(selectedSourceId)
            if (startRes.authorization_url) {
                window.location.href = startRes.authorization_url
            } else {
                setOauthError('URL di autorizzazione non restituita dal server.')
            }
        } catch (err) {
            setOauthError(err instanceof ApiError ? err.message : "Errore nell'avvio del flusso OAuth Google.")
            setOauthStarting(false)
        }
    }

    // Esecuzione test connessione nello Step 4
    async function handleTestConnection(targetSourceId?: string) {
        const sourceId = typeof targetSourceId === 'string' ? targetSourceId : selectedSourceId
        setTestingConnection(true)
        setTestError('')
        try {
            const res = await api.testSourceConnection(sourceId)
            setTestResult(res)
            if (!res.connected) {
                setTestError(res.message || 'La connessione alla fonte non è riuscita.')
            } else if (sourceId === 'gmail') {
                setProposedAliases((prev) => {
                    const set = new Set(prev)
                    if (res.primary_identity) set.add(res.primary_identity)
                    if (res.aliases) res.aliases.forEach((a) => set.add(a))
                    return Array.from(set)
                })
                setSelectedAliases((prev) => {
                    if (prev.length > 0) return prev
                    if (res.primary_identity) return [res.primary_identity]
                    return res.aliases || []
                })
                await refreshStatus('gmail')
            }
        } catch (err) {
            const msg = err instanceof ApiError ? err.message : 'Errore durante il test di connessione.'
            setTestError(msg)
            setTestResult(null)
        } finally {
            setTestingConnection(false)
        }
    }

    // Esecuzione preview nello Step 5
    async function handleLoadPreview() {
        setPreviewLoading(true)
        setPreviewError('')
        try {
            const afterIso = new Date(periodAfter).toISOString()
            const beforeIso = new Date(periodBefore).toISOString()
            const res = await api.fetchSourcePreview(selectedSourceId, {
                limit: 10,
                period_after: afterIso,
                period_before: beforeIso,
            })
            setPreviewResult(res)
        } catch (err) {
            setPreviewError(err instanceof ApiError ? err.message : "Errore durante il recupero dell'anteprima dati.")
            setPreviewResult(null)
        } finally {
            setPreviewLoading(false)
        }
    }

    // Avvio sincronizzazione nello Step 6
    async function handleTriggerSync() {
        if (selectedSourceId === 'gmail' && !aliasesReady) {
            setSyncError("Confermare almeno un alias email del proprietario prima di avviare l'importazione.")
            return
        }
        setSyncStarting(true)
        setSyncError('')
        try {
            const isImmich = selectedSourceId === 'immich'
            const payload: SourceSyncPayload = {
                album_id: isImmich && immichSelectionMode === 'album' ? immichAlbumId.trim() : undefined,
                period_after: (!isImmich || immichSelectionMode === 'period') ? new Date(periodAfter).toISOString() : undefined,
                period_before: (!isImmich || immichSelectionMode === 'period') ? new Date(periodBefore).toISOString() : undefined,
                item_limit: isImmich ? Math.min(itemLimit, 25) : itemLimit,
                request_budget: isImmich ? 50 : requestBudget,
                batch_size: 25,
                mode: 'full',
            }
            const res = await api.triggerSourceSync(selectedSourceId, payload)
            // Carica lo stato della fonte per inizializzare il job
            if (isImmich) {
                const st = await api.fetchSourceStatus('immich')
                setActiveJob(st.active_job || st.latest_job || null)
            } else {
                const jobData = await api.gmailJob(res.job_id)
                setActiveJob(jobData)
            }
        } catch (err) {
            setSyncError(err instanceof ApiError ? err.message : "Errore nell'avvio della sincronizzazione.")
        } finally {
            setSyncStarting(false)
        }
    }

    // Navigazione tra gli step
    function goToStep(step: WizardStep) {
        if (step > currentStep) return
        setCurrentStep(step)
    }

    const selectedCatalogItem = catalogItems.find((i) => i.id === selectedSourceId)

    return (
        <div className="wizard-modal-overlay" role="dialog" aria-modal="true" aria-labelledby="wizard-title">
            <div className="wizard-modal-container">
                {/* Header Wizard */}
                <header className="wizard-header">
                    <div>
                        <div className="wizard-badge-eyebrow">Connessione Fonte Guidata</div>
                        <h2 id="wizard-title" className="wizard-title">
                            {selectedCatalogItem ? selectedCatalogItem.name : 'Configura Fonte Dati'}
                        </h2>
                    </div>
                    <button
                        type="button"
                        className="wizard-close-btn"
                        onClick={onClose}
                        aria-label="Chiudi procedura guidata"
                        title="Chiudi (Esc)"
                    >
                        ✕
                    </button>
                </header>

                {/* Stepper visivo moderno */}
                <nav className="wizard-stepper" aria-label="Passaggi procedura guidata">
                    {([1, 2, 3, 4, 5, 6] as WizardStep[]).map((stepNumber) => {
                        const isCompleted = stepNumber < currentStep
                        const isCurrent = stepNumber === currentStep
                        return (
                            <button
                                key={stepNumber}
                                type="button"
                                className={`wizard-step-node ${isCurrent ? 'active' : ''} ${isCompleted ? 'completed' : ''}`}
                                onClick={() => goToStep(stepNumber)}
                                disabled={stepNumber > currentStep}
                                aria-current={isCurrent ? 'step' : undefined}
                            >
                                <span className="wizard-step-circle">
                                    {isCompleted ? '✓' : stepNumber}
                                </span>
                                <span className="wizard-step-label">{STEP_LABELS[stepNumber]}</span>
                            </button>
                        )
                    })}
                </nav>

                {/* Corpo del Wizard con Step condizionali */}
                <div className="wizard-body">
                    {/* STEP 1: Selezione Fonte */}
                    {currentStep === 1 && (
                        <div className="wizard-step-content">
                            <h3>1. Seleziona la fonte dati da connettere</h3>
                            <p className="wizard-description">
                                GBrain importa in modo sicuro le tue comunicazioni mantenendo il controllo completo dei dati sul tuo server privato.
                            </p>

                            <div className="source-catalog-grid">
                                {catalogItems.map((item) => {
                                    const isAvailable = isConnectorAvailable(item)
                                    const isSelected = selectedSourceId === item.id
                                    return (
                                        <div
                                            key={item.id}
                                            className={`source-card-selectable ${isSelected ? 'selected' : ''} ${!isAvailable ? 'disabled' : ''}`}
                                            onClick={() => {
                                                if (isAvailable) {
                                                    setSelectedSourceId(item.id)
                                                    refreshStatus(item.id)
                                                }
                                            }}
                                            role="button"
                                            tabIndex={isAvailable ? 0 : -1}
                                            aria-disabled={!isAvailable}
                                            onKeyDown={(e) => {
                                                if (isAvailable && (e.key === ' ' || e.key === 'Enter')) {
                                                    setSelectedSourceId(item.id)
                                                    refreshStatus(item.id)
                                                }
                                            }}
                                        >
                                            <div className="source-card-header">
                                                <span className="source-card-icon" aria-hidden="true">
                                                    {item.id === 'gmail' ? '✉️' : item.icon === 'chat' ? '💬' : item.icon === 'workspace' ? '📁' : '🔗'}
                                                </span>
                                                <span className={`source-status-pill ${isAvailable ? 'available' : 'coming_soon'}`}>
                                                    {isAvailable ? 'Disponibile' : 'Prossimamente'}
                                                </span>
                                            </div>
                                            <div className="source-card-name">{item.name}</div>
                                            <div className="source-card-category">{item.category.toUpperCase()}</div>
                                            <p className="source-card-desc">{item.description}</p>
                                            {isAvailable ? (
                                                <div className="source-card-note available-note">
                                                    ✓ Disponibile - Pronto per la configurazione
                                                </div>
                                            ) : (
                                                <div className="source-card-note">
                                                    Connettore pianificato per le prossime versioni del sistema.
                                                </div>
                                            )}
                                        </div>
                                    )
                                })}
                            </div>

                            <div className="wizard-actions">
                                <button type="button" className="secondary" onClick={onClose}>
                                    Annulla
                                </button>
                                <button
                                    type="button"
                                    className="primary"
                                    disabled={!selectedCatalogItem || !isConnectorAvailable(selectedCatalogItem)}
                                    onClick={() => {
                                        refreshStatus(selectedSourceId)
                                        setCurrentStep(2)
                                    }}
                                >
                                    Continua alla configurazione credenziali →
                                </button>
                            </div>
                        </div>
                    )}

                    {/* STEP 2: Configurazione Credenziali */}
                    {currentStep === 2 && (
                        <div className="wizard-step-content">
                            <h3>2. Configura le credenziali di accesso client</h3>
                            <p className="wizard-description">
                                Registra un'applicazione in <strong>Google Cloud Console</strong> e fornisci le credenziali OAuth 2.0 (Client ID e Client Secret). Le credenziali vengono cifrate a riposo con <strong>AES-256-GCM</strong>.
                            </p>

                            {/* Rilevamento dati di rete locale */}
                            {(() => {
                                const currentHost = window.location.host
                                const currentHostname = window.location.hostname
                                const currentPort = window.location.port ? `:${window.location.port}` : ''
                                const isPrivateIp =
                                    Boolean(sourceStatus?.lan_warning?.is_private_ip) ||
                                    /^(10\.\d{1,3}\.\d{1,3}\.\d{1,3}|192\.168\.\d{1,3}\.\d{1,3}|172\.(1[6-9]|2\d|3[01])\.\d{1,3}\.\d{1,3})$/.test(currentHostname)
                                const effectiveCallbackUrl =
                                    sourceStatus?.detected_callback_url || `${window.location.origin}/api/sources/${selectedSourceId}/oauth/callback`
                                const recommendedHostsEntry = `${currentHostname || '10.0.0.105'} gbrain.local`
                                const recommendedPublicUrl = `http://gbrain.local${currentPort || ':8185'}`
                                const recommendedCallbackUrl = `http://gbrain.local${currentPort || ':8185'}/api/sources/${selectedSourceId}/oauth/callback`

                                return (
                                    <>
                                        {/* Box Redirect URI attualmente calcolato dal sistema */}
                                        <div className="wizard-callout info">
                                            <div className="callout-header">
                                                <strong>Redirect URI Calcolato per Google Cloud Console</strong>
                                            </div>
                                            <p>Copia questo URI e incollalo nella sezione <em>"URI di reindirizzamento autorizzati"</em> della tua applicazione Google OAuth:</p>
                                            <div className="copy-uri-box">
                                                <code>{effectiveCallbackUrl}</code>
                                                <button
                                                    type="button"
                                                    className="secondary compact"
                                                    onClick={() => copyText(effectiveCallbackUrl, 'uri')}
                                                    disabled={!effectiveCallbackUrl}
                                                >
                                                    {copiedKey === 'uri' ? '✓ Copiato!' : 'Copia URI negli appunti'}
                                                </button>
                                            </div>
                                        </div>

                                        {/* Avviso contestuale dettagliato per accesso su IP privato LAN */}
                                        {isPrivateIp && (
                                            <div className="wizard-callout warning lan-warning-box">
                                                <div className="callout-header">
                                                    <span aria-hidden="true" style={{ fontSize: '1.2rem' }}>⚠️</span>
                                                    <strong>Avviso Vincolo Google OAuth su Rete Locale LAN ({currentHost})</strong>
                                                </div>
                                                <p className="lan-reason-text">
                                                    <strong>a) Perché Google rifiuta gli indirizzi IP privati numerici?</strong><br />
                                                    Google Cloud Console <strong>non consente</strong> per policy di sicurezza l'inserimento di indirizzi IP privati nudi (es. <code>{currentHostname}</code>) negli <em>"URI di reindirizzamento autorizzati"</em> per client Web OAuth 2.0. Google ammette esclusivamente <code>http://localhost</code>, <code>http://127.0.0.1</code> oppure nomi di dominio/hostname validi con estensione.
                                                </p>

                                                <div className="lan-solutions-container">
                                                    <div className="solution-item">
                                                        <div className="solution-title">
                                                            <strong>b.1) Soluzione Raccomandata: Assegna un Hostname Locale (File hosts)</strong>
                                                        </div>
                                                        <ol className="solution-steps">
                                                            <li>
                                                                Sul PC client da cui usi il browser, apri il file <code>hosts</code> con permessi di amministratore:
                                                                <ul>
                                                                    <li><strong>Windows</strong>: <code>C:\Windows\System32\drivers\etc\hosts</code> (apri Blocco Note come Amministratore)</li>
                                                                    <li><strong>Linux / macOS</strong>: <code>/etc/hosts</code> (es. <code>sudo nano /etc/hosts</code>)</li>
                                                                </ul>
                                                                Aggiungi questa riga:
                                                                <div className="copy-code-inline">
                                                                    <code>{recommendedHostsEntry}</code>
                                                                    <button
                                                                        type="button"
                                                                        className="secondary compact mini-btn"
                                                                        onClick={() => copyText(recommendedHostsEntry, 'hosts')}
                                                                    >
                                                                        {copiedKey === 'hosts' ? '✓ Copiato!' : 'Copia'}
                                                                    </button>
                                                                </div>
                                                            </li>
                                                            <li>
                                                                Nel file <code>.env</code> di GBrain sul server, imposta o aggiorna la variabile:
                                                                <div className="copy-code-inline">
                                                                    <code>GBRAIN_PUBLIC_URL={recommendedPublicUrl}</code>
                                                                    <button
                                                                        type="button"
                                                                        className="secondary compact mini-btn"
                                                                        onClick={() => copyText(`GBRAIN_PUBLIC_URL=${recommendedPublicUrl}`, 'env')}
                                                                    >
                                                                        {copiedKey === 'env' ? '✓ Copiato!' : 'Copia'}
                                                                    </button>
                                                                </div>
                                                            </li>
                                                            <li>
                                                                In Google Cloud Console, aggiungi negli <em>"URI di reindirizzamento autorizzati"</em>:
                                                                <div className="copy-code-inline">
                                                                    <code>{recommendedCallbackUrl}</code>
                                                                    <button
                                                                        type="button"
                                                                        className="secondary compact mini-btn"
                                                                        onClick={() => copyText(recommendedCallbackUrl, 'rec_uri')}
                                                                    >
                                                                        {copiedKey === 'rec_uri' ? '✓ Copiato!' : 'Copia URI'}
                                                                    </button>
                                                                </div>
                                                            </li>
                                                            <li>
                                                                Accedi a GBrain navigando nel browser all'indirizzo <code>{recommendedPublicUrl}</code>.
                                                            </li>
                                                        </ol>
                                                    </div>

                                                    <div className="solution-item">
                                                        <div className="solution-title">
                                                            <strong>b.2) Soluzione Alternativa: Reverse Proxy Locale con HTTPS (TLS)</strong>
                                                        </div>
                                                        <p className="solution-desc">
                                                            Configura un reverse proxy locale (come Caddy, Nginx o Traefik) con un certificato TLS/SSL (es. mkcert o Let's Encrypt), inoltrando le richieste HTTPS a GBrain e impostando <code>GBRAIN_PUBLIC_URL=https://...</code>. Google Cloud Console accetta domini o hostname con protocollo HTTPS.
                                                        </p>
                                                    </div>
                                                </div>
                                            </div>
                                        )}
                                    </>
                                )
                            })()}

                            {/* Indicatore se le credenziali sono già configurate */}
                            {sourceStatus?.configured && (
                                <div className="wizard-callout success">
                                    <div className="callout-header">
                                        <span aria-hidden="true">🔒</span>
                                        <strong>Credenziali client già configurate sul server</strong>
                                    </div>
                                    <p>
                                        Origine credenziali: <code>{sourceStatus.auth_source ?? 'db_encrypted'}</code>
                                        {sourceStatus.client_id_preview && ` · ID Client: ${sourceStatus.client_id_preview}`}
                                    </p>
                                    <div className="callout-actions">
                                        <button
                                            type="button"
                                            className="primary compact"
                                            onClick={() => setCurrentStep(3)}
                                        >
                                            Mantieni credenziali esistenti e procedi →
                                        </button>
                                    </div>
                                </div>
                            )}

                            {selectedSourceId === 'immich' ? (
                                <form onSubmit={handleSaveCredentials} className="manual-cred-form" style={{ marginTop: '1rem' }}>
                                    <div className="wizard-callout info">
                                        <div className="callout-header">
                                            <span aria-hidden="true">🔒</span>
                                            <strong>Connettore Immich Read-Only (Sola Lettura)</strong>
                                        </div>
                                        <p>GBrain sincronizza esclusivamente i metadati dell'istanza (timestamp, album, descrizione, persone già etichettate). Nessun file originale o miniatura viene mai scaricato o modificato.</p>
                                    </div>
                                    <label>
                                        URL Istanza Immich (HTTPS o HTTP su rete LAN locale)
                                        <input
                                            type="text"
                                            placeholder="es. http://192.168.1.100:2283 o https://immich.example.com"
                                            value={immichBaseUrl}
                                            onChange={(e) => setImmichBaseUrl(e.target.value)}
                                            required
                                        />
                                    </label>
                                    <label>
                                        API Key Immich
                                        <div className="password-input-wrap">
                                            <input
                                                type={showSecret ? 'text' : 'password'}
                                                placeholder="Genera una API Key in Immich > Account Settings > API Keys"
                                                value={immichApiKey}
                                                onChange={(e) => setImmichApiKey(e.target.value)}
                                                required
                                            />
                                            <button
                                                type="button"
                                                className="secondary compact"
                                                onClick={() => setShowSecret(!showSecret)}
                                            >
                                                {showSecret ? 'Nascondi' : 'Mostra'}
                                            </button>
                                        </div>
                                    </label>
                                </form>
                            ) : (
                                <div className="cred-tabs-wrap">
                                    <div className="cred-tabs" role="tablist">
                                        <button
                                            type="button"
                                            role="tab"
                                            aria-selected={credMode === 'upload'}
                                            className={`cred-tab-btn ${credMode === 'upload' ? 'active' : ''}`}
                                            onClick={() => setCredMode('upload')}
                                        >
                                            📁 Carica file JSON (Consigliato)
                                        </button>
                                        <button
                                            type="button"
                                            role="tab"
                                            aria-selected={credMode === 'manual'}
                                            className={`cred-tab-btn ${credMode === 'manual' ? 'active' : ''}`}
                                            onClick={() => setCredMode('manual')}
                                        >
                                            ✏️ Inserimento manuale ID & Secret
                                        </button>
                                    </div>

                                    {credMode === 'upload' ? (
                                        <div className="upload-dropzone-container">
                                            <div
                                                className={`dropzone ${uploadedFileName ? 'has-file' : ''}`}
                                                onDragOver={(e) => e.preventDefault()}
                                                onDrop={(e) => {
                                                    e.preventDefault()
                                                    const droppedFile = e.dataTransfer.files[0]
                                                    if (droppedFile) {
                                                        handleFileUpload(droppedFile)
                                                    }
                                                }}
                                            >
                                                <span className="dropzone-icon" aria-hidden="true">📥</span>
                                                <p className="dropzone-text">
                                                    {uploadedFileName ? (
                                                        <strong>File selezionato: {uploadedFileName}</strong>
                                                    ) : (
                                                        <>
                                                            Trascina qui il file <code>client_secret_*.json</code> scaricato da Google Cloud Console
                                                            <br />
                                                            <span className="muted">oppure fai clic sul pulsante sottostante</span>
                                                        </>
                                                    )}
                                                </p>
                                                <label className="button-like secondary">
                                                    Sfoglia file JSON…
                                                    <input
                                                        type="file"
                                                        accept=".json,application/json"
                                                        style={{ display: 'none' }}
                                                        onChange={(e) => {
                                                            const chosenFile = e.target.files?.[0]
                                                            if (chosenFile) {
                                                                handleFileUpload(chosenFile)
                                                            }
                                                        }}
                                                    />
                                                </label>
                                            </div>
                                        </div>
                                    ) : (
                                        <form onSubmit={handleSaveCredentials} className="manual-cred-form">
                                            <label>
                                                Client ID Google
                                                <input
                                                    type="text"
                                                    placeholder="es. 123456789-abcdef.apps.googleusercontent.com"
                                                    value={clientId}
                                                    onChange={(e) => setClientId(e.target.value)}
                                                    required
                                                />
                                            </label>
                                            <label>
                                                Client Secret Google
                                                <div className="password-input-wrap">
                                                    <input
                                                        type={showSecret ? 'text' : 'password'}
                                                        placeholder="GOCSPX-..."
                                                        value={clientSecret}
                                                        onChange={(e) => setClientSecret(e.target.value)}
                                                        required
                                                    />
                                                    <button
                                                        type="button"
                                                        className="secondary compact"
                                                        onClick={() => setShowSecret(!showSecret)}
                                                    >
                                                        {showSecret ? 'Nascondi' : 'Mostra'}
                                                    </button>
                                                </div>
                                            </label>
                                            <label>
                                                Redirect URI Override (Opzionale)
                                                <input
                                                    type="url"
                                                    placeholder="Lascia vuoto per rilevamento automatico dal server"
                                                    value={redirectUriOverride}
                                                    onChange={(e) => setRedirectUriOverride(e.target.value)}
                                                />
                                            </label>
                                        </form>
                                    )}
                                </div>
                            )}

                            {credError && <p className="error" role="alert">{credError}</p>}
                            {credSuccessMessage && <p className="success-msg" role="status">{credSuccessMessage}</p>}

                            <div className="wizard-actions">
                                <button type="button" className="secondary" onClick={() => setCurrentStep(1)}>
                                    ← Indietro
                                </button>
                                <button
                                    type="button"
                                    className="primary"
                                    disabled={credSaving || (selectedSourceId === 'immich' ? (!immichBaseUrl.trim() || !immichApiKey.trim()) : (credMode === 'upload' && !uploadedJsonContent))}
                                    onClick={() => handleSaveCredentials()}
                                >
                                    {credSaving ? 'Salvataggio sicuro in corso…' : 'Salva credenziali crittografate e continua →'}
                                </button>
                            </div>
                        </div>
                    )}

                    {/* STEP 3: Autorizzazione Account */}
                    {currentStep === 3 && (
                        <div className="wizard-step-content">
                            {selectedSourceId === 'immich' ? (
                                <>
                                    <h3>3. Autenticazione Immich</h3>
                                    <div className="wizard-callout info">
                                        <div className="callout-header">
                                            <span aria-hidden="true">🔑</span>
                                            <strong>Autenticazione con API Key Header</strong>
                                        </div>
                                        <p>Immich utilizza l'autenticazione tramite chiave API. L'API Key configurata nel passaggio precedente è salvata e cifrata con AES-256-GCM. Il flusso OAuth non è necessario.</p>
                                    </div>
                                    <div className="wizard-actions">
                                        <button type="button" className="secondary" onClick={() => setCurrentStep(2)}>
                                            ← Torna alle credenziali
                                        </button>
                                        <button
                                            type="button"
                                            className="primary"
                                            onClick={() => {
                                                setCurrentStep(4)
                                                handleTestConnection('immich')
                                            }}
                                        >
                                            Verifica connessione Immich →
                                        </button>
                                    </div>
                                </>
                            ) : (
                                <>
                                    <h3>3. Autorizzazione account con Google OAuth</h3>
                                    <p className="wizard-description">
                                        Collega in modo sicuro la tua casella di posta per consentire l'analisi del grafo di conoscenza.
                                    </p>

                                    <div className="privacy-badge-box">
                                        <div className="privacy-icon" aria-hidden="true">🛡️</div>
                                        <div>
                                            <h4>Politica di Accesso e Privacy Restrittiva</h4>
                                            <ul className="privacy-list">
                                                <li>
                                                    <strong>Scope richiesto</strong>: <code>https://www.googleapis.com/auth/gmail.readonly</code> in modalità di sola lettura.
                                                </li>
                                                <li>
                                                    <strong>Vincolo cartella SENT</strong>: GBrain scarica ed elabora <strong>esclusivamente la posta inviata</strong> dall'utente per mappare le interazioni. Nessuna email in arrivo (INBOX), bozza o messaggio privato non inviato viene mai aperto o salvato.
                                                </li>
                                                <li>
                                                    <strong>Sovranità locale</strong>: I dati e i token non lasciano mai la tua istanza privata.
                                                </li>
                                            </ul>
                                        </div>
                                    </div>

                                    {sourceStatus?.account_connected ? (
                                        <div className="wizard-callout success">
                                            <div className="callout-header">
                                                <span aria-hidden="true">✅</span>
                                                <strong>Account attualmente connesso: {sourceStatus.primary_email}</strong>
                                            </div>
                                            <p>L'account Google risulta già autorizzato e pronto per la verifica.</p>
                                            <div className="callout-actions">
                                                <button
                                                    type="button"
                                                    className="primary"
                                                    onClick={() => {
                                                        setCurrentStep(4)
                                                        handleTestConnection()
                                                    }}
                                                >
                                                    Procedi con questo account →
                                                </button>
                                                <button
                                                    type="button"
                                                    className="secondary"
                                                    disabled={oauthStarting}
                                                    onClick={handleStartOAuth}
                                                >
                                                    {oauthStarting ? 'Reindirizzamento…' : 'Ricollega o cambia account Google'}
                                                </button>
                                            </div>
                                        </div>
                                    ) : (
                                        <div className="oauth-start-wrap">
                                            <p>Fai clic sul pulsante sottostante per avviare il flusso di consenso sul server di Google:</p>
                                            <button
                                                type="button"
                                                className="google-oauth-btn"
                                                disabled={oauthStarting}
                                                onClick={handleStartOAuth}
                                            >
                                                <span className="google-icon" aria-hidden="true">G</span>
                                                <span>{oauthStarting ? 'Preparazione collegamento…' : 'Collega con Google'}</span>
                                            </button>
                                        </div>
                                    )}

                                    {oauthError && <p className="error" role="alert">{oauthError}</p>}

                                    <div className="wizard-actions">
                                        <button type="button" className="secondary" onClick={() => setCurrentStep(2)}>
                                            ← Torna alle credenziali
                                        </button>
                                        {sourceStatus?.account_connected && (
                                            <button
                                                type="button"
                                                className="primary"
                                                onClick={() => {
                                                    setCurrentStep(4)
                                                    handleTestConnection()
                                                }}
                                            >
                                                Continua alla verifica connessione →
                                            </button>
                                        )}
                                    </div>
                                </>
                            )}
                        </div>
                    )}

                    {/* STEP 4: Verifica Connessione */}
                    {currentStep === 4 && (
                        <div className="wizard-step-content">
                            <h3>4. Verifica della connessione remota</h3>
                            <p className="wizard-description">
                                Verifichiamo la raggiungibilità dell'API Google e la validità del token di accesso dell'account collegato.
                            </p>

                            {testingConnection && (
                                <div className="status-loading-box">
                                    <div className="spinner" aria-hidden="true"></div>
                                    <p>
                                        {selectedSourceId === 'immich'
                                            ? 'Verifica versione e capacità Immich in corso…'
                                            : 'Interrogazione profilo Gmail in corso…'}
                                    </p>
                                </div>
                            )}

                            {testResult && (
                                <div className={`connection-result-card ${testResult.connected ? 'success' : 'failed'}`}>
                                    <div className="result-header">
                                        <span className="result-icon">{testResult.connected ? '✅' : '❌'}</span>
                                        <div>
                                            <h4>{testResult.connected ? 'Connessione verificata con successo' : 'Verifica non riuscita'}</h4>
                                            <p className="muted">{testResult.message}</p>
                                        </div>
                                    </div>

                                    {testResult.connected && (
                                        <div className="result-details-grid">
                                            {selectedSourceId === 'immich' && (
                                                <>
                                                    <div>
                                                        <span className="detail-label">Versione server:</span>
                                                        <strong className="detail-val">{testResult.server_version ?? 'Non disponibile'}</strong>
                                                    </div>
                                                    <div>
                                                        <span className="detail-label">Capacità:</span>
                                                        <strong className="detail-val">
                                                            {Object.entries(testResult.capabilities ?? {})
                                                                .filter(([, enabled]) => enabled)
                                                                .map(([name]) => name)
                                                                .join(', ') || 'Nessuna capacità utilizzabile'}
                                                        </strong>
                                                    </div>
                                                </>
                                            )}
                                            <div>
                                                <span className="detail-label">Identità Primaria:</span>
                                                <strong className="detail-val">{testResult.primary_identity ?? 'Non specificato'}</strong>
                                            </div>
                                            <div>
                                                <span className="detail-label">Latenza API:</span>
                                                <span className="detail-val badge-latency">{testResult.latency_ms ? `${testResult.latency_ms.toFixed(0)} ms` : '—'}</span>
                                            </div>
                                            {testResult.aliases.length > 0 && (
                                                <div className="full-width">
                                                    <span className="detail-label">Alias rilevati:</span>
                                                    <div className="alias-tags">
                                                        {testResult.aliases.map((al) => (
                                                            <span key={al} className="alias-tag">{al}</span>
                                                        ))}
                                                    </div>
                                                </div>
                                            )}
                                        </div>
                                    )}
                                </div>
                            )}

                            {/* Sezione Conferma Alias Autobiografici (obbligatoria per Gmail) */}
                            {selectedSourceId === 'gmail' && (testResult?.connected || sourceStatus?.account_connected) && (
                                <div className="alias-confirmation-card">
                                    <div className="alias-card-header">
                                        <span className="alias-card-icon" aria-hidden="true">🛡️</span>
                                        <div>
                                            <h4>Identità Autobiografica e Conferma Alias</h4>
                                            <p className="muted">
                                                GBrain importa e indicizza <strong>esclusivamente i messaggi inviati (SENT)</strong> attribuibili a te. Prima di sincronizzare, devi confermare esplicitamente quale indirizzo email ti appartiene.
                                            </p>
                                        </div>
                                        <div className="alias-header-badge">
                                            {aliasesReady ? (
                                                <span className="status-pill active">● Identità Confermata</span>
                                            ) : (
                                                <span className="status-pill warning">⚠️ In attesa di conferma</span>
                                            )}
                                        </div>
                                    </div>

                                    <div className="alias-primary-row">
                                        <span className="detail-label">Indirizzo Primario Autenticato:</span>
                                        <strong className="primary-email-badge">
                                            ✉️ {sourceStatus?.primary_email || testResult?.primary_identity || 'Non rilevato'}
                                        </strong>
                                    </div>

                                    {aliasesReady && !isEditingAliases ? (
                                        <div className="confirmed-aliases-view">
                                            <div>
                                                <span className="detail-label">Alias confermati e attivi per l'attribuzione SENT:</span>
                                                <div className="alias-badges-list">
                                                    {confirmedAliases.map((addr) => (
                                                        <span key={addr} className="alias-confirmed-pill">
                                                            ✓ {addr}
                                                        </span>
                                                    ))}
                                                </div>
                                            </div>
                                            <div className="confirmed-aliases-actions">
                                                <button
                                                    type="button"
                                                    className="secondary compact"
                                                    onClick={() => setIsEditingAliases(true)}
                                                >
                                                    ✏️ Modifica o aggiungi alias
                                                </button>
                                            </div>
                                        </div>
                                    ) : (
                                        <div className="alias-selection-form">
                                            <p className="alias-selection-intro">
                                                Seleziona gli indirizzi da cui invii o hai inviato email come proprietario:
                                            </p>

                                            <div className="alias-checklist">
                                                {proposedAliases.map((addr) => {
                                                    const isChecked = selectedAliases.includes(addr)
                                                    const isPrimary = addr === (sourceStatus?.primary_email || testResult?.primary_identity)
                                                    const isAlreadyConfirmed = confirmedAliases.includes(addr)
                                                    return (
                                                        <label key={addr} className={`alias-check-item ${isChecked ? 'checked' : ''}`}>
                                                            <input
                                                                type="checkbox"
                                                                checked={isChecked}
                                                                onChange={() => toggleAliasSelection(addr)}
                                                            />
                                                            <span className="alias-check-label">
                                                                {addr}
                                                                {isPrimary && <span className="sub-badge">Primario</span>}
                                                                {isAlreadyConfirmed && <span className="sub-badge confirmed">Già confermato</span>}
                                                            </span>
                                                        </label>
                                                    )
                                                })}
                                            </div>

                                            <form className="add-alias-inline" onSubmit={handleAddCustomAlias}>
                                                <input
                                                    type="email"
                                                    placeholder="Aggiungi un altro alias (es. me@azienda.com)"
                                                    value={newAliasInput}
                                                    onChange={(e) => setNewAliasInput(e.target.value)}
                                                />
                                                <button type="submit" className="secondary compact" disabled={!newAliasInput.trim()}>
                                                    + Aggiungi
                                                </button>
                                            </form>

                                            {aliasError && <p className="error" role="alert">{aliasError}</p>}
                                            {aliasSuccessMessage && <p className="success-msg" role="status">✅ {aliasSuccessMessage}</p>}

                                            <div className="alias-action-bar">
                                                <button
                                                    type="button"
                                                    className="primary"
                                                    disabled={confirmingAliases || selectedAliases.length === 0}
                                                    onClick={() => handleConfirmAliases()}
                                                >
                                                    {confirmingAliases ? 'Conferma in corso…' : '✓ Conferma Alias Selezionati'}
                                                </button>
                                                {isEditingAliases && aliasesReady && (
                                                    <button
                                                        type="button"
                                                        className="secondary compact"
                                                        onClick={() => {
                                                            setIsEditingAliases(false)
                                                            setSelectedAliases(confirmedAliases)
                                                        }}
                                                    >
                                                        Annulla modifiche
                                                    </button>
                                                )}
                                            </div>
                                        </div>
                                    )}
                                </div>
                            )}

                            {testError && <p className="error" role="alert">{testError}</p>}

                            <div className="wizard-actions">
                                <button type="button" className="secondary" onClick={() => setCurrentStep(3)}>
                                    ← Torna all'autorizzazione
                                </button>
                                <button
                                    type="button"
                                    className="secondary"
                                    disabled={testingConnection}
                                    onClick={() => handleTestConnection()}
                                >
                                    {testingConnection ? 'Test in corso…' : 'Ripeti test'}
                                </button>
                                <button
                                    type="button"
                                    className="primary"
                                    disabled={!testResult?.connected || testingConnection}
                                    onClick={() => {
                                        if (selectedSourceId === 'immich') {
                                            onSuccess()
                                            return
                                        }
                                        setCurrentStep(5)
                                        handleLoadPreview()
                                    }}
                                >
                                    {selectedSourceId === 'immich'
                                        ? 'Completa collegamento'
                                        : "Continua all'anteprima dati →"}
                                </button>
                            </div>
                        </div>
                    )}

                    {/* STEP 5: Anteprima Dati / Importazione */}
                    {currentStep === 5 && (
                        <div className="wizard-step-content">
                            <h3>{selectedSourceId === 'immich' ? '5. Anteprima metadati Immich' : '5. Anteprima messaggi inviati (SENT)'}</h3>
                            <p className="wizard-description">
                                {selectedSourceId === 'immich'
                                    ? 'Ispeziona i metadati degli asset prima di avviare l\'importazione. Nessun originale verrà scaricato (limite massimo di 25 elementi).'
                                    : 'Ispeziona una stima della posta inviata e visualizza un campione dei messaggi prima di avviare qualsiasi sincronizzazione persistente.'}
                            </p>

                            {selectedSourceId === 'immich' ? (
                                <div className="preview-params-bar">
                                    <div className="param-field" style={{ minWidth: '220px' }}>
                                        <label htmlFor="immich-sel-mode">Modalità di selezione</label>
                                        <select
                                            id="immich-sel-mode"
                                            value={immichSelectionMode}
                                            onChange={(e) => setImmichSelectionMode(e.target.value as 'period' | 'album')}
                                        >
                                            <option value="period">📅 Intervallo Temporale</option>
                                            <option value="album">📁 Per Album ID</option>
                                        </select>
                                    </div>
                                    {immichSelectionMode === 'album' ? (
                                        <div className="param-field" style={{ flex: 1 }}>
                                            <label htmlFor="immich-album-id">Album ID</label>
                                            <input
                                                id="immich-album-id"
                                                type="text"
                                                placeholder="UUID dell'album da importare"
                                                value={immichAlbumId}
                                                onChange={(e) => setImmichAlbumId(e.target.value)}
                                            />
                                        </div>
                                    ) : (
                                        <>
                                            <div className="param-field">
                                                <label htmlFor="period-after">Dal</label>
                                                <input
                                                    id="period-after"
                                                    type="datetime-local"
                                                    value={periodAfter}
                                                    onChange={(e) => setPeriodAfter(e.target.value)}
                                                />
                                            </div>
                                            <div className="param-field">
                                                <label htmlFor="period-before">Al</label>
                                                <input
                                                    id="period-before"
                                                    type="datetime-local"
                                                    value={periodBefore}
                                                    onChange={(e) => setPeriodBefore(e.target.value)}
                                                />
                                            </div>
                                        </>
                                    )}
                                    <button
                                        type="button"
                                        className="secondary"
                                        disabled={previewLoading}
                                        onClick={handleLoadPreview}
                                    >
                                        {previewLoading ? 'Lettura anteprima…' : 'Ricalcola anteprima'}
                                    </button>
                                </div>
                            ) : (
                                <div className="preview-params-bar">
                                    <div className="param-field">
                                        <label htmlFor="period-after">Dal</label>
                                        <input
                                            id="period-after"
                                            type="datetime-local"
                                            value={periodAfter}
                                            onChange={(e) => setPeriodAfter(e.target.value)}
                                        />
                                    </div>
                                    <div className="param-field">
                                        <label htmlFor="period-before">Al</label>
                                        <input
                                            id="period-before"
                                            type="datetime-local"
                                            value={periodBefore}
                                            onChange={(e) => setPeriodBefore(e.target.value)}
                                        />
                                    </div>
                                    <button
                                        type="button"
                                        className="secondary"
                                        disabled={previewLoading}
                                        onClick={handleLoadPreview}
                                    >
                                        {previewLoading ? 'Lettura anteprima…' : 'Ricalcola anteprima'}
                                    </button>
                                </div>
                            )}

                            {previewLoading && (
                                <div className="status-loading-box">
                                    <div className="spinner" aria-hidden="true"></div>
                                    <p>Recupero campioni in corso da {selectedSourceId === 'immich' ? 'Immich' : 'Gmail'}…</p>
                                </div>
                            )}

                            {previewResult && (
                                <div className="preview-results-wrap">
                                    <div className="preview-summary-card">
                                        <div>
                                            <span className="summary-title">Stima messaggi SENT disponibili</span>
                                            <div className="summary-count">
                                                ~{previewResult.estimated_count} messaggi
                                            </div>
                                        </div>
                                        <div className="preview-query-badge">
                                            Query Gmail: <code>{previewResult.query}</code>
                                        </div>
                                    </div>

                                    <div className="preview-samples-section">
                                        <h4>Campione messaggi (primi {previewResult.sample_items.length}):</h4>
                                        {previewResult.sample_items.length === 0 ? (
                                            <p className="empty-samples">Nessun messaggio trovato nell'intervallo specificato.</p>
                                        ) : (
                                            <div className="sample-items-list">
                                                {previewResult.sample_items.map((sample) => (
                                                    <div key={sample.id} className="sample-item-card">
                                                        <div className="sample-top">
                                                            <span className="sample-to">A: {sample.to.join(', ') || '(destinatario non specificato)'}</span>
                                                            <span className="sample-date">
                                                                {sample.date ? new Date(sample.date).toLocaleString('it-IT') : '—'}
                                                            </span>
                                                        </div>
                                                        <div className="sample-subject">{sample.subject || '(Nessun oggetto)'}</div>
                                                        {sample.snippet && <p className="sample-snippet">{sample.snippet}</p>}
                                                    </div>
                                                ))}
                                            </div>
                                        )}
                                    </div>

                                    <div className="wizard-callout info">
                                        💡 <em>{previewResult.note}</em>. Nessun dato è stato ancora salvato nel database locale.
                                    </div>
                                </div>
                            )}

                            {previewError && <p className="error" role="alert">{previewError}</p>}

                            <div className="wizard-actions">
                                <button type="button" className="secondary" onClick={() => setCurrentStep(4)}>
                                    ← Torna alla verifica
                                </button>
                                <button
                                    type="button"
                                    className="primary"
                                    disabled={previewLoading}
                                    onClick={() => setCurrentStep(6)}
                                >
                                    Continua alla conferma e sync →
                                </button>
                            </div>
                        </div>
                    )}

                    {/* STEP 6: Conferma e Sincronizzazione */}
                    {currentStep === 6 && (
                        <div className="wizard-step-content">
                            <h3>6. Conferma parametri e avvio sincronizzazione</h3>
                            <p className="wizard-description">
                                Riepilogo finale delle impostazioni. Al click, verrà accodato un job Procrastinate asincrono.
                            </p>

                            <div className="sync-summary-grid">
                                <div className="summary-item">
                                    <span className="summary-key">Fonte Dati</span>
                                    <strong className="summary-val">{selectedCatalogItem?.name ?? selectedSourceId}</strong>
                                </div>
                                <div className="summary-item">
                                    <span className="summary-key">
                                        {selectedSourceId === 'immich' ? 'Istanza verificata' : 'Account autenticato'}
                                    </span>
                                    <strong className="summary-val">
                                        {selectedSourceId === 'immich' ? (testResult?.primary_identity ?? 'Immich') : (sourceStatus?.primary_email ?? '—')}
                                    </strong>
                                </div>
                                <div className="summary-item">
                                    <span className="summary-key">
                                        {selectedSourceId === 'immich' ? 'Selezione' : 'Alias confermati'}
                                    </span>
                                    {selectedSourceId === 'gmail' ? (
                                        aliasesReady && confirmedAliases.length > 0 ? (
                                            <strong className="summary-val text-success">
                                                ✅ {confirmedAliases.join(', ')}
                                            </strong>
                                        ) : (
                                            <strong className="summary-val text-warning">
                                                ⚠️ Nessun alias confermato
                                            </strong>
                                        )
                                    ) : (
                                        <strong className="summary-val">
                                            {immichAlbumId ? `Album ${immichAlbumId}` : 'Periodo selezionato'}
                                        </strong>
                                    )}
                                </div>
                                <div className="summary-item">
                                    <span className="summary-key">Intervallo Temporale</span>
                                    <strong className="summary-val">
                                        {new Date(periodAfter).toLocaleDateString('it-IT')} → {new Date(periodBefore).toLocaleDateString('it-IT')}
                                    </strong>
                                </div>
                                <div className="summary-item">
                                    <span className="summary-key">Modalità import</span>
                                    <strong className="summary-val">
                                        {selectedSourceId === 'immich' ? 'Solo metadati (max 25 asset)' : 'Full (iniziale)'}
                                    </strong>
                                </div>
                            </div>

                            {selectedSourceId === 'gmail' ? (
                                <div className="sync-policy-controls">
                                    <label>
                                        Limite massimo messaggi da elaborare
                                        <input
                                            type="number"
                                            min={1}
                                            max={5000}
                                            value={itemLimit}
                                            onChange={(e) => setItemLimit(Number(e.target.value))}
                                        />
                                    </label>
                                    <label>
                                        Budget massimo chiamate API
                                        <input
                                            type="number"
                                            min={1}
                                            max={2000}
                                            value={requestBudget}
                                            onChange={(e) => setRequestBudget(Number(e.target.value))}
                                        />
                                    </label>
                                </div>
                            ) : (
                                <p className="muted">L'importazione è read-only, solo metadati e limitata a 25 asset.</p>
                            )}

                            {/* Stato del Job attivo se avviato */}
                            {activeJob ? (
                                <div className="job-status-panel">
                                    <div className="job-status-top">
                                        <div className="job-status-title">
                                            <strong>Job Procrastinate ID #{activeJob.id.slice(0, 8)}</strong>
                                            <span className={`status-pill status-${activeJob.status}`}>
                                                {activeJob.status.toUpperCase()}
                                            </span>
                                        </div>
                                        <span className="job-items-count">
                                            {activeJob.processed_items} {selectedSourceId === 'immich' ? 'asset elaborati' : 'messaggi elaborati'}
                                        </span>
                                    </div>

                                    {['pending', 'running'].includes(activeJob.status) && (
                                        <>
                                            <div className="progress-bar-container">
                                                <div className="progress-bar-indeterminate"></div>
                                            </div>
                                            {selectedSourceId === 'immich' && (
                                                <button
                                                    type="button"
                                                    className="secondary compact"
                                                    disabled={syncCancelling}
                                                    onClick={handleCancelActiveJob}
                                                >
                                                    {syncCancelling ? 'Annullamento…' : 'Annulla import Immich'}
                                                </button>
                                            )}
                                        </>
                                    )}

                                    {['completed', 'completed_with_warnings'].includes(activeJob.status) && (
                                        <div className="job-finished-message">
                                            ✅ Sincronizzazione iniziale completata con successo! {selectedSourceId === 'immich' ? 'I metadati degli asset sono stati' : 'I messaggi sono stati'} memorizzati nell'archivio protetto.
                                        </div>
                                    )}

                                    {activeJob.status === 'cancelled' && (
                                        <div className="job-failed-message">Import annullato in sicurezza.</div>
                                    )}

                                    {activeJob.status === 'failed' && (
                                        <div className="job-failed-message">
                                            ❌ Errore durante l'elaborazione del job: {activeJob.error_detail || activeJob.error_code || 'Errore sconosciuto'}
                                        </div>
                                    )}
                                </div>
                            ) : (
                                <>
                                    {selectedSourceId === 'gmail' && !aliasesReady && (
                                        <div className="alias-blocker-callout" role="alert">
                                            <div className="callout-header">
                                                <span className="callout-icon" aria-hidden="true">⚠️</span>
                                                <div>
                                                    <h4>Conferma Alias Richiesta prima dell'Importazione</h4>
                                                    <p className="muted">
                                                        Per la policy di sicurezza autobiografica, GBrain non può sincronizzare la posta inviata finché non confermi esplicitamente la tua identità email.
                                                    </p>
                                                </div>
                                            </div>
                                            <div className="callout-actions">
                                                {(sourceStatus?.primary_email || testResult?.primary_identity) && (
                                                    <button
                                                        type="button"
                                                        className="primary compact"
                                                        disabled={confirmingAliases}
                                                        onClick={() =>
                                                            handleConfirmAliases([
                                                                sourceStatus?.primary_email || testResult?.primary_identity!,
                                                            ])
                                                        }
                                                    >
                                                        {confirmingAliases
                                                            ? 'Conferma in corso…'
                                                            : `✓ Conferma "${sourceStatus?.primary_email || testResult?.primary_identity}" ora`}
                                                    </button>
                                                )}
                                                <button
                                                    type="button"
                                                    className="secondary compact"
                                                    onClick={() => setCurrentStep(4)}
                                                >
                                                    ⚙️ Gestisci alias nello Step 4
                                                </button>
                                            </div>
                                            {aliasError && <p className="error" role="alert">{aliasError}</p>}
                                            {aliasSuccessMessage && <p className="success-msg" role="status">✅ {aliasSuccessMessage}</p>}
                                        </div>
                                    )}

                                    <div className="start-sync-box">
                                        <button
                                            type="button"
                                            className="primary big-action-btn"
                                            disabled={syncStarting || (selectedSourceId === 'gmail' && !aliasesReady)}
                                            onClick={handleTriggerSync}
                                            title={
                                                selectedSourceId === 'gmail' && !aliasesReady
                                                    ? 'Conferma almeno un alias prima di avviare'
                                                    : undefined
                                            }
                                        >
                                            {syncStarting ? 'Avvio job in corso…' : '🚀 Avvia Sincronizzazione Iniziale'}
                                        </button>
                                        {selectedSourceId === 'gmail' && !aliasesReady && (
                                            <p className="muted hint-disabled">
                                                ⚠️ Sincronizzazione bloccata: conferma il tuo indirizzo email o alias sopra prima di procedere.
                                            </p>
                                        )}
                                    </div>
                                </>
                            )}

                            {syncError && <p className="error" role="alert">{syncError}</p>}

                            <div className="wizard-actions">
                                <button
                                    type="button"
                                    className="secondary"
                                    onClick={() => setCurrentStep(5)}
                                    disabled={activeJob !== null && ['pending', 'running'].includes(activeJob.status)}
                                >
                                    ← Torna all'anteprima
                                </button>
                                <button
                                    type="button"
                                    className="primary"
                                    onClick={() => {
                                        onSuccess()
                                        onClose()
                                    }}
                                >
                                    {activeJob?.status === 'finished' ? 'Chiudi e torna alle Fonti' : 'Continua in background e torna alle Fonti →'}
                                </button>
                            </div>
                        </div>
                    )}
                </div>
            </div>
        </div>
    )
}

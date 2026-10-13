import { FormEvent, useEffect, useState } from 'react'
import {
    ApiError,
    GmailAccount,
    GmailCitation,
    GmailItemGap,
    GmailItemGapListResponse,
    GmailPreview,
    GmailSources,
    ImportJob,
    SourceCatalogItem,
    SourceCatalogResponse,
    SourceDisconnectResponse,
    SourceStatusResponse,
    SourceSyncConfig,
    SourceTestConnectionResponse,
    api,
    isConnectorAvailable,
} from './api'
import { SourceConnectionWizard } from './SourceConnectionWizard'

export interface SourcesPageProps {
    demoMode: boolean
}

export function SourcesPage({ demoMode }: SourcesPageProps) {
    const [catalog, setCatalog] = useState<SourceCatalogItem[]>([])
    const [gmailStatus, setGmailStatus] = useState<SourceStatusResponse | null>(null)
    const [gmailAccount, setGmailAccount] = useState<GmailAccount | null>(null)
    const [activeJob, setActiveJob] = useState<ImportJob | null>(null)
    const [sourcesData, setSourcesData] = useState<GmailSources | null>(null)
    const [searchQuery, setSearchQuery] = useState('')
    const [searching, setSearching] = useState(false)

    // Stato modali e wizard
    const [wizardOpen, setWizardOpen] = useState(false)
    const [wizardSourceId, setWizardSourceId] = useState('gmail')
    const [wizardInitialStep, setWizardInitialStep] = useState<1 | 2 | 3 | 4 | 5 | 6 | undefined>(undefined)
    const [disconnectModalOpen, setDisconnectModalOpen] = useState(false)
    const [removeStoredCredentials, setRemoveStoredCredentials] = useState(false)
    const [disconnecting, setDisconnecting] = useState(false)

    // Feedback rapido
    const [quickTestResult, setQuickTestResult] = useState<SourceTestConnectionResponse | null>(null)
    const [testingConnection, setTestingConnection] = useState(false)
    const [quickSyncBusy, setQuickSyncBusy] = useState(false)
    const [globalMessage, setGlobalMessage] = useState('')
    const [globalError, setGlobalError] = useState('')

    // Stato Pannello Persistente Gestione Gmail
    const [configPanelOpen, setConfigPanelOpen] = useState(true)
    const [editConfigMode, setEditConfigMode] = useState(false)
    const [formAfter, setFormAfter] = useState('')
    const [formBefore, setFormBefore] = useState('')
    const [formLimit, setFormLimit] = useState(10000)
    const [formBudget, setFormBudget] = useState(20000)
    const [formBatchSize, setFormBatchSize] = useState(50)
    const [savingConfig, setSavingConfig] = useState(false)
    const [historyPreview, setHistoryPreview] = useState<GmailPreview | null>(null)
    const [previewLoading, setPreviewLoading] = useState(false)
    const [historyImportLoading, setHistoryImportLoading] = useState(false)
    const [diagnosticsData, setDiagnosticsData] = useState<GmailItemGapListResponse | null>(null)
    const [diagPage, setDiagPage] = useState(1)
    const [loadingDiag, setLoadingDiag] = useState(false)

    const loadDiagnostics = async (page = 1) => {
        setLoadingDiag(true)
        try {
            const diag = await api.fetchSourceDiagnostics('gmail', page, 10)
            setDiagnosticsData(diag)
            setDiagPage(page)
        } catch {
            // diagnostica opzionale
        } finally {
            setLoadingDiag(false)
        }
    }

    async function loadAllData() {
        try {
            const [catalogRes, statusRes, sourcesRes, gmailOldStatus] = await Promise.all([
                api.fetchSourceCatalog(),
                api.fetchSourceStatus('gmail').catch(() => null),
                api.gmailSources(50).catch(() => null),
                api.gmailStatus().catch(() => null),
            ])

            if (catalogRes) setCatalog(catalogRes.items)
            if (statusRes) setGmailStatus(statusRes)
            if (sourcesRes) setSourcesData(sourcesRes)
            if (gmailOldStatus?.account) setGmailAccount(gmailOldStatus.account)
            await loadDiagnostics(1)
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore nel caricamento delle fonti.')
        }
    }

    useEffect(() => {
        loadAllData()

        // Controlla se la pagina è stata aperta/ricaricata con ritorno OAuth
        const urlParams = new URLSearchParams(window.location.search)
        const isConnected =
            urlParams.get('connected') === 'gmail' ||
            urlParams.get('gmail_connected') === '1' ||
            urlParams.get('gmail') === 'connected'
        const hasError =
            urlParams.has('error') ||
            urlParams.get('gmail') === 'oauth_error' ||
            urlParams.get('gmail') === 'consent_denied'

        if (isConnected || hasError) {
            setWizardOpen(true)
            setWizardSourceId('gmail')
        }
    }, [])

    // Polling per il job attivo
    useEffect(() => {
        if (!activeJob || !['pending', 'running'].includes(activeJob.status)) return
        const timer = window.setInterval(async () => {
            try {
                const refreshed = await api.gmailJob(activeJob.id)
                setActiveJob(refreshed)
                if (!['pending', 'running'].includes(refreshed.status)) {
                    loadAllData()
                }
            } catch {
                // Ignore transient polling failure
            }
        }, 2000)
        return () => window.clearInterval(timer)
    }, [activeJob])

    const isConnected = Boolean(gmailStatus?.account_connected || gmailStatus?.status === 'connected')
    const isConfigured = Boolean(gmailStatus?.configured || gmailStatus?.status === 'credentials_configured')
    const isError = Boolean(gmailStatus?.status === 'error' || gmailStatus?.status === 'suspended')
    const isAliasesReady = Boolean(
        gmailStatus?.aliases_ready ||
        (gmailAccount?.aliases_ready && (gmailAccount.confirmed_aliases?.length ?? 0) > 0)
    )
    const confirmedAliasesList = gmailStatus?.confirmed_aliases || gmailAccount?.confirmed_aliases || []

    // Azione: Test connessione rapido per Gmail
    async function handleQuickTestConnection() {
        setTestingConnection(true)
        setGlobalError('')
        setGlobalMessage('')
        try {
            const res = await api.testSourceConnection('gmail')
            setQuickTestResult(res)
            if (res.connected) {
                setGlobalMessage(`Connessione verificata con successo! Latenza: ${res.latency_ms?.toFixed(0) ?? '—'} ms`)
            } else {
                setGlobalError(`Test di connessione fallito: ${res.message}`)
            }
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore durante il test di connessione.')
        } finally {
            setTestingConnection(false)
        }
    }

    // Azione: Aggiorna ora (aggiornamento continuo backend-authoritative)
    async function handleQuickSync() {
        if (!isAliasesReady) {
            setGlobalError(
                "Confermare almeno un alias email del proprietario prima di avviare la sincronizzazione. Usa il pulsante 'Gestisci Alias'."
            )
            return
        }
        setQuickSyncBusy(true)
        setGlobalError('')
        setGlobalMessage('')
        try {
            const res = await api.startContinuousSync('gmail')
            setGlobalMessage(`Aggiornamento continuo (${res.mode ?? 'incremental'}) avviato (Job #${res.job_id.slice(0, 8)}).`)
            const jobData = await api.gmailJob(res.job_id)
            setActiveJob(jobData)
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore durante il lancio dell\'aggiornamento.')
        } finally {
            setQuickSyncBusy(false)
        }
    }

    // Azione: Salvataggio configurazione desiderata
    async function handleSaveConfig(e: FormEvent) {
        e.preventDefault()
        setSavingConfig(true)
        setGlobalError('')
        setGlobalMessage('')
        try {
            const payload: Partial<SourceSyncConfig> = {
                history_after: formAfter ? new Date(formAfter).toISOString() : new Date(Date.now() - 90 * 86400000).toISOString(),
                history_before: formBefore ? new Date(formBefore).toISOString() : null,
                item_limit: formLimit,
                request_budget: formBudget,
                batch_size: formBatchSize,
                selection_label: 'SENT',
                update_policy: 'continuous',
            }
            const updated = await api.updateSourceConfig('gmail', payload)
            setGlobalMessage(`Configurazione aggiornata con successo (Revisione #${updated.revision}).`)
            setEditConfigMode(false)
            await loadAllData()
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore nel salvataggio della configurazione.')
        } finally {
            setSavingConfig(false)
        }
    }

    // Azione: Anteprima intervallo storico
    async function handleHistoryPreview() {
        setPreviewLoading(true)
        setGlobalError('')
        try {
            const afterIso = formAfter ? new Date(formAfter).toISOString() : new Date(Date.now() - 90 * 86400000).toISOString()
            const beforeIso = formBefore ? new Date(formBefore).toISOString() : new Date().toISOString()
            const preview = await api.previewSourceHistory('gmail', {
                after: afterIso,
                before: beforeIso,
                item_limit: formLimit,
                request_budget: formBudget,
                batch_size: formBatchSize,
            })
            setHistoryPreview(preview)
            setGlobalMessage(`Anteprima completata: ${preview.selected_items} messaggi SENT stimati (${preview.pages_read} pagine).`)
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore durante l\'anteprima dello storico.')
        } finally {
            setPreviewLoading(false)
        }
    }

    // Azione: Avvio o ripresa importazione storico
    async function handleStartHistoryImport() {
        setHistoryImportLoading(true)
        setGlobalError('')
        setGlobalMessage('')
        try {
            const afterIso = formAfter ? new Date(formAfter).toISOString() : undefined
            const beforeIso = formBefore ? new Date(formBefore).toISOString() : undefined
            const job = await api.startSourceHistoryImport('gmail', { after: afterIso, before: beforeIso })
            setActiveJob(job)
            setGlobalMessage(`Importazione dello storico avviata (Job #${job.id.slice(0, 8)}).`)
            await loadAllData()
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore durante l\'avvio dell\'importazione storico.')
        } finally {
            setHistoryImportLoading(false)
        }
    }

    // Azione: Disconnessione sorgente
    async function handleDisconnectSource() {
        setDisconnecting(true)
        setGlobalError('')
        setGlobalMessage('')
        try {
            const res: SourceDisconnectResponse = await api.deleteSource('gmail', removeStoredCredentials)
            setGlobalMessage(
                res.remote_revoked
                    ? 'Account disconnesso, token revocato su Google e configurazione rimossa.'
                    : 'Account disconnesso localmente.',
            )
            setDisconnectModalOpen(false)
            setQuickTestResult(null)
            await loadAllData()
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore durante la disconnessione.')
        } finally {
            setDisconnecting(false)
        }
    }

    // Ricerca messaggi sincronizzati
    async function handleSearch(e: FormEvent) {
        e.preventDefault()
        setSearching(true)
        setGlobalError('')
        try {
            const trimmed = searchQuery.trim()
            const res = trimmed ? await api.gmailSearch(trimmed) : await api.gmailSources(50)
            setSourcesData(res)
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore durante la ricerca dei messaggi.')
        } finally {
            setSearching(false)
        }
    }

    // Cancellazione cooperativa job
    async function handleCancelJob() {
        if (!activeJob) return
        try {
            const cancelled = await api.gmailCancelJob(activeJob.id)
            setActiveJob(cancelled)
            setGlobalMessage('Richiesta di cancellazione inviata al worker.')
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore nella cancellazione del job.')
        }
    }

    return (
        <section className="sources-hub" aria-labelledby="sources-hub-title">
            {/* Header Hub */}
            <div className="hub-top-bar">
                <div>
                    <p className="eyebrow">Fonti & Connettori</p>
                    <h1 id="sources-hub-title">Gestione Fonti Dati</h1>
                    <p className="muted">
                        Collega e sincronizza le tue conversazioni con il grafo di conoscenza privato.
                    </p>
                </div>
                <div className="hub-header-actions">
                    <button
                        type="button"
                        className="primary-hero-btn"
                        onClick={() => {
                            setWizardSourceId('gmail')
                            setWizardOpen(true)
                        }}
                    >
                        <span aria-hidden="true">➕</span> Connetti Nuova Fonte
                    </button>
                </div>
            </div>

            {/* Messaggi globali di stato / errore */}
            {globalMessage && (
                <div className="alert-banner success" role="status">
                    <span>{globalMessage}</span>
                    <button type="button" onClick={() => setGlobalMessage('')} className="alert-close">✕</button>
                </div>
            )}
            {globalError && (
                <div className="alert-banner error" role="alert">
                    <span>{globalError}</span>
                    <button type="button" onClick={() => setGlobalError('')} className="alert-close">✕</button>
                </div>
            )}

            {/* Scheda Fonte Collegata: Gmail */}
            <div className="hub-connected-sources-section">
                <h2>Fonti Attive & Configurate</h2>

                <div className="connected-source-card">
                    <div className="connected-source-header">
                        <div className="connected-source-info">
                            <span className="source-avatar-icon" aria-hidden="true">✉️</span>
                            <div>
                                <div className="connected-source-title-row">
                                    <h3>Google Gmail (SENT Messages)</h3>
                                    <span className={`status-pill ${isConnected ? 'active' : isConfigured ? 'pending' : isError ? 'revoked' : 'not_configured'}`}>
                                        {isConnected ? '● Connesso & Attivo' : isConfigured ? 'Credenziali Configurate' : isError ? 'Errore Autorizzazione' : 'Non Configurato'}
                                    </span>
                                    {isConnected && (
                                        isAliasesReady ? (
                                            <span
                                                className="status-pill active"
                                                title={`Alias attivi: ${confirmedAliasesList.join(', ')}`}
                                            >
                                                ✓ {confirmedAliasesList.length === 1 ? '1 Alias Confermato' : `${confirmedAliasesList.length} Alias Confermati`}
                                            </span>
                                        ) : (
                                            <span
                                                className="status-pill warning"
                                                title="Conferma esplicita degli alias del proprietario obbligatoria prima dell'importazione"
                                            >
                                                ⚠️ Alias Non Confermati
                                            </span>
                                        )
                                    )}
                                    {demoMode && <span className="status-pill warning">Demo Sintetica</span>}
                                </div>
                                <p className="connected-source-subtitle">
                                    {isConnected && gmailStatus?.primary_email
                                        ? `Account autorizzato: ${gmailStatus.primary_email}`
                                        : isConfigured
                                            ? 'Credenziali client memorizzate con AES-256-GCM. In attesa di autorizzazione account.'
                                            : isError
                                                ? 'Stato account in errore o autorizzazione revocata. È necessario riautorizzare.'
                                                : 'Nessuna credenziale client configurata.'}
                                </p>
                                {isConnected && !isAliasesReady && (
                                    <div className="source-alias-warning" role="alert">
                                        <div className="warning-text">
                                            <strong>⚠️ Identità autobiografica non confermata:</strong>
                                            <span>
                                                {' '}Devi confermare almeno un alias email prima di poter sincronizzare le email inviate (SENT).
                                            </span>
                                        </div>
                                        <button
                                            type="button"
                                            className="primary compact"
                                            onClick={() => {
                                                setWizardSourceId('gmail')
                                                setWizardInitialStep(4)
                                                setWizardOpen(true)
                                            }}
                                        >
                                            Conferma alias ora
                                        </button>
                                    </div>
                                )}
                            </div>
                        </div>

                        {/* Azioni rapide */}
                        <div className="connected-source-actions">
                            {isConnected ? (
                                <>
                                    <button
                                        type="button"
                                        className="secondary compact"
                                        disabled={testingConnection}
                                        onClick={handleQuickTestConnection}
                                        title="Verifica la raggiungibilità e il token Google"
                                    >
                                        {testingConnection ? 'Test in corso…' : '🔍 Test Connessione'}
                                    </button>
                                    <button
                                        type="button"
                                        className="secondary compact"
                                        disabled={quickSyncBusy || (activeJob !== null && ['pending', 'running'].includes(activeJob.status))}
                                        onClick={handleQuickSync}
                                        title={gmailStatus?.initial_import_completed ? 'Avvia sincronizzazione incrementale' : 'Avvia sincronizzazione iniziale completa'}
                                    >
                                        {quickSyncBusy ? 'Avvio…' : '🔄 Sincronizza Ora'}
                                    </button>
                                    <button
                                        type="button"
                                        className="danger compact"
                                        onClick={() => setDisconnectModalOpen(true)}
                                        title="Disconnetti account o rimuovi credenziali"
                                    >
                                        Disconnetti
                                    </button>
                                </>
                            ) : (
                                <button
                                    type="button"
                                    className="primary compact"
                                    onClick={() => {
                                        setWizardSourceId('gmail')
                                        setWizardOpen(true)
                                    }}
                                >
                                    {isConfigured ? 'Completa Collegamento OAuth →' : 'Configura Fonte →'}
                                </button>
                            )}
                        </div>
                    </div>

                    {/* Dettagli tecnici dell'account collegato */}
                    {isConnected && (
                        <div className="connected-source-meta-grid">
                            <div className="meta-cell">
                                <span className="meta-label">ID Account GBrain</span>
                                <span className="meta-value"><code>{gmailStatus?.external_account_id?.slice(0, 12) ?? '—'}…</code></span>
                            </div>
                            <div className="meta-cell">
                                <span className="meta-label">Metodo di Autenticazione</span>
                                <span className="meta-value">OAuth 2.0 (Google PKCE / Web)</span>
                            </div>
                            <div className="meta-cell">
                                <span className="meta-label">Archiviazione Credenziali</span>
                                <span className="meta-value">{gmailStatus?.auth_source ?? 'db_encrypted'}</span>
                            </div>
                            <div className="meta-cell">
                                <span className="meta-label">Messaggi Mappati</span>
                                <span className="meta-value">{sourcesData?.total ?? 0} nel database</span>
                            </div>
                        </div>
                    )}

                    {/* Pannello Persistente Gestione Gmail: Copertura, Storico e Aggiornamento Continuo */}
                    {isConnected && (
                        <div className="gmail-sync-dashboard-panel" aria-labelledby="gmail-panel-title">
                            <div className="panel-header-row">
                                <div>
                                    <h3 id="gmail-panel-title" className="panel-title">
                                        ⚡ Gestione Persistente Fonte & Copertura
                                    </h3>
                                    <p className="panel-subtitle">
                                        Configurazione revisionata, stato backfill, copertura completata e aggiornamento continuo open-ended.
                                    </p>
                                </div>
                                <button
                                    type="button"
                                    className="secondary compact"
                                    onClick={() => setConfigPanelOpen(!configPanelOpen)}
                                    aria-expanded={configPanelOpen}
                                >
                                    {configPanelOpen ? 'Comprimi Pannello ▲' : 'Espandi Pannello ▼'}
                                </button>
                            </div>

                            {configPanelOpen && (
                                <div className="panel-body">
                                    {/* Alert se la revisione o i filtri sono cambiati e richiedono nuovo import */}
                                    {gmailStatus?.requires_import && (
                                        <div className="requires-import-alert" role="alert">
                                            <strong>⚠️ Nuova Revisione / Selezione da Importare:</strong> La configurazione desiderata è cambiata rispetto alla copertura storicizzata attuale. Avvia un'importazione dello storico per allineare la fonte. Il cambio filtro è additivo: i messaggi già importati che non corrispondono più non vengono rimossi o reindicizzati automaticamente.
                                        </div>
                                    )}

                                    {/* Schede di stato sintetico */}
                                    <div className="panel-status-strip">
                                        <div className="status-cell">
                                            <span className="cell-label">Revisione Configurazione</span>
                                            <span className="cell-value highlight">
                                                Rev. #{gmailStatus?.sync_config?.revision ?? 1}
                                            </span>
                                            <span className="cell-sub text-muted" title={gmailStatus?.sync_config?.query_fingerprint ?? ''}>
                                                FP: <code>{gmailStatus?.sync_config?.query_fingerprint ? gmailStatus.sync_config.query_fingerprint.slice(0, 10) + '…' : '—'}</code>
                                            </span>
                                        </div>

                                        <div className="status-cell">
                                            <span className="cell-label">Intervallo Desiderato</span>
                                            <span className="cell-value">
                                                {gmailStatus?.sync_config?.history_after
                                                    ? new Date(gmailStatus.sync_config.history_after).toLocaleDateString()
                                                    : 'Non impostato'}
                                                {' → '}
                                                {gmailStatus?.sync_config?.history_before
                                                    ? new Date(gmailStatus.sync_config.history_before).toLocaleDateString()
                                                    : 'Continuo (Now)'}
                                            </span>
                                            <span className="cell-sub text-muted">
                                                Policy: <strong>{gmailStatus?.sync_config?.update_policy ?? 'continuous'}</strong>
                                            </span>
                                        </div>

                                        <div className="status-cell">
                                            <span className="cell-label">Copertura Effettiva</span>
                                            <span className="cell-value">
                                                {gmailStatus?.coverage?.covered_after || gmailStatus?.coverage?.covered_before ? (
                                                    <>
                                                        {gmailStatus.coverage.covered_after ? new Date(gmailStatus.coverage.covered_after).toLocaleDateString() : '—'}
                                                        {' → '}
                                                        {gmailStatus.coverage.covered_before ? new Date(gmailStatus.coverage.covered_before).toLocaleDateString() : '—'}
                                                    </>
                                                ) : (
                                                    <span className="text-warning">Incompleta / Da importare</span>
                                                )}
                                            </span>
                                            {gmailStatus?.coverage?.status === 'complete_with_gaps' || gmailStatus?.coverage?.has_gaps ? (
                                                <span className="badge-warning">
                                                    ⚠️ Completa con Lacune ({gmailStatus?.coverage?.missing_count ?? 0} non importati)
                                                </span>
                                            ) : gmailStatus?.coverage?.is_legacy ? (
                                                <span className="badge-warning">Legacy / Non verificata</span>
                                            ) : gmailStatus?.coverage?.status === 'completed' && (
                                                <span className="badge-success">✅ Integrità Completa</span>
                                            )}
                                        </div>

                                        <div className="status-cell">
                                            <span className="cell-label">Stato Backfill & Contatori</span>
                                            <span className="cell-value">
                                                <span className={`status-tag status-${gmailStatus?.backfill_state?.status ?? 'idle'}`}>
                                                    {(gmailStatus?.backfill_state?.status ?? 'IDLE').toUpperCase()}
                                                </span>
                                            </span>
                                            <span className="cell-sub text-muted">
                                                Importati: <strong>{gmailStatus?.backfill_state?.items_imported ?? 0}</strong>
                                                {' · '}
                                                Non Importati (Lacune): <strong className={gmailStatus?.backfill_state?.items_not_imported ? 'text-warning' : ''}>{gmailStatus?.backfill_state?.items_not_imported ?? diagnosticsData?.open_count ?? 0}</strong>
                                            </span>
                                        </div>
                                    </div>

                                    {/* Griglia a due sezioni operative distinte */}
                                    <div className="panel-sections-grid">
                                        {/* SEZIONE 1: Importazione dello storico */}
                                        <div className="operation-card history-import-card">
                                            <div className="card-heading">
                                                <h4>📥 Importazione dello Storico</h4>
                                                <p className="card-desc">
                                                    Finestra passata modificabile per import iniziale o backfill retroattivo. Cursore isolato con idempotenza totale senza duplicati.
                                                </p>
                                            </div>

                                            <div className="card-form-grid">
                                                <label className="field-group">
                                                    <span>Data Inizio (After):</span>
                                                    <input
                                                        type="datetime-local"
                                                        value={formAfter}
                                                        onChange={(e) => setFormAfter(e.target.value)}
                                                    />
                                                </label>
                                                <label className="field-group">
                                                    <span>Data Fine (Before):</span>
                                                    <input
                                                        type="datetime-local"
                                                        value={formBefore}
                                                        onChange={(e) => setFormBefore(e.target.value)}
                                                        placeholder="Lascia vuoto per data odierna"
                                                    />
                                                </label>
                                            </div>

                                            <div className="card-actions-row">
                                                <button
                                                    type="button"
                                                    className="secondary compact"
                                                    disabled={previewLoading || !formAfter}
                                                    onClick={handleHistoryPreview}
                                                    title="Esegue un'anteprima sicura senza mutare checkpoint o dati"
                                                >
                                                    {previewLoading ? 'Verifica…' : '🔍 Anteprima Storico'}
                                                </button>
                                                <button
                                                    type="button"
                                                    className="primary compact"
                                                    disabled={historyImportLoading || (activeJob !== null && ['pending', 'running'].includes(activeJob.status))}
                                                    onClick={handleStartHistoryImport}
                                                    title="Avvia o riprende l'importazione mirata dell'intervallo storico"
                                                >
                                                    {historyImportLoading ? 'Avvio…' : '📥 Avvia Import / Backfill'}
                                                </button>
                                            </div>

                                            {historyPreview && (
                                                <div className="history-preview-results" aria-live="polite">
                                                    <strong>Risultato Anteprima:</strong> {historyPreview.selected_items} messaggi SENT individuati ({historyPreview.pages_read} pagine lette, {historyPreview.requests_used} richieste usate).
                                                </div>
                                            )}

                                            <div className="idempotency-callout">
                                                <span>🛡️ <strong>Idempotenza:</strong> I messaggi già catalogati non vengono duplicati. L'espansione verso il passato mantiene invariato l'history ID incrementale.</span>
                                            </div>
                                        </div>

                                        {/* SEZIONE 2: Aggiornamento della fonte (Aggiorna ora) */}
                                        <div className="operation-card continuous-sync-card">
                                            <div className="card-heading">
                                                <h4>🔄 Aggiornamento Continuo</h4>
                                                <p className="card-desc">
                                                    Acquisisce nuove email <code>SENT</code> successive al checkpoint. Nessun upper bound rigido o statico: l'orizzonte temporale viene calcolato a runtime nel worker.
                                                </p>
                                            </div>

                                            <div className="continuous-info-box">
                                                <div className="meta-pair">
                                                    <span className="muted">Ultimo Aggiornamento:</span>
                                                    <span>
                                                        {gmailStatus?.last_sync_at ? new Date(gmailStatus.last_sync_at).toLocaleString() : 'Nessuno registrato'}
                                                    </span>
                                                </div>
                                                <div className="meta-pair">
                                                    <span className="muted">History ID Checkpoint:</span>
                                                    <span><code>{gmailStatus?.checkpoint_history_id ?? 'Non impostato'}</code></span>
                                                </div>
                                                <div className="meta-pair">
                                                    <span className="muted">Filtro Perimetro:</span>
                                                    <span>Esclusivamente <strong>SENT (posta inviata)</strong></span>
                                                </div>
                                            </div>

                                            <div className="card-actions-row">
                                                <button
                                                    type="button"
                                                    className="primary compact"
                                                    disabled={quickSyncBusy || (activeJob !== null && ['pending', 'running'].includes(activeJob.status))}
                                                    onClick={handleQuickSync}
                                                    title="Lancia l'aggiornamento incrementale continuo backend-authoritative"
                                                >
                                                    {quickSyncBusy ? 'Accodamento…' : '🔄 Aggiorna Ora'}
                                                </button>
                                            </div>

                                            <p className="continuous-note text-muted">
                                                Non riutilizza la data limite dello storico. Eventuali modifiche ai filtri incrementano la revisione e preservano l'integrità del checkpoint.
                                            </p>
                                        </div>
                                    </div>

                                    {/* Modifica Parametri & Budget */}
                                    <div className="panel-config-edit-footer">
                                        {!editConfigMode ? (
                                            <button
                                                type="button"
                                                className="secondary compact"
                                                onClick={() => setEditConfigMode(true)}
                                            >
                                                ⚙️ Modifica Parametri & Budget Desiderati
                                            </button>
                                        ) : (
                                            <form onSubmit={handleSaveConfig} className="config-edit-form">
                                                <h5>Modifica Configurazione (Genera Nuova Revisione)</h5>
                                                <div className="config-fields-inline">
                                                    <label>
                                                        <span>Limite Messaggi:</span>
                                                        <input
                                                            type="number"
                                                            min={1}
                                                            max={100000}
                                                            value={formLimit}
                                                            onChange={(e) => setFormLimit(Number(e.target.value))}
                                                        />
                                                    </label>
                                                    <label>
                                                        <span>Budget Richieste:</span>
                                                        <input
                                                            type="number"
                                                            min={1}
                                                            max={200000}
                                                            value={formBudget}
                                                            onChange={(e) => setFormBudget(Number(e.target.value))}
                                                        />
                                                    </label>
                                                    <label>
                                                        <span>Dimensione Batch:</span>
                                                        <input
                                                            type="number"
                                                            min={1}
                                                            max={500}
                                                            value={formBatchSize}
                                                            onChange={(e) => setFormBatchSize(Number(e.target.value))}
                                                        />
                                                    </label>
                                                </div>
                                                <div className="form-buttons-row">
                                                    <button type="submit" className="primary compact" disabled={savingConfig}>
                                                        {savingConfig ? 'Salvataggio…' : '💾 Salva Nuova Revisione'}
                                                    </button>
                                                    <button
                                                        type="button"
                                                        className="secondary compact"
                                                        disabled={savingConfig}
                                                        onClick={() => setEditConfigMode(false)}
                                                    >
                                                        Annulla
                                                    </button>
                                                </div>
                                            </form>
                                        )}
                                    </div>
                                </div>
                            )}
                        </div>
                    )}

                    {/* Risultato rapido test connessione */}
                    {quickTestResult && (
                        <div className={`quick-test-box ${quickTestResult.connected ? 'success' : 'error'}`}>
                            <span aria-hidden="true">{quickTestResult.connected ? '✅' : '❌'}</span>
                            <span>{quickTestResult.message}</span>
                            {quickTestResult.latency_ms && (
                                <span className="badge-latency">{quickTestResult.latency_ms.toFixed(0)} ms</span>
                            )}
                        </div>
                    )}

                    {/* Avviso LAN HTTP se presente */}
                    {gmailStatus?.lan_warning && (
                        <div className="lan-warning-card">
                            <span className="warning-icon" aria-hidden="true">⚠️</span>
                            <div>
                                <strong>Vincolo Google OAuth su IP Privato HTTP rilevato:</strong>
                                <p>{gmailStatus.lan_warning.message}</p>
                            </div>
                        </div>
                    )}
                </div>
            </div>

            {/* Sezione Job Attivo o Recente */}
            {activeJob && (
                <div className="job-banner-section">
                    <div className="job-banner-header">
                        <div>
                            <strong>Job di Sincronizzazione in Esecuzione</strong>
                            <span className={`status-pill status-${activeJob.status}`}>
                                {activeJob.status.toUpperCase()}
                            </span>
                        </div>
                        {['pending', 'running'].includes(activeJob.status) && (
                            <button
                                type="button"
                                className="danger compact"
                                onClick={handleCancelJob}
                            >
                                Annulla Job
                            </button>
                        )}
                    </div>
                    <p className="job-banner-desc">
                        Modalità: <strong>{activeJob.mode ?? 'full'}</strong> · Messaggi elaborati: <strong>{activeJob.processed_items}</strong>
                        {activeJob.total_items ? ` su ${activeJob.total_items}` : ''}
                        {' · '}
                        Importati: <strong>{activeJob.imported_items ?? 0}</strong>
                        {' · '}
                        Duplicati: <strong>{activeJob.duplicate_items ?? 0}</strong>
                        {' · '}
                        Non importati: <strong className={activeJob.not_imported_items ? 'text-warning' : ''}>{activeJob.not_imported_items ?? 0}</strong>
                    </p>
                    {['pending', 'running'].includes(activeJob.status) && (
                        <div className="progress-bar-container">
                            <div className="progress-bar-indeterminate"></div>
                        </div>
                    )}
                </div>
            )}

            {/* Catalogo Connettori */}
            <div className="hub-catalog-section">
                <div className="section-header-row">
                    <div>
                        <h2>Catalogo Connettori</h2>
                        <p className="muted">Fonti dati disponibili ed integrazioni in arrivo nel sistema GBrain.</p>
                    </div>
                </div>

                <div className="hub-catalog-grid">
                    {catalog.map((item) => {
                        const isAvailable = isConnectorAvailable(item)
                        const isThisConnected = item.id === 'gmail' && isConnected
                        return (
                            <article key={item.id} className={`hub-catalog-card ${!isAvailable ? 'card-coming-soon' : ''}`}>
                                <div className="card-top-row">
                                    <span className="card-source-icon" aria-hidden="true">
                                        {item.id === 'gmail' ? '✉️' : item.icon === 'chat' ? '💬' : item.icon === 'workspace' ? '📁' : '🔗'}
                                    </span>
                                    <span className={`status-pill ${isThisConnected ? 'active' : isAvailable ? 'available' : 'coming_soon'}`}>
                                        {isThisConnected ? 'Collegato' : isAvailable ? 'Disponibile' : 'Prossimamente'}
                                    </span>
                                </div>
                                <h3 className="card-title">{item.name}</h3>
                                <div className="card-category-tag">{item.category.toUpperCase()}</div>
                                <p className="card-desc">{item.description}</p>
                                <div className="card-footer">
                                    {isAvailable ? (
                                        <button
                                            type="button"
                                            className="secondary compact"
                                            onClick={() => {
                                                setWizardSourceId(item.id)
                                                setWizardOpen(true)
                                            }}
                                        >
                                            {isThisConnected ? 'Gestisci configurazione' : 'Connetti ora →'}
                                        </button>
                                    ) : (
                                        <span className="coming-soon-label">Prossimamente</span>
                                    )}
                                </div>
                            </article>
                        )
                    })}
                </div>
            </div>

            {/* Sezione Messaggi Sincronizzati & Provenance */}
            <div className="hub-messages-section">
                <div className="messages-section-header">
                    <div>
                        <h2>Esplora Messaggi Sincronizzati</h2>
                        <p className="muted">
                            Ispezione dei metadati e citazioni con vincolo di provenance RFC-2822.
                        </p>
                    </div>
                    <form onSubmit={handleSearch} className="search-form-row">
                        <input
                            type="search"
                            placeholder="Cerca per oggetto, destinatario o snippet…"
                            value={searchQuery}
                            onChange={(e) => setSearchQuery(e.target.value)}
                        />
                        <button type="submit" className="secondary" disabled={searching}>
                            {searching ? 'Ricerca…' : 'Cerca'}
                        </button>
                    </form>
                </div>

                {sourcesData && sourcesData.items.length > 0 ? (
                    <div className="sources-citation-list">
                        {sourcesData.items.map((cit) => (
                            <div key={cit.source_item_id} className="citation-card">
                                <div className="citation-header">
                                    <span className="citation-id">ID: <code>{cit.source_item_id.slice(0, 16)}…</code></span>
                                    <span className="citation-date">
                                        {cit.occurred_at ? new Date(cit.occurred_at).toLocaleString('it-IT') : '—'}
                                    </span>
                                </div>
                                <h4 className="citation-subject">{cit.subject || '(Senza oggetto)'}</h4>
                                {cit.snippet && <p className="citation-snippet">{cit.snippet}</p>}
                                <div className="citation-footer">
                                    <span className="citation-limit-tag">{cit.source_limit}</span>
                                    <a
                                        href={cit.proof_url}
                                        target="_blank"
                                        rel="noopener noreferrer"
                                        className="citation-proof-link"
                                    >
                                        Ispeziona RFC-2822 / Prova ↗
                                    </a>
                                </div>
                            </div>
                        ))}
                    </div>
                ) : (
                    <div className="empty-sources-box">
                        <p>Nessun messaggio trovato. Avvia una sincronizzazione per popolare il grafo di conoscenza.</p>
                    </div>
                )}
            </div>

            {/* Sezione Diagnostica Lacune Gmail Per-Item */}
            {isConnected && (
                <div className="hub-catalog-section" style={{ marginTop: '2rem' }}>
                    <div className="section-header-row">
                        <div>
                            <h3 className="section-title">🔍 Diagnostica Lacune Gmail (Per-Item)</h3>
                            <p className="section-subtitle">
                                Registro consultabile, sanitizzato e bounded degli elementi non importabili (es. corpi oltre limite, MIME non conformi).
                            </p>
                        </div>
                        <button
                            type="button"
                            className="secondary compact"
                            onClick={() => loadDiagnostics(diagPage)}
                            disabled={loadingDiag}
                        >
                            {loadingDiag ? 'Aggiornamento…' : 'Aggiorna Diagnostica'}
                        </button>
                    </div>

                    {diagnosticsData && diagnosticsData.total > 0 ? (
                        <div style={{ marginTop: '1rem' }}>
                            <div style={{ marginBottom: '0.75rem', fontSize: '0.9rem' }}>
                                Lacune Totali: <strong>{diagnosticsData.total}</strong> · Aperti: <strong className="text-warning">{diagnosticsData.open_count}</strong> · Risolti: <strong>{diagnosticsData.resolved_count}</strong>
                            </div>
                            <div className="table-responsive">
                                <table className="data-table" style={{ width: '100%', fontSize: '0.85rem' }}>
                                    <thead>
                                        <tr>
                                            <th>Message ID</th>
                                            <th>Ragione Scarto</th>
                                            <th>Fase</th>
                                            <th>Rilevato il</th>
                                            <th>Stato</th>
                                            <th>Dettagli Sanitizzati</th>
                                        </tr>
                                    </thead>
                                    <tbody>
                                        {diagnosticsData.items.map((item: GmailItemGap) => (
                                            <tr key={item.id}>
                                                <td><code>{item.gmail_message_id}</code></td>
                                                <td>
                                                    <span className="badge-warning" style={{ fontSize: '0.75rem' }}>
                                                        {item.reason_code}
                                                    </span>
                                                </td>
                                                <td>{item.phase}</td>
                                                <td>{new Date(item.occurred_at).toLocaleString('it-IT')}</td>
                                                <td>
                                                    <span className={`status-pill ${item.status === 'open' ? 'status-pending' : 'status-completed'}`}>
                                                        {item.status.toUpperCase()}
                                                    </span>
                                                </td>
                                                <td>
                                                    <code style={{ fontSize: '0.75rem' }}>
                                                        {JSON.stringify(item.details)}
                                                    </code>
                                                </td>
                                            </tr>
                                        ))}
                                    </tbody>
                                </table>
                            </div>
                            <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginTop: '1rem' }}>
                                <button
                                    type="button"
                                    className="secondary compact"
                                    onClick={() => loadDiagnostics(diagPage - 1)}
                                    disabled={diagPage <= 1 || loadingDiag}
                                >
                                    ← Precedente
                                </button>
                                <span style={{ fontSize: '0.85rem' }}>
                                    Pagina {diagnosticsData.page} di {Math.max(1, Math.ceil(diagnosticsData.total / diagnosticsData.page_size))}
                                </span>
                                <button
                                    type="button"
                                    className="secondary compact"
                                    onClick={() => loadDiagnostics(diagPage + 1)}
                                    disabled={diagPage * diagnosticsData.page_size >= diagnosticsData.total || loadingDiag}
                                >
                                    Successiva →
                                </button>
                            </div>
                        </div>
                    ) : (
                        <div className="empty-state-box" style={{ padding: '1.5rem', marginTop: '1rem', textAlign: 'center' }}>
                            <p className="text-muted">Nessuna lacuna riscontrata. Tutti i messaggi analizzati sono stati importati o esclusi regolarmente.</p>
                        </div>
                    )}
                </div>
            )}

            {/* Modale Disconnessione */}
            {disconnectModalOpen && (
                <div className="wizard-modal-overlay" role="dialog" aria-modal="true">
                    <div className="disconnect-modal-card">
                        <h3>Disconnetti Google Gmail</h3>
                        <p>
                            Sei sicuro di voler disconnettere questa fonte? I token OAuth remoti verranno revocati
                            e il connettore non potrà più sincronizzare nuovi messaggi.
                        </p>
                        <label className="checkbox-option">
                            <input
                                type="checkbox"
                                checked={removeStoredCredentials}
                                onChange={(e) => setRemoveStoredCredentials(e.target.checked)}
                            />
                            <span>Elimina anche le credenziali client (Client ID e Secret) crittografate sul server</span>
                        </label>
                        <div className="wizard-actions">
                            <button
                                type="button"
                                className="secondary"
                                onClick={() => setDisconnectModalOpen(false)}
                                disabled={disconnecting}
                            >
                                Annulla
                            </button>
                            <button
                                type="button"
                                className="danger"
                                onClick={handleDisconnectSource}
                                disabled={disconnecting}
                            >
                                {disconnecting ? 'Disconnessione in corso…' : 'Conferma Disconnessione'}
                            </button>
                        </div>
                    </div>
                </div>
            )}

            {/* Wizard Modale / Full-Screen */}
            {wizardOpen && (
                <SourceConnectionWizard
                    catalogItems={catalog}
                    initialSourceId={wizardSourceId}
                    initialStep={wizardInitialStep}
                    onClose={() => setWizardOpen(false)}
                    onSuccess={() => {
                        setWizardOpen(false)
                        loadAllData()
                    }}
                />
            )}
        </section>
    )
}

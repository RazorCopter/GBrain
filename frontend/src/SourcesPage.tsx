import { FormEvent, useEffect, useState } from 'react'
import {
    ApiError,
    GmailAccount,
    GmailCitation,
    GmailSources,
    ImportJob,
    SourceCatalogItem,
    SourceCatalogResponse,
    SourceDisconnectResponse,
    SourceStatusResponse,
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
    const [disconnectModalOpen, setDisconnectModalOpen] = useState(false)
    const [removeStoredCredentials, setRemoveStoredCredentials] = useState(false)
    const [disconnecting, setDisconnecting] = useState(false)

    // Feedback rapido
    const [quickTestResult, setQuickTestResult] = useState<SourceTestConnectionResponse | null>(null)
    const [testingConnection, setTestingConnection] = useState(false)
    const [quickSyncBusy, setQuickSyncBusy] = useState(false)
    const [globalMessage, setGlobalMessage] = useState('')
    const [globalError, setGlobalError] = useState('')

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

    // Azione: Sincronizza ora (incrementale o full rapido)
    async function handleQuickSync() {
        setQuickSyncBusy(true)
        setGlobalError('')
        setGlobalMessage('')
        try {
            const res = await api.triggerSourceSync('gmail', {
                mode: 'incremental',
                item_limit: 500,
                request_budget: 200,
                batch_size: 25,
            })
            setGlobalMessage(`Sincronizzazione avviata (Job #${res.job_id.slice(0, 8)}).`)
            const jobData = await api.gmailJob(res.job_id)
            setActiveJob(jobData)
        } catch (err) {
            setGlobalError(err instanceof ApiError ? err.message : 'Errore durante il lancio della sincronizzazione.')
        } finally {
            setQuickSyncBusy(false)
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

    const isConnected = Boolean(gmailStatus?.account_connected || gmailStatus?.status === 'connected')
    const isConfigured = Boolean(gmailStatus?.configured || gmailStatus?.status === 'credentials_configured')
    const isError = Boolean(gmailStatus?.status === 'error' || gmailStatus?.status === 'suspended')

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
                                        title="Avvia sincronizzazione incrementale"
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

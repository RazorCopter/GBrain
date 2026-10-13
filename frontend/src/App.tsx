import { FormEvent, useEffect, useState } from 'react'
import {
    ApiError,
    DemoFixtureStatus,
    ImportJobs,
    PublicConfig,
    SessionState,
    StatusState,
    api,
} from './api'
import { GmailSourcesPage } from './GmailSourcesPage'

type View = 'dashboard' | 'sources' | 'settings'

const emptySession: SessionState = {
    authenticated: false,
    bootstrap_required: false,
    owner: null,
    csrf_token: null,
}

export function App() {
    const [session, setSession] = useState<SessionState>(emptySession)
    const [config, setConfig] = useState<PublicConfig | null>(null)
    const [loading, setLoading] = useState(true)
    const [error, setError] = useState('')

    useEffect(() => {
        Promise.all([api.config(), api.session()])
            .then(([nextConfig, nextSession]) => {
                setConfig(nextConfig)
                setSession(nextSession)
            })
            .catch((reason: unknown) => setError(errorMessage(reason)))
            .finally(() => setLoading(false))
    }, [])

    if (loading) return <main className="centered"><p>Caricamento sicuro…</p></main>

    return (
        <>
            {config?.demo_mode && (
                <div className="demo-banner" role="status">
                    MODALITÀ DEMO SINTETICA — autenticazione e CSRF restano attivi; nessun servizio reale è collegato
                </div>
            )}
            {session.authenticated && session.owner ? (
                <PrivateShell
                    config={config}
                    session={session}
                    onLogout={() => setSession(emptySession)}
                />
            ) : (
                <AuthPage
                    bootstrapRequired={session.bootstrap_required}
                    globalError={error}
                    onAuthenticated={setSession}
                />
            )}
        </>
    )
}

function AuthPage({
    bootstrapRequired,
    globalError,
    onAuthenticated,
}: {
    bootstrapRequired: boolean
    globalError: string
    onAuthenticated: (session: SessionState) => void
}) {
    const [username, setUsername] = useState('')
    const [displayName, setDisplayName] = useState('')
    const [password, setPassword] = useState('')
    const [error, setError] = useState(globalError)
    const [pending, setPending] = useState(false)

    async function submit(event: FormEvent<HTMLFormElement>) {
        event.preventDefault()
        setPending(true)
        setError('')
        try {
            const nextSession = bootstrapRequired
                ? await api.bootstrap({ username, display_name: displayName, password })
                : await api.login({ username, password })
            setPassword('')
            onAuthenticated(nextSession)
        } catch (reason: unknown) {
            setError(errorMessage(reason))
        } finally {
            setPending(false)
        }
    }

    return (
        <main className="centered auth-page">
            <section className="auth-card" aria-labelledby="auth-title">
                <p className="eyebrow">GBrain · archivio privato</p>
                <h1 id="auth-title">{bootstrapRequired ? 'Crea il proprietario' : 'Accedi'}</h1>
                <p className="muted">
                    {bootstrapRequired
                        ? 'Configurazione iniziale unica. Non esiste una password predefinita.'
                        : 'La sessione resta in un cookie protetto e non nel browser storage.'}
                </p>
                <form onSubmit={submit}>
                    {bootstrapRequired && (
                        <label>
                            Nome visualizzato
                            <input
                                autoComplete="name"
                                maxLength={160}
                                required
                                value={displayName}
                                onChange={(event) => setDisplayName(event.target.value)}
                            />
                        </label>
                    )}
                    <label>
                        Nome utente
                        <input
                            autoCapitalize="none"
                            autoComplete="username"
                            maxLength={80}
                            required
                            value={username}
                            onChange={(event) => setUsername(event.target.value)}
                        />
                    </label>
                    <label>
                        Password
                        <input
                            autoComplete={bootstrapRequired ? 'new-password' : 'current-password'}
                            minLength={bootstrapRequired ? 14 : 1}
                            maxLength={256}
                            required
                            type="password"
                            value={password}
                            onChange={(event) => setPassword(event.target.value)}
                        />
                    </label>
                    {error && <p className="error" role="alert">{error}</p>}
                    <button disabled={pending} type="submit">
                        {pending ? 'Attendi…' : bootstrapRequired ? 'Crea e accedi' : 'Accedi'}
                    </button>
                </form>
            </section>
        </main>
    )
}

function PrivateShell({
    config,
    session,
    onLogout,
}: {
    config: PublicConfig | null
    session: SessionState
    onLogout: () => void
}) {
    const [view, setView] = useState<View>(() => {
        const pathname = window.location.pathname
        const search = new URLSearchParams(window.location.search)
        if (
            pathname.startsWith('/sources') ||
            search.get('view') === 'sources' ||
            search.has('connected') ||
            search.has('gmail_connected') ||
            search.has('gmail')
        ) {
            return 'sources'
        }
        if (pathname.startsWith('/settings') || search.get('view') === 'settings') {
            return 'settings'
        }
        return 'dashboard'
    })
    const [statusState, setStatusState] = useState<StatusState | null>(null)
    const [imports, setImports] = useState<ImportJobs | null>(null)
    const [demoStatus, setDemoStatus] = useState<DemoFixtureStatus | null>(null)
    const [error, setError] = useState('')

    useEffect(() => {
        Promise.all([api.status(), api.imports(), api.demoStatus()])
            .then(([nextStatus, nextImports, nextDemoStatus]) => {
                setStatusState(nextStatus)
                setImports(nextImports)
                setDemoStatus(nextDemoStatus)
            })
            .catch((reason: unknown) => setError(errorMessage(reason)))
    }, [])

    async function signOut() {
        try {
            await api.logout()
            onLogout()
        } catch (reason: unknown) {
            setError(errorMessage(reason))
        }
    }

    return (
        <div className="app-shell">
            <header className="topbar">
                <div>
                    <strong>GBrain</strong>
                    <span className="muted"> Second brain privato</span>
                </div>
                <div className="owner-actions">
                    <span>{session.owner?.display_name}</span>
                    <button className="secondary compact" onClick={signOut} type="button">Esci</button>
                </div>
            </header>
            <nav aria-label="Navigazione principale">
                <button aria-current={view === 'dashboard' ? 'page' : undefined} onClick={() => {
                    setView('dashboard')
                    if (window.location.pathname !== '/') {
                        window.history.pushState({}, '', '/')
                    }
                }}>Stato</button>
                <button aria-current={view === 'sources' ? 'page' : undefined} onClick={() => {
                    setView('sources')
                    if (window.location.pathname !== '/sources') {
                        window.history.pushState({}, '', '/sources')
                    }
                }}>Fonti</button>
                <button aria-current={view === 'settings' ? 'page' : undefined} onClick={() => {
                    setView('settings')
                    if (window.location.pathname !== '/settings') {
                        window.history.pushState({}, '', '/settings')
                    }
                }}>Impostazioni</button>
            </nav>
            <main className="content">
                {error && <p className="error" role="alert">{error}</p>}
                {config?.demo_mode && (
                    <DemoStatusPanel status={demoStatus} />
                )}
                {view === 'dashboard' && <Dashboard status={statusState} imports={imports} />}
                {view === 'sources' && <GmailSourcesPage demoMode={Boolean(config?.demo_mode)} />}
                {view === 'settings' && <Settings config={config} session={session} />}
            </main>
        </div>
    )
}

function DemoStatusPanel({ status }: { status: DemoFixtureStatus | null }) {
    return (
        <section className="demo-status" aria-labelledby="demo-status-title">
            <div>
                <p className="eyebrow">Ambiente isolato</p>
                <h2 id="demo-status-title">Dataset demo sintetico</h2>
                <p>
                    {status?.seeded
                        ? 'Fixture caricate e complete nel namespace riservato.'
                        : 'Modalità attiva; fixture non ancora caricate o incomplete.'}
                </p>
            </div>
            <dl className="demo-counts">
                <div><dt>Elementi</dt><dd>{status ? `${status.item_count}/${status.expected_item_count}` : '…'}</dd></div>
                <div><dt>Revisioni</dt><dd>{status ? `${status.revision_count}/${status.expected_revision_count}` : '…'}</dd></div>
                <div><dt>Import job</dt><dd>{status?.import_job_count ?? '…'}</dd></div>
            </dl>
            <p className="demo-safety">
                Solo osservazioni fittizie marcate. Gmail, Immich, OmniRoute e altri servizi reali non sono collegati.
            </p>
        </section>
    )
}

function Dashboard({ status, imports }: { status: StatusState | null; imports: ImportJobs | null }) {
    return (
        <section aria-labelledby="dashboard-title">
            <p className="eyebrow">Fondazione Fase 1</p>
            <h1 id="dashboard-title">Stato del sistema</h1>
            <div className="cards">
                <StatusCard title="PostgreSQL" state={status?.postgres} />
                <StatusCard title="Proiezione Neo4j" state={status?.neo4j_projection} />
                <StatusCard title="Graphiti" state={status?.graphiti} />
                <article className="card"><h2>Importazioni</h2><strong>{imports?.total ?? '—'}</strong><p>Job persistenti registrati</p></article>
            </div>
            <p className="notice">PostgreSQL è l’autorità del dato. Neo4j è solo una proiezione ricostruibile.</p>
        </section>
    )
}

function StatusCard({ title, state }: { title: string; state?: { status: string; detail: string } }) {
    return (
        <article className="card">
            <h2>{title}</h2>
            <span className={`status status-${state?.status ?? 'loading'}`}>{state?.status ?? 'verifica'}</span>
            <p>{state?.detail ?? 'Verifica in corso…'}</p>
        </article>
    )
}

function Settings({ config, session }: { config: PublicConfig | null; session: SessionState }) {
    return (
        <section aria-labelledby="settings-title">
            <p className="eyebrow">Configurazione non sensibile</p>
            <h1 id="settings-title">Impostazioni di base</h1>
            <dl className="settings-list">
                <div><dt>Proprietario</dt><dd>{session.owner?.username}</dd></div>
                <div><dt>Lingua</dt><dd>Italiano</dd></div>
                <div><dt>Ambiente</dt><dd>{config?.environment ?? '—'}</dd></div>
                <div><dt>Durata sessione</dt><dd>{config ? `${Math.round(config.session_ttl_seconds / 3600)} ore` : '—'}</dd></div>
                <div><dt>Modalità demo</dt><dd>{config?.demo_mode ? 'Attiva' : 'Disattiva'}</dd></div>
            </dl>
        </section>
    )
}

function errorMessage(reason: unknown): string {
    if (reason instanceof ApiError || reason instanceof Error) return reason.message
    return 'Errore inatteso'
}

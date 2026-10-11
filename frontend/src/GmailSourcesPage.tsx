import { FormEvent, useEffect, useState } from 'react'
import {
    ApiError,
    GmailPolicy,
    GmailPreview,
    GmailSavedPolicy,
    GmailSources,
    GmailStatus,
    ImportJob,
    api,
} from './api'

const initialPolicy: GmailPolicy = {
    after: new Date(Date.now() - 30 * 24 * 60 * 60 * 1000).toISOString(),
    before: new Date().toISOString(),
    item_limit: 500,
    request_budget: 1500,
    batch_size: 25,
}

export function GmailSourcesPage({ demoMode }: { demoMode: boolean }) {
    const [gmail, setGmail] = useState<GmailStatus | null>(null)
    const [policy, setPolicy] = useState<GmailPolicy>(initialPolicy)
    const [savedPolicy, setSavedPolicy] = useState<GmailSavedPolicy | null>(null)
    const [preview, setPreview] = useState<GmailPreview | null>(null)
    const [job, setJob] = useState<ImportJob | null>(null)
    const [sources, setSources] = useState<GmailSources | null>(null)
    const [search, setSearch] = useState('')
    const [busy, setBusy] = useState(false)
    const [message, setMessage] = useState('')
    const [error, setError] = useState('')

    async function refresh() {
        const [nextStatus, nextSources] = await Promise.all([api.gmailStatus(), api.gmailSources()])
        setGmail(nextStatus)
        setSources(nextSources)
    }

    useEffect(() => {
        refresh().catch((reason: unknown) => setError(errorMessage(reason)))
    }, [])

    useEffect(() => {
        if (!job || !['pending', 'running'].includes(job.status)) return
        const timer = window.setInterval(() => {
            api.gmailJob(job.id).then((next) => {
                setJob(next)
                if (!['pending', 'running'].includes(next.status)) {
                    window.clearInterval(timer)
                    refresh().catch(() => undefined)
                }
            }).catch((reason: unknown) => setError(errorMessage(reason)))
        }, 2000)
        return () => window.clearInterval(timer)
    }, [job?.id, job?.status])

    async function run(action: () => Promise<void>) {
        setBusy(true)
        setError('')
        setMessage('')
        try {
            await action()
        } catch (reason: unknown) {
            setError(errorMessage(reason))
        } finally {
            setBusy(false)
        }
    }

    const connect = () => run(async () => {
        const result = await api.gmailOAuthStart()
        window.location.assign(result.authorization_url)
    })

    const disconnect = () => run(async () => {
        const result = await api.gmailDisconnect()
        setMessage(result.remote_revoked
            ? 'Consenso Google revocato e connessione locale rimossa.'
            : 'Connessione locale rimossa; la revoca remota non è stata confermata.')
        setPreview(null)
        setSavedPolicy(null)
        await refresh()
    })

    const confirmPrimaryAlias = () => run(async () => {
        if (!gmail?.account) return
        const account = await api.gmailConfirmAliases([gmail.account.primary_email])
        setGmail({ ...gmail, account })
        setMessage('Alias ottenuto dal profilo Gmail e confermato esplicitamente.')
    })

    const previewPolicy = (event: FormEvent) => {
        event.preventDefault()
        return run(async () => {
            const saved = await api.gmailSavePolicy(policy)
            const queued = await api.gmailPreview(policy)
            setSavedPolicy(saved)
            setPreview(queued)
            setJob(await waitForJob(queued.job_id))
            const completed = await api.gmailJob(queued.job_id)
            setPreview(previewFromJob(completed, queued.policy))
            setMessage('Preview completata dalla coda: messaggi completi non persistiti e checkpoint invariato.')
        })
    }

    const startImport = (mode: 'full' | 'incremental' | 'reconciliation') => run(async () => {
        if (!savedPolicy || !preview) return
        setJob(await api.gmailStartJob(savedPolicy.id, mode, preview.policy))
        setMessage('Lavoro persistente accodato senza token OAuth nel payload.')
    })

    const cancelJob = () => run(async () => {
        if (!job) return
        setJob(await api.gmailCancelJob(job.id))
        setMessage('Cancellazione cooperativa richiesta e resa visibile al worker.')
    })

    const submitSearch = (event: FormEvent) => {
        event.preventDefault()
        return run(async () => setSources(await api.gmailSearch(search)))
    }

    return (
        <section aria-labelledby="sources-title">
            <p className="eyebrow">Fonti · Gmail</p>
            <h1 id="sources-title">Posta inviata</h1>
            {demoMode && <p className="notice warning">Fixture/test sintetici attivi: non equivalgono a una connessione Gmail reale.</p>}
            {error && <p className="error" role="alert">{error}</p>}
            {message && <p className="notice" role="status">{message}</p>}

            <article className="card wide-card">
                <div className="row-between">
                    <div><h2>Autorizzazione Google</h2><p className="muted">Scope esatto: <code>gmail.readonly</code></p></div>
                    <StatusPill value={!gmail?.configured ? 'non configurato' : gmail.connected ? 'connesso' : gmail.account?.status ?? 'non connesso'} />
                </div>
                <p>{gmail?.scope_notice ?? 'Lettura dello stato…'}</p>
                <p className="notice warning">Il consenso copre tecnicamente l’intera casella, ma il wrapper server-side acquisisce esclusivamente <strong>SENT</strong>. Invio, modifica e bozze non sono disponibili.</p>
                {!gmail?.configured ? (
                    <p><strong>Configurazione richiesta:</strong> {gmail?.configuration_action ?? 'Montare i secret file OAuth.'}</p>
                ) : !gmail.connected ? (
                    <button disabled={busy} onClick={connect} type="button">Collega Gmail</button>
                ) : (
                    <div className="button-row"><span>{gmail.account?.primary_email}</span><button className="danger" disabled={busy} onClick={disconnect} type="button">Revoca e disconnetti</button></div>
                )}
            </article>

            {gmail?.connected && gmail.account && (
                <article className="card">
                    <h2>Alias proprietario</h2>
                    <p className="muted">Usato per attribuzione solo dopo conferma esplicita; mai dedotto dal campo From.</p>
                    {gmail.account.confirmed_aliases.length ? <ul>{gmail.account.confirmed_aliases.map((alias) => <li key={alias}><code>{alias}</code></li>)}</ul> : <p>Nessun alias confermato: importazione bloccata.</p>}
                    {!gmail.account.aliases_ready && <button disabled={busy} onClick={confirmPrimaryAlias} type="button">Conferma {gmail.account.primary_email}</button>}
                </article>
            )}

            {gmail?.connected && gmail.account?.aliases_ready && <>
                <article className="card policy-card">
                    <h2>1. Selezione e preview</h2>
                    <form onSubmit={previewPolicy}>
                        <div className="form-grid">
                            <label>Dal<input type="datetime-local" value={toLocalInput(policy.after)} onChange={(event) => setPolicy({ ...policy, after: fromLocalInput(event.target.value) })} required /></label>
                            <label>Al<input type="datetime-local" value={toLocalInput(policy.before)} onChange={(event) => setPolicy({ ...policy, before: fromLocalInput(event.target.value) })} required /></label>
                            <label>Limite messaggi<input min={1} max={100000} type="number" value={policy.item_limit} onChange={(event) => setPolicy({ ...policy, item_limit: Number(event.target.value) })} required /></label>
                            <label>Budget richieste<input min={1} max={200000} type="number" value={policy.request_budget} onChange={(event) => setPolicy({ ...policy, request_budget: Number(event.target.value) })} required /></label>
                        </div>
                        <button disabled={busy} type="submit">Calcola preview SENT</button>
                    </form>
                    {preview && <div className="preview-box"><strong>{preview.selected_items} messaggi</strong><span>{preview.pages_read} pagine · {preview.requests_used} richieste</span><span>{String(preview.policy.after)} → {String(preview.policy.before)}</span><span>{preview.truncated ? 'Troncata da limite/budget' : 'Copertura completa'}</span><span>Persistiti: 0 · checkpoint avanzato: no</span></div>}
                </article>
                {preview && savedPolicy && <article className="card"><h2>2. Conferma importazione</h2><p>La conferma separata usa esattamente la policy mostrata.</p><div className="button-row"><button disabled={busy} onClick={() => startImport('full')} type="button">Conferma import completo</button><button className="secondary" disabled={busy} onClick={() => startImport('incremental')} type="button">Incrementale</button><button className="secondary" disabled={busy} onClick={() => startImport('reconciliation')} type="button">Riconcilia</button></div></article>}
                {job && <article className="card"><h2>Progresso e ripresa</h2><p><StatusPill value={job.status} /> · {job.processed_items} elementi · retry {job.retry_count}</p><p className="muted">Checkpoint: {String(job.checkpoint.phase ?? 'in attesa')}</p>{job.error_detail && <p className="error">{job.error_code}: {job.error_detail}</p>}{job.cancel_requested_at && <p className="notice">Cancellazione richiesta.</p>}{['pending', 'running'].includes(job.status) && !job.cancel_requested_at && <button className="danger" onClick={cancelJob} type="button">Richiedi cancellazione</button>}</article>}
            </>}

            <article className="card source-list-card">
                <div className="row-between"><div><h2>Fonti importate</h2><p className="muted">Citazioni verificabili · solo posta inviata</p></div><form className="search-form" onSubmit={submitSearch}><input aria-label="Ricerca fonti Gmail" minLength={2} maxLength={200} placeholder="Cerca testo importato" value={search} onChange={(event) => setSearch(event.target.value)} /><button disabled={busy || search.trim().length < 2} type="submit">Cerca</button></form></div>
                {!sources?.items.length ? <p>Nessuna fonte Gmail importata.</p> : <div className="source-list">{sources.items.map((source) => <article className="source-item" key={source.source_revision_id}><div><strong>{source.subject ?? '(senza oggetto)'}</strong><p>{source.snippet ?? 'Nessuna anteprima'}</p></div><div className="citation"><code>SourceItem {source.source_item_id}</code><code>SourceRevision {source.source_revision_id}</code><a href={source.proof_url} rel="noreferrer" target="_blank">Apri prova Gmail SENT</a></div></article>)}</div>}
            </article>
        </section>
    )
}

async function waitForJob(id: string): Promise<ImportJob> {
    for (let attempt = 0; attempt < 150; attempt += 1) {
        const current = await api.gmailJob(id)
        if (!['pending', 'running'].includes(current.status)) return current
        await new Promise((resolve) => window.setTimeout(resolve, 1000))
    }
    throw new Error('Timeout della preview Gmail; il job resta consultabile e riprendibile.')
}

function previewFromJob(job: ImportJob, policy: Record<string, string | number>): GmailPreview {
    if (job.status !== 'completed') throw new Error(job.error_detail ?? 'Preview Gmail non riuscita')
    return {
        job_id: job.id,
        selected_items: Number(job.checkpoint.selected_items ?? job.processed_items),
        pages_read: Number(job.checkpoint.pages_read ?? 0),
        requests_used: Number(job.checkpoint.requests_used ?? 0),
        state: String(job.checkpoint.state ?? 'complete'),
        truncated: Boolean(job.checkpoint.truncated),
        policy,
        persisted_messages: 0,
        checkpoint_advanced: false,
    }
}

function StatusPill({ value }: { value: string }) {
    return <span className={`status-pill status-${value.replaceAll('_', '-')}`}>{value}</span>
}

function toLocalInput(value: string): string {
    const date = new Date(value)
    return new Date(date.getTime() - date.getTimezoneOffset() * 60000).toISOString().slice(0, 16)
}

function fromLocalInput(value: string): string {
    return new Date(value).toISOString()
}

function errorMessage(reason: unknown): string {
    if (reason instanceof ApiError || reason instanceof Error) return reason.message
    return 'Errore inatteso'
}

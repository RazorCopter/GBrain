import { FormEvent, useEffect, useMemo, useState } from 'react'
import { ApiError, ImportJob, ImmichAssetList, SourceStatusResponse, api } from './api'

interface ImmichConfigPanelProps {
    onClose: () => void
}

const terminal = new Set(['completed', 'completed_with_warnings', 'failed', 'cancelled', 'superseded'])

function localInput(date: Date): string {
    const offset = date.getTimezoneOffset() * 60_000
    return new Date(date.getTime() - offset).toISOString().slice(0, 16)
}

export function ImmichConfigPanel({ onClose }: ImmichConfigPanelProps) {
    const [status, setStatus] = useState<SourceStatusResponse | null>(null)
    const [assets, setAssets] = useState<ImmichAssetList | null>(null)
    const [selection, setSelection] = useState<'all' | 'range'>('range')
    const [after, setAfter] = useState(() => localInput(new Date(Date.now() - 30 * 86400000)))
    const [before, setBefore] = useState(() => localInput(new Date()))
    const [itemLimit, setItemLimit] = useState(5000)
    const [requestBudget, setRequestBudget] = useState(200)
    const [loading, setLoading] = useState(true)
    const [starting, setStarting] = useState(false)
    const [cancelling, setCancelling] = useState(false)
    const [error, setError] = useState('')

    const job: ImportJob | null = status?.active_job ?? status?.latest_job ?? null
    const running = Boolean(status?.active_job && !terminal.has(status.active_job.status))
    const checkpoint = job?.checkpoint ?? {}
    const processed = Number(checkpoint.total_enumerated ?? job?.processed_items ?? 0)
    const limit = Number(checkpoint.item_limit ?? itemLimit)
    const percent = Math.min(100, limit > 0 ? Math.round((processed / limit) * 100) : 0)

    async function refresh() {
        try {
            const [nextStatus, nextAssets] = await Promise.all([
                api.fetchSourceStatus('immich'),
                api.fetchSourceItems('immich', 25),
            ])
            setStatus(nextStatus)
            setAssets(nextAssets)
            setError('')
        } catch (err) {
            setError(err instanceof ApiError ? err.message : 'Impossibile caricare il pannello Immich.')
        } finally {
            setLoading(false)
        }
    }

    useEffect(() => {
        void refresh()
    }, [])

    useEffect(() => {
        if (!running) return
        const timer = window.setInterval(() => void refresh(), 2000)
        return () => window.clearInterval(timer)
    }, [running])

    async function startImport(event: FormEvent) {
        event.preventDefault()
        setStarting(true)
        setError('')
        try {
            await api.triggerSourceSync('immich', {
                selection,
                period_after: selection === 'range' ? new Date(after).toISOString() : null,
                period_before: selection === 'range' ? new Date(before).toISOString() : null,
                item_limit: itemLimit,
                request_budget: requestBudget,
                batch_size: 25,
                concurrency: 1,
                mode: 'full',
            })
            await refresh()
        } catch (err) {
            setError(err instanceof ApiError ? err.message : 'Avvio import Immich non riuscito.')
        } finally {
            setStarting(false)
        }
    }

    async function cancelImport() {
        if (!job) return
        setCancelling(true)
        try {
            await api.cancelSourceJob('immich', job.id)
            await refresh()
        } catch (err) {
            setError(err instanceof ApiError ? err.message : 'Cancellazione non riuscita.')
        } finally {
            setCancelling(false)
        }
    }

    const stateLabel = useMemo(() => {
        if (!job) return 'Nessuna importazione eseguita'
        if (running) return 'Importazione in corso'
        if (job.status === 'completed') return 'Importazione completata'
        if (job.status === 'completed_with_warnings') return 'Importazione parziale'
        if (job.status === 'cancelled') return 'Importazione annullata'
        if (job.status === 'failed') return 'Importazione non riuscita'
        return job.status
    }, [job, running])

    return (
        <section className="immich-panel" aria-labelledby="immich-panel-title">
            <header className="immich-panel-hero">
                <div>
                    <p className="eyebrow">Immich · Metadata only</p>
                    <h2 id="immich-panel-title">Importazione libreria</h2>
                    <p>Gli originali restano in Immich. GBrain legge solo metadati, senza vision o ML.</p>
                </div>
                <button type="button" className="secondary compact" onClick={onClose}>Chiudi</button>
            </header>

            {error && <div className="alert error">{error}</div>}
            {loading ? <div className="empty-state">Caricamento configurazione…</div> : (
                <div className="immich-panel-grid">
                    <form className="immich-config-card" onSubmit={startImport}>
                        <h3>Nuova importazione</h3>
                        <div className="selection-toggle">
                            <button type="button" className={selection === 'range' ? 'active' : ''} onClick={() => setSelection('range')}>Intervallo date</button>
                            <button type="button" className={selection === 'all' ? 'active' : ''} onClick={() => setSelection('all')}>Intera libreria</button>
                        </div>
                        {selection === 'range' && (
                            <div className="immich-date-grid">
                                <label>Dal<input type="datetime-local" value={after} onChange={(e) => setAfter(e.target.value)} required /></label>
                                <label>Al<input type="datetime-local" value={before} onChange={(e) => setBefore(e.target.value)} required /></label>
                            </div>
                        )}
                        <div className="immich-date-grid">
                            <label>Limite totale asset<input type="number" min={1} max={50000} value={itemLimit} onChange={(e) => setItemLimit(Number(e.target.value))} /></label>
                            <label>Budget richieste<input type="number" min={1} max={2000} value={requestBudget} onChange={(e) => setRequestBudget(Number(e.target.value))} /></label>
                        </div>
                        <p className="muted">Le API remote vengono sempre interrogate a blocchi di 25 asset.</p>
                        <button className="primary" disabled={running || starting}>{starting ? 'Avvio…' : 'Avvia importazione'}</button>
                    </form>

                    <article className={`immich-progress-card ${running ? 'is-running' : ''}`}>
                        <div className="card-top-row"><h3>Progresso</h3><span className="status-pill active">{stateLabel}</span></div>
                        <strong className="immich-progress-value">{processed.toLocaleString('it-IT')}</strong>
                        <span className="muted">asset elaborati su un massimo di {limit.toLocaleString('it-IT')}</span>
                        <div className="immich-progress-track" role="progressbar" aria-valuenow={percent} aria-valuemin={0} aria-valuemax={100}><span style={{ width: `${percent}%` }} /></div>
                        <div className="immich-metrics">
                            <span><strong>{Number(checkpoint.total_inserted ?? job?.imported_items ?? 0)}</strong> importati</span>
                            <span><strong>{Number(checkpoint.total_duplicates ?? job?.duplicate_items ?? 0)}</strong> già presenti</span>
                            <span><strong>{Number(checkpoint.pages_completed ?? 0)}</strong> batch</span>
                        </div>
                        {job?.error_detail && <p className="alert warning">{job.error_detail}</p>}
                        {running && <button type="button" className="danger" disabled={cancelling} onClick={cancelImport}>{cancelling ? 'Annullamento…' : 'Annulla tra i batch'}</button>}
                    </article>
                </div>
            )}

            <article className="immich-assets-card">
                <div className="section-header-row"><div><h3>Asset importati</h3><p className="muted">Ultimi metadati persistiti · {assets?.total ?? 0} totali</p></div><button type="button" className="secondary compact" onClick={() => void refresh()}>Aggiorna</button></div>
                {!assets?.items.length ? <div className="empty-state">Nessun asset importato.</div> : (
                    <div className="immich-assets-list">{assets.items.map((asset) => <div key={asset.source_item_id} className="immich-asset-row"><span aria-hidden="true">{asset.asset_type === 'VIDEO' ? '🎬' : '🖼️'}</span><div><strong>{asset.description || 'Asset senza descrizione'}</strong><small>{asset.occurred_at ? new Date(asset.occurred_at).toLocaleString('it-IT') : 'Data non disponibile'}</small></div></div>)}</div>
                )}
            </article>
        </section>
    )
}

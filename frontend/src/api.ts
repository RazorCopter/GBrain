export interface Owner {
    id: string
    username: string
    display_name: string
}

export interface SessionState {
    authenticated: boolean
    bootstrap_required: boolean
    owner: Owner | null
    csrf_token: string | null
}

export interface PublicConfig {
    app_name: string
    language: string
    demo_mode: boolean
    environment: string
    session_ttl_seconds: number
    implemented_sources: string[]
}

export interface ServiceStatus {
    status: 'ok' | 'degraded' | 'unavailable' | 'not_implemented'
    detail: string
}

export interface StatusState {
    overall: 'ok' | 'degraded'
    postgres: ServiceStatus
    neo4j_projection: ServiceStatus
    graphiti: ServiceStatus
    demo_mode: boolean
}

export interface ImportJob {
    id: string
    source_kind: string
    status: string
    processed_items: number
    error_code: string | null
    created_at: string
    updated_at: string
    started_at: string | null
    finished_at: string | null
}

export interface ImportJobs {
    items: ImportJob[]
    total: number
}

export interface DemoFixtureStatus {
    enabled: boolean
    seeded: boolean
    dataset: string
    marker: string
    item_count: number
    revision_count: number
    import_job_count: number
    expected_item_count: number
    expected_revision_count: number
    cases: string[]
    security_controls_unchanged: boolean
    real_integrations: string[]
}

interface ApiErrorBody {
    detail?: string
}

export class ApiError extends Error {
    constructor(
        public readonly status: number,
        message: string,
    ) {
        super(message)
    }
}

function csrfFromCookie(): string | null {
    const match = document.cookie
        .split('; ')
        .find((entry) => entry.startsWith('gbrain_csrf='))
    return match ? decodeURIComponent(match.slice('gbrain_csrf='.length)) : null
}

async function request<T>(path: string, init: RequestInit = {}): Promise<T> {
    const headers = new Headers(init.headers)
    if (init.body) headers.set('Content-Type', 'application/json')
    if (init.method && !['GET', 'HEAD'].includes(init.method.toUpperCase())) {
        const csrf = csrfFromCookie()
        if (csrf) headers.set('X-CSRF-Token', csrf)
    }
    const response = await fetch(path, {
        ...init,
        headers,
        credentials: 'same-origin',
    })
    if (!response.ok) {
        let body: ApiErrorBody = {}
        try {
            body = (await response.json()) as ApiErrorBody
        } catch {
            // Una risposta non JSON non viene inclusa nell'errore per evitare payload sensibili.
        }
        throw new ApiError(response.status, body.detail ?? 'Richiesta non riuscita')
    }
    if (response.status === 204) return undefined as T
    return (await response.json()) as T
}

export const api = {
    config: () => request<PublicConfig>('/api/public/config'),
    session: () => request<SessionState>('/api/auth/session'),
    bootstrap: (data: { username: string; display_name: string; password: string }) =>
        request<SessionState>('/api/auth/bootstrap', {
            method: 'POST',
            body: JSON.stringify(data),
        }),
    login: (data: { username: string; password: string }) =>
        request<SessionState>('/api/auth/login', {
            method: 'POST',
            body: JSON.stringify(data),
        }),
    logout: () => request<void>('/api/auth/logout', { method: 'POST' }),
    profile: () => request<Owner>('/api/profile'),
    status: () => request<StatusState>('/api/status'),
    imports: () => request<ImportJobs>('/api/imports'),
    demoStatus: () => request<DemoFixtureStatus>('/api/demo/status'),
}

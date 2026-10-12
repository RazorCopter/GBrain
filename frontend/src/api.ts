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
    connector_account_id: string | null
    source_policy_id: string | null
    queue_job_id: number | null
    mode: string | null
    status: string
    processed_items: number
    total_items: number | null
    checkpoint: Record<string, unknown>
    policy_snapshot: Record<string, string | number>
    error_code: string | null
    error_detail: string | null
    retry_count: number
    cancel_requested_at: string | null
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

export interface GmailAccount {
    id: string
    primary_email: string
    status: string
    confirmed_aliases: string[]
    aliases_ready: boolean
    authorized_at: string | null
    revoked_at: string | null
}

export interface GmailStatus {
    configured: boolean
    connected: boolean
    account: GmailAccount | null
    scope: string
    scope_notice: string
    sent_only: boolean
    capabilities: string[]
    configuration_action: string | null
}

export interface GmailPolicy {
    after: string
    before: string
    item_limit: number
    request_budget: number
    batch_size: number
}

export interface GmailSavedPolicy extends GmailPolicy {
    id: string
    status: string
    concurrency: number
}

export interface GmailPreview {
    job_id: string
    selected_items: number
    pages_read: number
    requests_used: number
    state: string
    truncated: boolean
    policy: Record<string, string | number>
    persisted_messages: number
    checkpoint_advanced: boolean
}

export interface GmailCitation {
    source_item_id: string
    source_revision_id: string
    account_id: string
    occurred_at: string | null
    subject: string | null
    snippet: string | null
    evidence: Record<string, unknown>
    proof_url: string
    source_limit: string
}

export interface GmailSources {
    items: GmailCitation[]
    total: number
    source_limit: string
}

export interface ConnectorCapabilities {
    read_only: boolean
    pagination: boolean
    incremental: boolean
    reconciliation: boolean
    max_page_size: number
    concurrency: number
    operations: string[]
}

export interface SourceFieldDescriptor {
    name: string
    label: string
    field_type: string
    required: boolean
    placeholder: string
    description: string
    help_text: string
}

export interface ImportPolicyDescriptor {
    supports_date_range: boolean
    default_window_days: number
    max_items_limit: number
    default_item_limit: number
    max_request_budget: number
    default_request_budget: number
    label_restriction: string | null
}

export interface SourceCatalogItem {
    id: string
    name: string
    category: string
    description: string
    icon: string
    auth_method: string
    status: 'available' | 'coming_soon' | 'deprecated' | string
    test_available: boolean
    sync_capabilities: ConnectorCapabilities | null
    config_fields: SourceFieldDescriptor[]
    import_policy: ImportPolicyDescriptor | null
    documentation_url: string | null
}

export interface SourceCatalogResponse {
    items: SourceCatalogItem[]
    total: number
}

export interface LanWarningResponse {
    is_private_ip: boolean
    warning_code: string
    message: string
}

export interface SourceStatusResponse {
    source_id: string
    configured: boolean
    auth_source: string | null
    client_id_preview: string | null
    account_connected: boolean
    external_account_id: string | null
    primary_email: string | null
    status: string
    detected_callback_url: string
    lan_warning: LanWarningResponse | null
}

export interface SourceCredentialsUploadPayload {
    raw_json?: Record<string, unknown> | string | null
    client_id?: string | null
    client_secret?: string | null
    redirect_uri_override?: string | null
}

export interface SourceCredentialsSaveResponse {
    source_id: string
    client_id: string | null
    client_type: string
    configured_redirect_uri: string | null
    message: string
}

export interface SourceTestConnectionResponse {
    source_id: string
    connected: boolean
    latency_ms: number | null
    primary_identity: string | null
    aliases: string[]
    message: string
}

export interface SourcePreviewSampleItem {
    id: string
    date: string | null
    subject: string | null
    to: string[]
    snippet: string | null
}

export interface SourcePreviewResponse {
    source_id: string
    estimated_count: number
    period_after: string
    period_before: string
    sample_items: SourcePreviewSampleItem[]
    query: string
    note: string
}

export interface SourceSyncPayload {
    period_after?: string | null
    period_before?: string | null
    item_limit?: number
    request_budget?: number
    batch_size?: number
    concurrency?: number
    mode?: 'full' | 'incremental' | 'reconciliation'
}

export interface SourceSyncResponse {
    source_id: string
    job_id: string
    queue_job_id: number | null
    status: string
    mode: string
    message: string
}

export interface SourceDisconnectResponse {
    source_id: string
    disconnected: boolean
    remote_revoked: boolean
    credentials_removed: boolean
    message: string
}

export interface GmailOAuthStartResponse {
    authorization_url: string
    state: string
    expires_at: string
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

function queryString(values: Record<string, string | number | undefined>): string {
    const parameters = new URLSearchParams()
    Object.entries(values).forEach(([key, value]) => {
        if (value !== undefined && value !== '') parameters.set(key, String(value))
    })
    const encoded = parameters.toString()
    return encoded ? `?${encoded}` : ''
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
    gmailStatus: () => request<GmailStatus>('/api/gmail/status'),
    gmailOAuthStart: () => request<{ authorization_url: string; expires_at: string }>(
        '/api/gmail/oauth/start',
        { method: 'POST' },
    ),
    gmailDisconnect: () => request<{ remote_attempted: boolean; remote_revoked: boolean; detail_code: string }>(
        '/api/gmail/disconnect',
        { method: 'POST' },
    ),
    gmailConfirmAliases: (aliases: string[]) => request<GmailAccount>('/api/gmail/aliases', {
        method: 'PUT',
        body: JSON.stringify({ aliases }),
    }),
    gmailRemoveAlias: (alias: string) => request<GmailAccount>(
        `/api/gmail/aliases/${encodeURIComponent(alias)}`,
        { method: 'DELETE' },
    ),
    gmailSavePolicy: (policy: GmailPolicy) => request<GmailSavedPolicy>('/api/gmail/policy', {
        method: 'PUT',
        body: JSON.stringify(policy),
    }),
    gmailPreview: (policy: GmailPolicy) => request<GmailPreview>('/api/gmail/preview', {
        method: 'POST',
        body: JSON.stringify(policy),
    }),
    gmailStartJob: (
        policyId: string,
        mode: 'full' | 'incremental' | 'reconciliation',
        confirmedPolicy: Record<string, string | number>,
    ) => request<ImportJob>('/api/gmail/jobs', {
        method: 'POST',
        body: JSON.stringify({
            policy_id: policyId,
            mode,
            confirmed_policy: confirmedPolicy,
            confirm_import: true,
        }),
    }),
    gmailJob: (id: string) => request<ImportJob>(`/api/gmail/jobs/${id}`),
    gmailCancelJob: (id: string) => request<ImportJob>(`/api/gmail/jobs/${id}/cancel`, {
        method: 'POST',
    }),
    gmailSources: (limit = 50) => request<GmailSources>(
        `/api/gmail/sources${queryString({ limit })}`,
    ),
    gmailSearch: (query: string, limit = 20) => request<GmailSources>(
        `/api/gmail/search${queryString({ query, limit })}`,
    ),

    // Endpoint unificati per Connettori e Source Wizard (Fase C / D)
    fetchSourceCatalog: () => request<SourceCatalogResponse>('/api/sources/catalog'),
    fetchSourceStatus: (sourceId: string) =>
        request<SourceStatusResponse>(`/api/sources/${encodeURIComponent(sourceId)}/status`),
    saveSourceCredentials: (sourceId: string, payload: SourceCredentialsUploadPayload) =>
        request<SourceCredentialsSaveResponse>(`/api/sources/${encodeURIComponent(sourceId)}/credentials`, {
            method: 'POST',
            body: JSON.stringify(payload),
        }),
    testSourceConnection: (sourceId: string) =>
        request<SourceTestConnectionResponse>(`/api/sources/${encodeURIComponent(sourceId)}/test-connection`, {
            method: 'POST',
        }),
    fetchSourcePreview: (
        sourceId: string,
        params?: { limit?: number; period_after?: string; period_before?: string },
    ) =>
        request<SourcePreviewResponse>(
            `/api/sources/${encodeURIComponent(sourceId)}/preview${queryString(params ?? {})}`,
        ),
    triggerSourceSync: (sourceId: string, payload?: SourceSyncPayload) =>
        request<SourceSyncResponse>(`/api/sources/${encodeURIComponent(sourceId)}/sync`, {
            method: 'POST',
            body: JSON.stringify(payload ?? {}),
        }),
    deleteSource: (sourceId: string, removeCredentials = false) =>
        request<SourceDisconnectResponse>(
            `/api/sources/${encodeURIComponent(sourceId)}${queryString({
                remove_credentials: removeCredentials ? 'true' : undefined,
            })}`,
            { method: 'DELETE' },
        ),
    startSourceOAuth: (sourceId: string) =>
        request<GmailOAuthStartResponse>(`/api/sources/${encodeURIComponent(sourceId)}/oauth/authorize`, {
            method: 'POST',
        }),
}

# PROJECT STATUS — checkpoint ufficiale riprendibile

**Data checkpoint:** 15 ottobre 2026
**Checkpoint:** Fase II — Coordinamento Quota Gmail, Pacing PostgreSQL condiviso e Fencing Transazionale Stale Snapshot (Migrazione 0007)
**Fase corrente:** Fase 0 consolidata; Fondazione Fase 1 verificata; Fase 2 Gmail `SENT` completata e protetta da coordinamento quote e isolamento concorrenza:
- Distinzione strutturata di errori HTTP 403 quota (`rateLimitExceeded`, `userRateLimitExceeded`, `quotaExceeded`) con [`ConnectorErrorCategory.QUOTA`](backend/src/gbrain/domain/connectors.py:18) (`retryable=True`) vs 403 auth con [`ConnectorErrorCategory.AUTH`](backend/src/gbrain/domain/connectors.py:17) (`retryable=False`);
- Header `Retry-After` delta/date bounded (`MAX_RETRY_AFTER_SECONDS = 300.0s`), backoff con jitter e ownership dei retry delegata al runner Procrastinate;
- Pacing condiviso tra worker tramite PostgreSQL con [`PostgresGmailRateLimiter`](backend/src/gbrain/infrastructure/gmail_rate_limiter.py:158) su tabella [`gmail_rate_limits`](backend/migrations/versions/20261015_0007_gmail_rate_limit.py:28), serializzazione atomica basata su `clock_timestamp()` senza burst (`GBRAIN_GMAIL_RATE_LIMIT_PER_SECOND=4`, min 250 ms) sia per account persistiti che per lo scope globale anonimo pre-account OAuth (`global:oauth_pre_account`);
- Matrice chiamate API strutturata (`list_messages`, `get_metadata`, `get_full`, `get_profile`, `list_history`) e deduplicazione preventiva degli ID messaggio;
- Snapshot semantico di sincronizzazione separato dai limiti operativi/budget richieste;
- Stato terminale [`ImportJobStatus.SUPERSEDED`](backend/src/gbrain/domain/models.py:34) con fencing transazionale per invalidare job concorrenti obsoleti in caso di riconfigurazione senza fallimenti spuri;
- Migrazione lineare `20261015_0007_gmail_rate_limit.py` con downgrade testato che riallinea i job `superseded` a `cancelled`;
- Supporto a messaggi Gmail grandi (rimosso veto preventivo 25 MiB), allegati metadata-only, diagnostica per-item (`gmail_item_gaps`), contatori semantici separati e stato `completed_with_warnings`;
- Suite test deterministica backend: **240 superati, 5 skipped su 245 raccolti**.
**Giudizio di prontezza:** controlli automatici locali superati (pytest, Ruff check/format, Mypy, build Vite) e migrazione PostgreSQL effimera verificata. I limiti runtime e l'integrazione con account Google reali restano vincolati all'autorizzazione con credenziali OAuth effettive (vedere Gate Gmail Reale).
**Non è una dichiarazione di MVP finale né di integrazione con servizi personali.**

## Regola di lettura dello stato

Questo checkpoint distingue rigorosamente:

- **codice predisposto:** file, schemi, API e UI esistono, sono stati revisionati trasversalmente e allineati;
- **test deterministico superato:** comandi locali eseguiti senza rete né servizi esterni hanno restituito exit 0 (240 test backend superati, 5 skipped su 245 raccolti + typecheck/build frontend);
- **test integrazione pendente:** test marcati esplicitamente con `@pytest.mark.skip` (5 test) che richiedono demone Docker Linux, PostgreSQL reale con estensioni o credenziali Google Cloud autorizzate;
- **integrazione reale non eseguita:** non eseguito collaudo runtime con account Google reale o container Docker su Linux host.

Un elemento predisposto o coperto da fixture/fake non equivale a un collaudo runtime. I rapporti precedenti ([`docs/RAPPORTO_COLLAUDO_FASE1_2026-10-11.md`](docs/RAPPORTO_COLLAUDO_FASE1_2026-10-11.md), sottocompiti A–E) rimangono storici; la sintesi ufficiale corrente della Fase 2 e dell'infrastruttura connettori è questo documento.

---

## Matrice criteri Fase 2: Raggiunti vs Pendenti

| Area / Criterio | Stato | Evidenza e Note |
|---|---|---|
| **Invariante `SENT` a più livelli** | **RAGGIUNTO** | Gateway filtra `q="label:SENT"`, verifica post-fetch `SENT` label, rifetch eventi history; gateway e connettore privi di metodi `send`, `modify`, `drafts`, `thread.get`. Testati in [`test_gmail_core.py`](backend/tests/test_gmail_core.py), [`test_gmail_gateway.py`](backend/tests/test_gmail_gateway.py), [`test_gmail_audit_fase2.py`](backend/tests/test_gmail_audit_fase2.py). |
| **Parser MIME e sicurezza contenuti** | **RAGGIUNTO** | Sanitizzazione HTML priva di caricamento tracker/immagini remote; supporto charset UTF-8 e ISO-8859-1 con fallback sicuro; enforcement massimali depth (10), body bytes (5 MB), parts count (100); prompt injection nel corpo e subject trattati puramente come testo passivo di evidenza non eseguibile (`untrusted_content: True`). |
| **OAuth Web server-side & PKCE** | **RAGGIUNTO** | Scope unico e stringente `gmail.readonly` con rifiuto immediato di ogni ampliamento; state HMAC-SHA256 ad alta entropia monouso con scadenza (10 min) e binding a owner e sessione; PKCE verifier cifrato AES-256-GCM; callback incompleta respinta; refresh controllato; revoca locale deterministica anche in caso di timeout/errore di Google Revoke. |
| **Cifratura token versionata (v1)** | **RAGGIUNTO** | AES-256-GCM autenticato con AAD contestuale, formato versionato `\x01` + IV (12 B) + ciphertext + tag (16 B); chiave da secret file dedicato a 32 byte; fallimento garantito su manomissione o chiave errata; nessun token o secret in log, payload Procrastinate o schema API pubblici. |
| **Gestione Identità e Alias Owner** | **RAGGIUNTO** | Import subordinato a conferma esplicita di almeno un alias verificato (`alias_required`); rimozione alias preserva integrità delle fonti già archiviate e incrementa `alias_set_version`. Corretto difetto di controllo preventivo in API e worker. |
| **Sincronizzazione, Transazioni e Checkpoint** | **RAGGIUNTO** | Batching atomico con checkpoint correlato; crash simulato prima/dopo commit gestito con rollback transazionale e persistenza del progresso; elementi invariati non toccati (`unchanged`); doppio sync idempotente; history expired attiva transizione controllata a `REQUIRED_HISTORY_EXPIRED`; elementi con contenuto identico ma Gmail ID diversi conservati distinti; elementi esclusi preservati; cancellazione cooperativa a confine di pagina. |
| **API Private e Protezione CSRF** | **RAGGIUNTO** | Tutte le route `/api/gmail/*` richiedono sessione valida (401 per anonimi); tutte le mutazioni (POST/PUT/DELETE) richiedono token CSRF valido (403 per CSRF mancante/errato); preview isolata senza persistenza di record sorgente; import richiede conferma crittografica della policy visualizzata; query utente FTS PostgreSQL isolata e mai inviata a Google. |
| **Coda Procrastinate & Concorrenza** | **RAGGIUNTO** | Lock globale `gmail-heavy-global` su coda `gmail`; retry limitato con backoff esponenziale solo per errori transienti/quota; payload task serializzati con soli ID logici e mode (nessuna credenziale); concorrenza fissata a 1. |
| **Ricerca Testuale FTS e Citazioni** | **RAGGIUNTO** | Indice PostgreSQL GIN su `to_tsvector('simple', ...)` con `websearch_to_tsquery`; citazioni strutturate con `source_item_id`, `source_revision_id`, `evidence`, `revision_hash` e link verificabile; filtri per account, date e limite massimo applicati. |
| **Source Connection Wizard & UI Fonti** | **RAGGIUNTO** | Nuova UI unificata [`SourcesPage.tsx`](frontend/src/SourcesPage.tsx) e [`SourceConnectionWizard.tsx`](frontend/src/SourceConnectionWizard.tsx) con catalogo connettori a schede, drag-and-drop / upload file JSON Google Cloud o input manuale credenziali, collegata a [`GmailSourcesPage.tsx`](frontend/src/GmailSourcesPage.tsx). Build verificata con Vite. |
| **Catalogo Connettori Estensibile** | **RAGGIUNTO** | Struttura modulare in [`backend/src/gbrain/domain/connectors.py`](backend/src/gbrain/domain/connectors.py:1) con metadata completi (`ConnectorCategory`, `AuthModel`, `SourceFieldDefinition`, `ConnectorCardMetadata`), endpoint `/api/sources/catalog` e predisposizione connettori futuri (Immich, NAS/Directory, OmniRoute). |
| **Credenziali Cifrate AES-256-GCM in DB** | **RAGGIUNTO** | Modulo crittografico [`backend/src/gbrain/application/crypto.py`](backend/src/gbrain/application/crypto.py:1) con crittografia autenticata AES-256-GCM e derivazione chiave PBKDF2; repository relazionale [`backend/src/gbrain/infrastructure/source_credentials_repository.py`](backend/src/gbrain/infrastructure/source_credentials_repository.py:1); endpoint REST privati `/api/sources/{source_id}/credentials` con CSRF protection; fallback trasparente su file secrets del filesystem. |
| **Schema e Migrazioni DB (Alembic Head)** | **RAGGIUNTO** | Quattro migrazioni Alembic incrementali e reversibili con Alembic head a `20261012_0004` ([`backend/migrations/versions/20261012_0004_source_credentials.py`](backend/migrations/versions/20261012_0004_source_credentials.py:1)); perfetta coerenza tra modelli ORM SQLAlchemy ([`SourceCredentialsModel`](backend/src/gbrain/domain/models.py:171)) e DDL migrazioni. |
| **Collaudo runtime Docker/Linux reale** | **PENDENTE** | Richiede demone Docker Linux operativo su host di prova (in ambiente di sviluppo locale la named pipe Docker Desktop Linux era assente). |
| **Collaudo Gmail reale Google Cloud** | **PENDENTE** | Richiede credenziali Google Cloud di prova configurate e consenso esplicito su casella reale; intenzionalmente non eseguito per evitare uso di credenziali o dati non autorizzati. |
| **Esecuzione Procrastinate su DB reale** | **PENDENTE** | Richiede PostgreSQL reale con estensioni e migrazioni applicate per validare il lock advisory/tabella Procrastinate. |

---

## Evoluzione e Nuove Feature — GBrain Source Connection Wizard

Nel ciclo di implementazione del **Source Connection Wizard** sono stati consolidati i seguenti componenti architetturali:

1. **Catalogo Modulare dei Connettori:**
   - Astrazione formale delle definizioni connettore in [`backend/src/gbrain/domain/connectors.py`](backend/src/gbrain/domain/connectors.py:1).
   - Esposizione di metadata dichiarativi via API (`id`, `name`, `category`, `description`, `icon`, `auth_model`, `config_fields`, `documentation_url`).
   - Supporto nativo per molteplici modelli di autenticazione: `oauth2_web`, `api_key`, `local_directory`, `webhook`.
2. **Gestione Credenziali Cifrate in Database:**
   - Tabella relazionale `source_credentials` introdotta tramite migrazione Alembic [`backend/migrations/versions/20261012_0004_source_credentials.py`](backend/migrations/versions/20261012_0004_source_credentials.py:1).
   - Conservazione confidenziale dei campi sensibili (`encrypted_client_secret`, `encrypted_extra_config`) cifrati con AES-256-GCM a 256 bit e IV casuale a 96 bit via [`backend/src/gbrain/application/crypto.py`](backend/src/gbrain/application/crypto.py:1).
   - Risoluzione gerarchica delle credenziali: il repository interroga prioritariamente il database relazionale; se assente, esegue il fallback non distruttivo sui secret montati da filesystem (`/run/secrets/gmail_oauth_client`).
3. **Interfaccia Utente Reattiva e Source Wizard:**
    - Componente [`frontend/src/SourceConnectionWizard.tsx`](frontend/src/SourceConnectionWizard.tsx:1) per la configurazione assistita in modalità upload file JSON Google o compilazione manuale dei campi (`Client ID`, `Client Secret`).
    - Schermata panoramica [`frontend/src/SourcesPage.tsx`](frontend/src/SourcesPage.tsx:1) con raggruppamento per stato (fonti connesse vs catalogo connettori disponibili) e navigazione fluida verso il connettore Gmail o il wizard di attivazione.
4. **Resilienza Flusso OAuth Google e Gestione Session Cookie:**
    - Configurazione del cookie di sessione con `SameSite=Lax` (default e parametro `session_samesite_cookie` in [`backend/src/gbrain/config.py`](backend/src/gbrain/config.py:63)) per preservare la sessione autenticata durante il redirect cross-site di callback OAuth da Google.
    - Gestione robusta dei callback OAuth con parametri di errore ed eventuale consenso negato dall'utente in [`backend/src/gbrain/presentation/sources_api.py`](backend/src/gbrain/presentation/sources_api.py:672) e [`backend/src/gbrain/presentation/gmail_api.py`](backend/src/gbrain/presentation/gmail_api.py:164) con reindirizzamento coerente alla vista `/sources` e feedback visivo.
5. **Risoluzione Errori Profile/Discovery, Offset-Naive Datetime e Diagnostica Gateway:**
    - Gestione timezone-naive/aware normalizzata su UTC per `expiry` di `google.oauth2.credentials.Credentials` in [`backend/src/gbrain/application/gmail_oauth.py`](backend/src/gbrain/application/gmail_oauth.py:148) e [`backend/src/gbrain/application/gmail_oauth.py`](backend/src/gbrain/application/gmail_oauth.py:192), eliminando l'errore `TypeError: can't compare offset-naive and offset-aware datetimes` durante il refresh automatico interno della libreria Google.
    - Disaccoppiamento del test di connessione da `users.getProfile` mediante fallback trasparente su `OfficialGmailGateway.list_history` o conteggio messaggi quando `users.getProfile` non è disponibile per gli scope autorizzati (`gmail.readonly`).
    - Classificazione degli errori HTTP e di refresh Google con diagnostica sanitizzata esente da segreti in [`backend/src/gbrain/infrastructure/gmail_gateway.py`](backend/src/gbrain/infrastructure/gmail_gateway.py:400) e persistenza del token rinfrescato in anteprima su [`backend/src/gbrain/presentation/sources_api.py`](backend/src/gbrain/presentation/sources_api.py:520).
6. **Risoluzione Autoritativa Modalità di Sincronizzazione e Gestione Robusta Checkpoint:**
    - Determinazione autoritativa backend-driven in [`backend/src/gbrain/domain/gmail.py`](backend/src/gbrain/domain/gmail.py:320) (`resolve_sync_mode`): forza la scansione `full` se l'importazione iniziale non è conclusa con successo (assenza di checkpoint, assenza di `history_id` valido o `page_token` residuo), consentendo la modalità `incremental` solo a fronte di checkpoint iniziale integro e forzando `reconciliation` in caso di `REQUIRED_HISTORY_EXPIRED`.
    - Resilienza a range vuoto: acquisizione automatica di `historyId` tramite fallback su `users.getProfile` quando la scansione iniziale copre un periodo privo di email inviate, prevenendo loop infiniti di riesecuzione `full`.
    - Checkpoint parziali e resumption token: supporto al tracciamento di `is_initial_complete=False` e salvataggio del token di paginazione corrente in caso di interruzione/raggiungimento budget, garantendo la continuazione senza salti.
    - Esposizione stato alle API e UI: inclusione di `initial_import_completed`, `has_checkpoint`, `checkpoint_history_id`, `checkpoint_page_token` in `GET /api/sources/{source_id}/status` e selezione automatica della modalità `full` vs `incremental` nel trigger rapido di sincronizzazione in [`frontend/src/SourcesPage.tsx`](frontend/src/SourcesPage.tsx:142).

---

## Risultati dei controlli eseguiti

Tutti i controlli deterministici sono stati eseguiti localmente con esito positivo:

- **Pytest:** `uv run pytest`
  - **240 test superati**, **5 test marcati esplicitamente pendenti/skipped** (collaudo Docker/PostgreSQL reale, Procrastinate reale, Google Cloud reale).
  - 0 fallimenti, 0 errori (**245 test raccolti**).
- **Ruff linter:** `uv run ruff check .`
  - **Superato:** 0 errori, 0 avvisi.
- **Ruff formatter:** `uv run ruff format --check .`
  - **Superato:** 90 file formattati in modo conforme.
- **Mypy type check:** `uv run mypy backend/src backend/tests scripts`
  - **Superato:** Successo su tutti i 59 file sorgente, 0 errori di tipo.
- **Frontend TypeScript & Build:** `npm --prefix frontend run typecheck && npm --prefix frontend run build`
  - **Superato:** Type check `tsc --noEmit` superato, `tsc -b` e bundle Vite di produzione (19 moduli) generato con successo.
- **Collaudo Migrazione Alembic Effimera:** ciclo `0004 → 0005 → 0004 → 0005`
  - **Superato:** Eseguito su PostgreSQL 16 Alpine containerizzato effimero (`gbrain-migration-ephemeral`), con bootstrap conservativo dei record legacy (revisione 1, policy `continuous`, `is_legacy=true`, backfill `idle`), verifica vincoli (`ck_sync_config_sent_only`, `ck_sync_config_batch_size`), tabelle `source_sync_configs`, `gmail_backfill_states`, `gmail_coverages` e colonne additive `config_revision`, `query_fingerprint` su `import_jobs` e `gmail_checkpoints`.
- **Collaudo Runtime Locale (API / Worker / Database effimero):**
  - **Superato:** Eseguita catena creazione configurazione sorgente -> aggiornamento a revisione 2 con filtro `has:attachment` -> accodamento open-ended job continuo -> esecuzione worker con `FakeGmailGateway` -> persistenza batch e avanzamento checkpoint History API e aggiornamento copertura (`config_revision=2`, `is_legacy=False`) su PostgreSQL effimero.
- **Docker Compose Validation:** `docker compose config` / `docker compose --profile compact config`
  - **Superato:** 0 errori sintattici o strutturali; isolate reti `backend` (internal) ed `edge` per worker; zero porte worker/postgres/neo4j.
- **Collaudo Connettività Worker:** [`scripts/verify_worker_connectivity.py`](scripts/verify_worker_connectivity.py)
  - **Superato:** Dal container del servizio worker reale (`gbrain:0.1.0`): DNS `oauth2.googleapis.com` (`142.251.127.95`), DNS `gmail.googleapis.com` (`172.217.114.4`), TLS handshake 443 OK, probe HTTPS HEAD senza token OK (Status 404 remoto).

---

## File modificati e creati nella feature Coordinamento Quota Gmail, Pacing PostgreSQL e Fencing Transazionale Stale Snapshot

- **Creati:**
  - [`backend/migrations/versions/20261015_0007_gmail_rate_limit.py`](backend/migrations/versions/20261015_0007_gmail_rate_limit.py:1) — Migrazione Alembic (head lineare `20261015_0007`) che introduce il valore enum `'superseded'` in `import_job_status`, la tabella di pacing `gmail_rate_limits` con indice `ix_gmail_rate_limits_connector_account_id` e procedura di downgrade con allineamento dei job superseded a `cancelled`.
  - [`backend/src/gbrain/infrastructure/gmail_rate_limiter.py`](backend/src/gbrain/infrastructure/gmail_rate_limiter.py:1) — Interfaccia astratta [`GmailRateLimiter`](backend/src/gbrain/infrastructure/gmail_rate_limiter.py:41), implementazione in-memory [`InMemoryNextSlotRateLimiter`](backend/src/gbrain/infrastructure/gmail_rate_limiter.py:76) e implementazione distribuita PostgreSQL [`PostgresGmailRateLimiter`](backend/src/gbrain/infrastructure/gmail_rate_limiter.py:158) basata sull'orologio atomico `clock_timestamp()` e `next_slot_at`, con supporto per scope account persistiti e scope globale anonimo pre-account OAuth (`global:oauth_pre_account`).
  - [`backend/tests/test_gmail_rate_limiter.py`](backend/tests/test_gmail_rate_limiter.py:1) — Suite esaustiva con test per pacing senza burst, cancellazione cooperativa, blocco Retry-After e gestione degli scope.
- **Modificati:**
  - [`backend/src/gbrain/config.py`](backend/src/gbrain/config.py:46) — Nuova variabile di configurazione `gmail_rate_limit_per_second` (default 4.0 req/s, corrispondente a un distanziamento minimo di 250 ms tra inizi chiamate successive).
  - [`example.env`](example.env:54) e [`compose.yaml`](compose.yaml:69) — Definizione e propagazione della variabile `GBRAIN_GMAIL_RATE_LIMIT_PER_SECOND=4` ai servizi worker e backend.
  - [`backend/src/gbrain/domain/models.py`](backend/src/gbrain/domain/models.py:34) — Aggiunta dello stato terminale `SUPERSEDED = "superseded"` nell'enum `ImportJobStatus`.
  - [`backend/src/gbrain/domain/connectors.py`](backend/src/gbrain/domain/connectors.py:18) — Formalizzazione della categoria `ConnectorErrorCategory.QUOTA` per errori di quota distinguibili dall'autenticazione.
  - [`backend/src/gbrain/infrastructure/gmail_gateway.py`](backend/src/gbrain/infrastructure/gmail_gateway.py:686) — Distinzione rigorosa tra errori HTTP 403 quota (`rateLimitExceeded`, `userRateLimitExceeded`, `quotaExceeded`) e 403 auth (`insufficientPermissions`, `invalid_grant`), parsing esteso di header `Retry-After` numerico e HTTP-date RFC 2822 limitato al cap di 300s, applicazione cooldown al rate limiter condiviso e deduplicazione ID messaggi.
  - [`backend/src/gbrain/infrastructure/gmail_repository.py`](backend/src/gbrain/infrastructure/gmail_repository.py:32) — Sollevamento di `GmailJobStaleSnapshotError` al rilevamento di discrepanza revisione/snapshot e metodo per contrassegnare i job invalidati con lo stato `superseded`.
  - [`backend/src/gbrain/application/gmail_jobs.py`](backend/src/gbrain/application/gmail_jobs.py:774) — Cattura di `GmailJobStaleSnapshotError` con fencing transazionale e marcatura del job a `superseded` (`phase: superseded_stale_revision`) senza generare fallimenti o retry spuri; integrazione del rate limiter condiviso Postgres nelle factory del gateway.
  - [`backend/src/gbrain/application/gmail_oauth.py`](backend/src/gbrain/application/gmail_oauth.py:1) — Applicazione del rate limiter con scope anonimo `global:oauth_pre_account` durante l'interrogazione del profilo utente (`users.getProfile`).
  - [`backend/src/gbrain/application/gmail_service.py`](backend/src/gbrain/application/gmail_service.py:1) — Fencing al momento dell'accodamento di nuovi job con invalidazione (`superseded`) dei lavori pendenti o in esecuzione sulla configurazione obsoleta.
  - [`backend/src/gbrain/worker.py`](backend/src/gbrain/worker.py:1) — Gestione dello stato `superseded` nel runner Procrastinate, logging dedicato e delega del backoff/jitter al retry mechanism di Procrastinate.
  - [`scripts/test_postgres_migration_recovery.py`](scripts/test_postgres_migration_recovery.py:1) — Aggiornamento del target di test migrazione da 0006 a 0007.

## File modificati e creati nella feature Gestione Robusta Messaggi Grandi e Diagnostica Per-Item

- **Creati:**
  - [`backend/migrations/versions/20261014_0006_gmail_item_diagnostics.py`](backend/migrations/versions/20261014_0006_gmail_item_diagnostics.py:1) — Migrazione Alembic (head lineare `20261014_0006`) per la tabella `gmail_item_gaps`, colonne contatori su `import_jobs`, stato e gap su `gmail_coverages` e `gmail_backfill_states`, e valore enum `completed_with_warnings`.
  - [`backend/tests/test_gmail_large_messages_and_diagnostics.py`](backend/tests/test_gmail_large_messages_and_diagnostics.py:1) — Suite end-to-end con 12 test dedicati che verificano analiticamente gli scenari da A a H (mail grandi, allegati metadata-only, body over limit, batch misto, ripresa, risoluzione gap, vincoli MIME estremi, worker status e assenza PII nelle API).
- **Modificati:**
  - [`backend/src/gbrain/application/gmail_mime.py`](backend/src/gbrain/application/gmail_mime.py:1) — Rimozione veto preventivo `sizeEstimate > 25 MiB`, parser bounded con calcolo preventivo prima di base64 decode, gestione allegati metadata-only senza download blob, sanitizzazione nomi file e sollevamento `GmailMimeError` con codici stabili (`BODY_TOO_LARGE`, `MIME_PARTS_LIMIT`, `MIME_DEPTH_LIMIT`, `ATTACHMENT_COUNT_LIMIT`, `MIME_TIMEOUT`).
  - [`backend/src/gbrain/domain/connectors.py`](backend/src/gbrain/domain/connectors.py:1) — Aggiunta classe `GmailMimeError` e attributo `reason_code` su `ConnectorError`.
  - [`backend/src/gbrain/domain/gmail.py`](backend/src/gbrain/domain/gmail.py:1) — Dataclass `GmailItemGapData`, estensione `GmailCoverageData`, `GmailBackfillStateData` e `GmailSyncResult` con contatori non importati e lacune.
  - [`backend/src/gbrain/domain/models.py`](backend/src/gbrain/domain/models.py:1) — Modello ORM `GmailItemGap` con unique constraint e indici, colonne contatori `imported_items`, `not_imported_items`, `duplicate_items` su `ImportJob`, `has_gaps`, `missing_count`, `status` su `GmailCoverage`, `items_not_imported` su `GmailBackfillState`, e valore `COMPLETED_WITH_WARNINGS` su `ImportJobStatus`.
  - [`backend/src/gbrain/application/gmail_sync.py`](backend/src/gbrain/application/gmail_sync.py:1) — Isolamento per elemento nei batch: errori permanenti di parsing diventano gap invece di abbattere la pagina, avanzamento atomico del checkpoint, salvataggio e risoluzione gap in `InMemoryGmailUnitOfWork`.
  - [`backend/src/gbrain/infrastructure/gmail_repository.py`](backend/src/gbrain/infrastructure/gmail_repository.py:1) — Persistenza transazionale atomica dei messaggi, risoluzione dei gap risolti, upsert idempotente dei gap aperti, calcolo automatico di `has_gaps` e `missing_count` nella copertura, e metodi `list_gaps`/`count_gaps`.
  - [`backend/src/gbrain/application/gmail_jobs.py`](backend/src/gbrain/application/gmail_jobs.py:1) — Gestione per-item nei runner di job (`_run_full`, `_run_incremental`, `_run_reconciliation`), commutazione a `COMPLETED_WITH_WARNINGS` in presenza di lacune permanenti, aggiornamento contatori nel job e rilancio controllato di errori fatali.
  - [`backend/src/gbrain/worker.py`](backend/src/gbrain/worker.py:1) — Verifica post-esecuzione del job applicativo nel wrapper Procrastinate: emissione warning per `COMPLETED_WITH_WARNINGS`, propagazione `GmailJobFatalError` per job `FAILED` senza doppi retry spuri.
  - [`backend/src/gbrain/presentation/schemas.py`](backend/src/gbrain/presentation/schemas.py:1) — Schemi Pydantic `GmailItemGapResponse`, `GmailItemGapListResponse`, arricchimento contatori su `ImportJobResponse`, `GmailCoverageResponse`, `GmailBackfillStateResponse` e `SourceStatusResponse` con coercizione sicura.
  - [`backend/src/gbrain/presentation/sources_api.py`](backend/src/gbrain/presentation/sources_api.py:1) — Endpoint protetto `GET /api/sources/{source_id}/diagnostics` per consultare lacune paginate senza PII ed esposizione dei flag di lacune in `get_source_status`.
  - [`frontend/src/api.ts`](frontend/src/api.ts:1) — Contratti TypeScript aggiornati e metodo `fetchSourceDiagnostics`.
  - [`frontend/src/SourcesPage.tsx`](frontend/src/SourcesPage.tsx:1) — Integrazione interfaccia: visualizzazione contatori semantici distinti, badge di copertura con lacune, banner job arricchito ed esploratore diagnostico paginato con dettagli sanitizzati.
  - [`backend/tests/test_gmail_core.py`](backend/tests/test_gmail_core.py:1) — Aggiornamento test per verificare che `sizeEstimate` grande non blocchi il parsing quando il corpo è entro i limiti e che corpi oltre limite sollevino `BODY_TOO_LARGE`.

## File modificati e creati nella feature Source Connection Wizard

- **Creati:**
  - [`backend/migrations/versions/20261012_0004_source_credentials.py`](backend/migrations/versions/20261012_0004_source_credentials.py:1) — Migrazione Alembic (head `20261012_0004`) per la tabella `source_credentials`.
  - [`backend/src/gbrain/application/crypto.py`](backend/src/gbrain/application/crypto.py:1) — Modulo crittografico unificato AES-256-GCM con autenticazione AAD e derivazione di chiave.
  - [`backend/src/gbrain/infrastructure/source_credentials_repository.py`](backend/src/gbrain/infrastructure/source_credentials_repository.py:1) — Repository relazionale asincrono per il ciclo di vita delle credenziali fonti.
  - [`backend/src/gbrain/presentation/sources_api.py`](backend/src/gbrain/presentation/sources_api.py:1) — Router FastAPI con endpoint `/api/sources/catalog` e `/api/sources/{source_id}/credentials`, arricchito con flag `aliases_ready` e lista `confirmed_aliases`.
  - [`backend/tests/test_sources_wizard_backend.py`](backend/tests/test_sources_wizard_backend.py:1) — Suite di 17 test per il catalogo fonti, cifratura credenziali, API private, fallback filesystem e flusso conferma esplicita alias.
  - [`frontend/src/SourceConnectionWizard.tsx`](frontend/src/SourceConnectionWizard.tsx:1) — Componente React per la configurazione guidata delle fonti con upload JSON, form manuale e step dedicato per la conferma/modifica esplicita degli alias proprietario prima della prima sincronizzazione.
  - [`frontend/src/SourcesPage.tsx`](frontend/src/SourcesPage.tsx:1) — Pagina catalogo fonti con schede connettori, stato di connessione, badge di prontezza alias e avvio guidato del wizard al passaggio alias.
- **Modificati:**
  - [`backend/src/gbrain/domain/connectors.py`](backend/src/gbrain/domain/connectors.py:1) — Estensione contrattuale con dataclass dei metadata connettori e catalogo registry.
  - [`backend/src/gbrain/domain/models.py`](backend/src/gbrain/domain/models.py:1) — Definizione ORM del modello [`SourceCredentialsModel`](backend/src/gbrain/domain/models.py:171).
  - [`backend/src/gbrain/application/gmail_oauth.py`](backend/src/gbrain/application/gmail_oauth.py:1) — Integrazione della risoluzione ibrida delle credenziali e normalizzazione UTC/naive dell'attributo `expiry` per prevenire errori di comparazione datetime.
  - [`backend/src/gbrain/infrastructure/gmail_gateway.py`](backend/src/gbrain/infrastructure/gmail_gateway.py:1) — Resilienza di `check_connection` con fallback su `list_history`/messaggi se `users.getProfile` non è disponibile per lo scope e diagnostica arricchita e sanitizzata.
  - [`backend/src/gbrain/presentation/sources_api.py`](backend/src/gbrain/presentation/sources_api.py:1) — Salvataggio automatico del token aggiornato dopo il refresh e router per wizard fonti.
  - [`backend/src/gbrain/application/gmail_service.py`](backend/src/gbrain/application/gmail_service.py:1) — Cablaggio del repository credenziali e gestione stato configurato dinamico.
  - [`backend/src/gbrain/presentation/api.py`](backend/src/gbrain/presentation/api.py:1) — Registrazione del nuovo router `sources_router`.
  - [`backend/src/gbrain/presentation/schemas.py`](backend/src/gbrain/presentation/schemas.py:1) — Schemi Pydantic per catalogo connettori e credenziali fonti.
  - [`backend/src/gbrain/config.py`](backend/src/gbrain/config.py:1) — Supporto a `GBRAIN_ENCRYPTION_MASTER_KEY` con fallback su chiave token Gmail.
  - [`backend/tests/test_gmail_gateway.py`](backend/tests/test_gmail_gateway.py:1) — Test di regressione per classificazione sanitizzata di errori Google API (HttpError, RefreshError, ServerNotFoundError).
  - [`backend/tests/test_sources_wizard_backend.py`](backend/tests/test_sources_wizard_backend.py:1) — Suite estesa (16 test) con validazione di preview e aggiornamento token su database.
  - [`frontend/src/api.ts`](frontend/src/api.ts:1) — Client API con metodi per catalogo fonti e salvataggio credenziali cifrate.
  - [`frontend/src/GmailSourcesPage.tsx`](frontend/src/GmailSourcesPage.tsx:1) — Integrazione del wizard e pulsante di configurazione rapida credenziali.
  - [`frontend/src/styles.css`](frontend/src/styles.css:1) — Stili responsivi per wizard di onboarding, card del catalogo fonti, upload box e interfaccia di selezione/conferma alias.
  - [`backend/src/gbrain/presentation/gmail_api.py`](backend/src/gbrain/presentation/gmail_api.py:1) — Endpoint dedicato `GET /api/gmail/account` per recupero account e supporto validazione flessibile alias.
  - [`backend/src/gbrain/presentation/schemas.py`](backend/src/gbrain/presentation/schemas.py:1) — Estensione schema `GmailAliasesRequest` per supportare `confirmed_addresses` e arricchimento `SourceStatusResponse` con `aliases_ready` e `confirmed_aliases`.
  - [`backend/src/gbrain/worker.py`](backend/src/gbrain/worker.py:1) — Inclusione della coda `gmail` nelle code di ascolto del worker Procrastinate (`WORKER_QUEUES`).
  - [`backend/tests/test_worker_lifecycle.py`](backend/tests/test_worker_lifecycle.py:1) — Verifica parametri di avvio e registrazione code ascoltate dal worker Procrastinate.
  - [`backend/src/gbrain/domain/gmail.py`](backend/src/gbrain/domain/gmail.py:1) — Definizione delle logiche di dominio autoritative: `resolve_sync_mode()`, `is_initial_import_completed()` e property `is_initial_complete` su `GmailCheckpointState`.
  - [`backend/src/gbrain/application/gmail_jobs.py`](backend/src/gbrain/application/gmail_jobs.py:1) — Supporto a checkpoint parziali e resumption token, risoluzione autoritativa del sync mode in esecuzione job Procrastinate, fallback su `get_profile` per `historyId` a range vuoto.
  - [`backend/src/gbrain/application/gmail_sync.py`](backend/src/gbrain/application/gmail_sync.py:1) — Determinazione autoritativa del sync mode all'interno di `GmailSyncEngine`, preservazione token di paginazione su budget parziale e lettura profilo su completamento scansione vuota.
  - [`backend/src/gbrain/application/gmail_service.py`](backend/src/gbrain/application/gmail_service.py:1) — Risoluzione autoritativa della modalità in `create_and_enqueue_job` e popolamento stato checkpoint.
  - [`backend/src/gbrain/presentation/sources_api.py`](backend/src/gbrain/presentation/sources_api.py:1) — Esposizione di `initial_import_completed`, `has_checkpoint`, `checkpoint_history_id`, `checkpoint_page_token` e normalizzazione sincronizzazione su `POST /api/sources/{source_id}/sync`.
  - [`backend/tests/test_gmail_sync_mode_and_checkpoints.py`](backend/tests/test_gmail_sync_mode_and_checkpoints.py:1) — Suite esaustiva (7 test) per determinazione autoritativa del sync mode, gestione range vuoto con fallback `historyId`, ripresa incrementale e ripresa da checkpoint parziale.
  - [`frontend/src/api.ts`](frontend/src/api.ts:1) — Tipizzazione estesa `SourceStatusResponse` con i flag di checkpoint e completamento iniziale.
  - [`frontend/src/SourcesPage.tsx`](frontend/src/SourcesPage.tsx:1) — Selezione dinamica e automatica di `incremental` vs `full` nel trigger rapido di sincronizzazione basata sullo stato autoritativo del backend.
  - [`compose.yaml`](compose.yaml:1) — Servizio worker connesso sia a `backend` sia a `edge`, nessuna porta pubblicata; PostgreSQL e Neo4j confinati a `backend` (`internal: true`).
  - [`backend/src/gbrain/worker.py`](backend/src/gbrain/worker.py:62) — Allineamento del budget retry al modello Procrastinate (`max_attempts=settings.gmail_job_retry_budget + 1`) per preservare il numero di riesecuzioni oltre al tentativo iniziale.
  - [`backend/src/gbrain/infrastructure/gmail_gateway.py`](backend/src/gbrain/infrastructure/gmail_gateway.py:1) — Rilevamento errori transitori nella catena causale delle eccezioni (`is_transient_transport_error`), classificazione retryable su `RefreshError` di rete, e sanitizzazione avanzata di token, refresh token, code, cookie, email e callback URL.
  - [`backend/src/gbrain/application/gmail_oauth.py`](backend/src/gbrain/application/gmail_oauth.py:1) — Definizione di `GmailOAuthTransientError`, classificazione transitoria vs permanente durante `refresh_credentials`, sanitizzazione messaggi.
  - [`backend/src/gbrain/application/gmail_jobs.py`](backend/src/gbrain/application/gmail_jobs.py:1) — Cattura di `GmailOAuthTransientError` con propagazione a `GmailJobTransientError` per retry controllato dal worker senza revoca dell'account.
  - [`backend/tests/test_gmail_gateway.py`](backend/tests/test_gmail_gateway.py:238) — Test mirati di sanitizzazione errori per refresh token, code, cookie, callback URL e classificazione transitoria errori DNS annidati.
  - [`backend/tests/test_worker_lifecycle.py`](backend/tests/test_worker_lifecycle.py:195) — Test della strategia di retry Procrastinate con verifica del comportamento al tentativo iniziale e all'esaurimento budget.
  - [`backend/tests/test_worker_network_and_transport.py`](backend/tests/test_worker_network_and_transport.py:1) — Suite (9 test) per invarianti di rete Compose, catena causale DNS (`NameResolutionError`), retry limitato, permanenza `invalid_grant` e sanitizzazione segreti.
  - [`scripts/verify_worker_connectivity.py`](scripts/verify_worker_connectivity.py:1) — Script probe innocuo (DNS + TLS 443 + HTTP HEAD senza credenziali) per validazione live dal container worker.
  - [`PROJECT_STATUS.md`](PROJECT_STATUS.md:1) — Checkpoint ufficiale aggiornato con connettività worker, classificazione errori trasporto e suite a 172 test superati.

---

## Verifiche runtime Gmail reali pendenti

Le seguenti verifiche richiedono un account Google Cloud / mailbox Gmail live e deployment sul nodo host, pertanto rimangono esplicitamente **pendenti** e non eseguite in locale:

1. **Mail reale con allegato >25 MiB:** Importazione di un messaggio reale con allegato esterno di grandi dimensioni, verificando che solo i metadati vengano salvati e nessun blob venga scaricato.
2. **Corpo reale oltre limite:** Ricezione e parsing di un'email reale con corpo eccedente la soglia bounded (`BODY_TOO_LARGE`), verificando la registrazione del gap e la continuazione del batch.
3. **Mixed page reale Gmail / History:** Paginazione mista su endpoint Gmail reali con mix di elementi validi e malformati/non importabili.
4. **Reprocess di gap dopo cambio policy:** Risoluzione automatica delle lacune diagnostiche (`status='resolved'`) in seguito a riconfigurazione o nuovo sync con account Google reale.
5. **Verifica UI Portainer post-deploy:** Accesso alla UI e monitoraggio dei container dopo il rilascio sul nodo LXC/Linux di produzione.

---

## Rischi residui

1. **Stack multi-container completo e persistenza su ambiente Linux di destinazione:** Il container worker è stato collaudato con successo a runtime per la connettività di rete esterna (DNS/TLS), ma l'orchestrazione congiunta completa con PostgreSQL su volumi host e locking advisory Procrastinate reale a caldo rimane da validare sul nodo LXC/Linux di produzione.
2. **Interazione di rete reale con Google API e limiti quota:** L'architettura implementa rate limiting distribuito a 4 req/s, backoff/jitter e gestione bounded di `Retry-After`. Tuttavia, le quote totali lato Google Cloud (250 quota units/sec per utente e 1.000.000.000 quota units/giorno di progetto), eventuali blocchi da client/processi concorrenti esterni non coordinati o variazioni unilaterali delle policy del provider non possono essere garantiti in modo assoluto dal solo client locale; l'effettiva reattività alle revocazioni remote e la latenza dei refresh token rimangono verificabili soltanto con account Google Cloud reale.
3. **Backup Neo4j/PostgreSQL non testato a caldo:** Neo4j resta una proiezione sincrona non ancora popolata dall'outbox worker.

---

## Prossimo incremento — Backlog prioritizzato Fase 3

La Fase 2 Gmail `SENT` è formalmente **chiusa a livello di codice e test deterministici**. Il backlog per i prossimi incrementi è strutturato secondo priorità decrescente:

1. **Gate di Collaudo Runtime (Gate Infrastrutturale 0/1):**
   - Configurare host Linux/LXC con demone Docker Engine e Compose v2 operativo;
   - Eseguire migrazioni da zero su PostgreSQL 16 con pgvector;
   - Eseguire suite con worker Procrastinate reale e lock PostgreSQL nativi;
   - Verificare backup/restore reale con [`scripts/backup.sh`](scripts/backup.sh) e [`scripts/restore.sh`](scripts/restore.sh).
2. **Gate Gmail Reale Autorizzato (Gate Rete):**
   - Registrazione client OAuth Google Cloud (tipo Web Application) con redirect HTTPS o localhost;
   - Montaggio secret file (`gmail-oauth-client.json` e `gmail-token-key.txt`);
   - Test con account Gmail di prova: verifica autorizzazione, fetch campionario, checkpoint e revoca.
3. **Fase 3 — Memoria Autobiografica e Graphiti (Architettura Primaria):**
   - **P3-001:** Definizione schema entità/relazioni e proiezioni temporali su PostgreSQL autorevole (episodi, persone, luoghi, decisioni derivate da email `SENT`);
   - **P3-002:** Consumer outbox per la proiezione asincrona su Neo4j (grafo della conoscenza) con procedura di rebuild integrale da PostgreSQL;
   - **P3-003:** Pipeline di estrazione con Graphiti / LLM locale/remoto: consumo chunk da `SourceRevision` con citazioni verificabili e guardrail contro prompt injection passiva;
   - **P3-004:** Integrazione ricerca ibrida: FTS lessicale PostgreSQL + ricerca semantica vettoriale (pgvector) + esplorazione relazioni sul grafo Neo4j.
4. **Connettore Secondario Immich (Architettura Connettori):**
   - Implementazione connettore Immich (asset foto/video, metadata EXIF, timestamp geografici) secondo il contratto `ConnectorCapability` in [`backend/src/gbrain/domain/connectors.py`](backend/src/gbrain/domain/connectors.py).

---

## Verifiche e Prove Eseguite (Fase I — Gestione Persistente Fonte Gmail)

1. **Test di Unità e Regressione E2E:**
  - Esecuzione `uv run pytest`: **178 superati, 5 skipped** (183 raccolti).
  - Nuova suite mirata [`backend/tests/test_gmail_persistent_source_e2e.py`](backend/tests/test_gmail_persistent_source_e2e.py:1): 6/6 test superati a copertura degli scenari A (import storico e copertura coerente), B (aggiornamento continuo senza riuso upper bound statico), C (ampliamento storico verso il passato), D (idempotenza), E (ripresa con page token), F (invalidazione token e nuova revisione su cambio filtri) e relative API.
2. **Controllo Statico e Formattazione:**
  - `uv run ruff check backend`: superato (0 errori).
  - `uv run ruff format --check backend`: superato (59 file formattati).
  - `uv run mypy backend/src`: superato (Success: no issues found in 34 source files).
3. **Build Frontend:**
  - `npm run build` in `frontend/`: superato senza warning TypeScript o Vite.
4. **Linearità Migrazioni Alembic:**
  - `uv run alembic heads`: superato con head lineare `20261013_0005`.

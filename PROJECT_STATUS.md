# PROJECT STATUS — checkpoint ufficiale riprendibile

**Data checkpoint:** 13 ottobre 2026
**Checkpoint:** Fase G — Risoluzione autoritativa modalità di sincronizzazione (full vs incremental), gestione robusta checkpoint e ripresa esatta
**Fase corrente:** Fase 0 consolidata; Fondazione Fase 1 verificata staticamente/localmente; Fase 2 Gmail `SENT` arricchita con il Source Connection Wizard e con la **determinazione autoritativa backend-driven della modalità di sincronizzazione** (`resolve_sync_mode`), validazione rigorosa del completamento iniziale (`is_initial_complete`), fallback su `users.getProfile` per acquisizione `historyId` su caselle senza messaggi nel range, gestione affidabile della ripresa checkpoint parziale con token di paginazione, e suite di test deterministica estesa a **161 test superati** (166 totali raccolti, 5 skipped).
**Giudizio di prontezza:** codice Fase 1 e Fase 2 completo a livello applicativo e UI; pronto per il collaudo infrastrutturale Docker/Linux e configurazione con account reali; non pronto per dichiarare MVP finale né integrazione con servizi personali già collaudata in produzione.
**Non è una dichiarazione di MVP finale né di integrazione con servizi personali.**

## Regola di lettura dello stato

Questo checkpoint distingue rigorosamente:

- **codice predisposto:** file, schemi, API e UI esistono, sono stati revisionati trasversalmente e allineati;
- **test deterministico superato:** comandi locali eseguiti senza rete né servizi esterni hanno restituito exit 0 (161 test backend superati + typecheck/build frontend);
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

- **Pytest:** `uv run pytest backend/tests -v`
  - **161 test superati**, **5 test marcati esplicitamente pendenti/skipped** (collaudo Docker/PostgreSQL reale, Procrastinate reale, Google Cloud reale).
  - 0 fallimenti, 0 errori (166 raccolti).
- **Ruff linter:** `uv run ruff check backend`
  - **Superato:** 0 errori, 0 avvisi.
- **Ruff formatter:** `uv run ruff format --check backend`
  - **Superato:** 56 file formattati in modo conforme.
- **Mypy type check:** `uv run mypy backend/src backend/tests`
  - **Superato:** Successo su tutti i 51 file sorgente, 0 errori di tipo.
- **Frontend TypeScript & Build:** `npm --prefix frontend run build`
  - **Superato:** Type check `tsc -b` superato, bundle Vite completato senza errori.

---

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
  - [`PROJECT_STATUS.md`](PROJECT_STATUS.md:1) — Checkpoint ufficiale aggiornato con risoluzione autoritativa sync mode, robustezza checkpoint e suite test a 161 test superati.

---

## Rischi residui

1. **Mancanza di demone Docker operativo locale:** Impedisce la validazione dei vincoli a runtime (migrazione PostgreSQL reale da zero, locking advisory Procrastinate nativo, healthcheck container e mapping volumi LXC non-root).
2. **Interazione di rete reale con Google API:** Le quote reali (250 quota units/sec utente), l'effettiva reattività alle revocazioni remote e la latenza dei refresh token rimangono verificabili soltanto con account Google Cloud reale.
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

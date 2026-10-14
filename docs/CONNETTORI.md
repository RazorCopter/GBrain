# Contratto dei connettori e Catalogo Modulare Fonti

> **Stato 12 ottobre 2026:** contratto dei connettori, nucleo Gmail `SENT` e nuovo **Catalogo Modulare Fonti con Source Connection Wizard** (gestione credenziali cifrate AES-256-GCM su PostgreSQL con Alembic head `20261012_0004`, fallback filesystem, UI reattiva con upload JSON e 108 test deterministici superati) sono implementati e verificabili senza rete. Collaudi reali con Docker/Linux e Google Cloud restano pendenti; nessuna credenziale reale è stata usata.

Il contratto in [`backend/src/gbrain/domain/connectors.py`](../backend/src/gbrain/domain/connectors.py) usa protocolli e dataclass generici senza esporre oggetti dinamici Google. Gmail specializza selezione, cursor, locator, raw message, checkpoint e risultato in [`backend/src/gbrain/domain/gmail.py`](../backend/src/gbrain/domain/gmail.py). Setup operativo e restrizioni sono in [`docs/GMAIL_OAUTH_SETUP.md`](GMAIL_OAUTH_SETUP.md).

## Catalogo Modulare delle Fonti

GBrain adotta un catalogo estensibile per registrare, visualizzare e configurare sorgenti dati eterogenee (email, fotografie, file locali, chat, note, dati biometrici).

### Metadata e Astrazioni del Catalogo
In [`backend/src/gbrain/domain/connectors.py`](../backend/src/gbrain/domain/connectors.py:1) sono formalizzate le strutture dati fondamentali:
- **`ConnectorCategory`:** `email`, `photos`, `files`, `chat`, `notes`, `health`, `custom`;
- **`AuthModel`:** `oauth2_web`, `api_key`, `local_directory`, `webhook`, `none`;
- **`SourceFieldDefinition`:** specifica dichiarativa di ogni campo richiesto dal connettore (`id`, `label`, `type`, `required`, `placeholder`, `description`, `default_value`, `is_secret`);
- **`ConnectorCardMetadata`:** metadata completi della scheda per la UI (`id`, `name`, `category`, `description`, `icon`, `auth_model`, `status`, `config_fields`, `documentation_url`).

### Connettori Registrati nel Catalogo
1. **Gmail SENT (`gmail`):** attivo e configurabile. Autenticazione `oauth2_web` con scope restrittivo `gmail.readonly`, importazione guidata via JSON o campi `client_id`/`client_secret`.
2. **Immich Photos (`immich`):** predisposto a catalogo (categoria `photos`, auth `api_key`). Campi: endpoint host e API key.
3. **Directory / NAS Locale (`local_directory`):** predisposto a catalogo (categoria `files`, auth `local_directory`). Campi: percorso directory (`path`) e pattern filtri.
4. **OmniRoute Chat (`omniroute`):** predisposto a catalogo (categoria `chat`, auth `webhook`).

### Endpoint REST del Catalogo e Credenziali
- `GET /api/sources/catalog`: restituisce la lista di tutti i connettori disponibili arricchiti con il relativo stato di configurazione dinamico rilevato dal repository;
- `POST /api/sources/{source_id}/credentials`: riceve le credenziali dal client (protetto da sessione e token anti-CSRF), effettua la validazione e le memorizza cifrate;
- `GET /api/sources/{source_id}/credentials`: restituisce i metadati della fonte senza mai esporre i secret cifrati in chiaro.

## Operazioni e Interfacce

Ogni connettore deve implementare la seguente specifica funzionale minima:

1. `test_connection() -> status`  
   Ping a un endpoint protetto utile per misurare l'identità o stabilire permessi.

2. `inspect_capabilities() -> dict`  
   Restituisce i massimali (rate-limit contrattualizzati, estensioni supportate, capacità di ordinamento nativo e paginazione).

3. `preview_selection(criteria) -> metrics`  
   Analizza quanto un pool logico restituirà per estrarre statistiche pre-consumo.

4. `enumerate_page(cursor) -> [OutputLocator]`  
   Recupera un pool limitato e scalabile di puntatori a eventi/documenti (es. IDs delle transazioni, hash dei commit).

5. `fetch_item(locator) -> RawData`  
   Richiede il transito fisico del payload o del contenuto di un ID precedentemente ritornato. Assicura ritentativi passivi (exponential backoffs).

6. `normalize_item(raw_data) -> NormalizedSchema`  
   Decosta il formato proprietario, emettendo la controparte allineata col [MODELLO DATI](./MODELLO_DATI.md) applicativo (schema Pydantic stringente).

7. `checkpoint(state_locator) -> persistence`  
   Richiamato dalla pipeline superiore, fissa l'alto corso di scaricamento su Data Base per permettere riprese lineari all'effettuarsi di un riavvio imprevisto dell'applicativo (es. Out Of Memory).

8. `reconcile() -> report_diffs`  
   Verifica il discostamento fra lo snapshot posseduto attualmente e un set modificato dalla sorgente (fondamentale per intercettare soft deletes o tombstone non comunicate attivamente tramite callback).

## Idempotenza
Nessun connettore sa nulla di Neo4j. L'assunzione dei connettori è d'inserimento o merge condizionato in base a chiavi uniche (hash sha256 + ID sistema) verso gli endpoints di ingestione centrali di PostgreSQL.

## Gmail `SENT` implementato fixture-first

Il nucleo implementato comprende:

- scope esatto `https://www.googleapis.com/auth/gmail.readonly`; l'interfaccia non contiene send, modify, drafts o threads;
- gateway ufficiale in [`backend/src/gbrain/infrastructure/gmail_gateway.py`](../backend/src/gbrain/infrastructure/gmail_gateway.py), con `labelIds=["SENT"]`, query temporale costruita internamente, fetch `full`/`metadata`, verifica post-fetch della label e history sempre rifetchata;
- preview senza fetch/persistenza/checkpoint, paginazione `nextPageToken`, limite elementi, budget richieste/elementi, stato completo/parziale/troncato e concorrenza dichiarata uno;
- motore full/incrementale/reconcile in [`backend/src/gbrain/application/gmail_sync.py`](../backend/src/gbrain/application/gmail_sync.py), con batch/pagina transazionale, replay idempotente, history scaduta marcata per riconciliazione e rimozione logica senza cancellare Gmail;
- parser MIME limitato in [`backend/src/gbrain/application/gmail_mime.py`](../backend/src/gbrain/application/gmail_mime.py): multipart, base64url, charset, HTML convertito in testo senza fetch remoti, metadata allegati e separazione euristica deterministica tra nuovo testo, firma, citazione e inoltro;
- attribuzione certa soltanto quando il solo mittente coincide con un alias owner confermato; il solo `From` non registra nuovi alias;
- contenuti marcati non fidati: URL e prompt injection restano dati citabili e non influenzano policy o strumenti.

### Gestione Quota Google, Pacing Condiviso e Fencing Transazionale

Per garantire un'interazione resiliente e coordinata con le API di Google:
1. **Pacing condiviso via PostgreSQL ([`backend/src/gbrain/infrastructure/gmail_rate_limiter.py`](backend/src/gbrain/infrastructure/gmail_rate_limiter.py:1)):**
   - Implementazione [`PostgresGmailRateLimiter`](backend/src/gbrain/infrastructure/gmail_rate_limiter.py:158) che usa la tabella [`gmail_rate_limits`](backend/migrations/versions/20261015_0007_gmail_rate_limit.py:28) e l'orologio atomico `clock_timestamp()` del database per serializzare e distanziare uniformemente le chiamate API HTTP;
   - La frequenza predefinita è impostata da [`Settings.gmail_rate_limit_per_second`](backend/src/gbrain/config.py:46) (`GBRAIN_GMAIL_RATE_LIMIT_PER_SECOND=4`, pari a uno slot ogni 250 ms senza burst accumulato);
   - Supporta scope dedicati per singolo account registrato (FK su [`connector_accounts.id`](backend/src/gbrain/domain/models.py:130)) e uno scope globale pre-account ([`GMAIL_OAUTH_PRE_ACCOUNT_SCOPE`](backend/src/gbrain/infrastructure/gmail_rate_limiter.py:20) = `'global:oauth_pre_account'`) per distanziare le chiamate `get_profile` pre-salvataggio senza memorizzare alcuna informazione personale (PII).
2. **Distinzione semantica errori Quota vs Auth ([`backend/src/gbrain/infrastructure/gmail_gateway.py`](backend/src/gbrain/infrastructure/gmail_gateway.py:686)):**
   - Gli errori HTTP 403 con motivi `rateLimitExceeded`, `userRateLimitExceeded` o `quotaExceeded` (oltre a HTTP 429) sono classificati categoricamente come [`ConnectorErrorCategory.QUOTA`](backend/src/gbrain/domain/connectors.py:18) (`retryable=True`);
   - Gli errori HTTP 403 privi di tali ragioni specifiche vengono invece classificati come [`ConnectorErrorCategory.AUTH`](backend/src/gbrain/domain/connectors.py:17) (`retryable=False`, non tentabili);
   - Gli header `Retry-After` (sia in formato delta-seconds numerico sia in formato HTTP-Date RFC 2822) sono parsati e limitati a un tetto massimo di sicurezza (`MAX_RETRY_AFTER_SECONDS = 300.0s`).
3. **Fencing transazionale e stato terminale `superseded` ([`backend/src/gbrain/domain/models.py`](backend/src/gbrain/domain/models.py:34)):**
   - Qualora la configurazione di un account o lo snapshot semantico di sincronizzazione vengano alterati mentre un lavoro asincrono è in corso, il controllo di versione in [`AsyncPostgresGmailRepository`](backend/src/gbrain/infrastructure/gmail_repository.py:1) solleva [`GmailJobStaleSnapshotError`](backend/src/gbrain/infrastructure/gmail_repository.py:32);
   - Il job non viene fallito erroneamente né riaccodato all'infinito, ma marcato nello stato terminale [`ImportJobStatus.SUPERSEDED`](backend/src/gbrain/domain/models.py:34) con fase `superseded_stale_revision`, impedendo sovrascritture distruttive concorrenti.
4. **Matrice chiamate e deduplicazione ID:**
   - La matrice delle operazioni censisce `list_messages`, `get_metadata`, `get_full`, `get_profile` e `list_history`, con deduplicazione preventiva degli ID messaggio su ciascun batch enumerato.

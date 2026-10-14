# Runbook Operativo: Diagnosi e Recupero Puntuale Job Gmail `1f983c97-90f5-43b9-a45b-4bed0bb4ea43`

## 1. Avvertenza Iniziale e Perimetro Operativo

> [!CAUTION]
> **AVVERTENZA DI SICUREZZA ED ESECUZIONE:**
> 1. **Nessun accesso server o container LXC è stato eseguito** durante la redazione di questo runbook.
> 2. **Nessun dato reale di produzione è stato osservato** o estratto.
> 3. **Nessun recupero o mutazione è stata effettuata**: il presente documento costituisce una **procedura operativa strettamente condizionale**, destinata all'operatore autorizzato.
> 4. Ogni comando ed eventuale azione correttiva deve essere subordinata all'esito dei controlli di sola lettura descritti nella Sezione 2 e alla Decision Table della Sezione 3.

Questo runbook definisce la procedura per diagnosticare lo stato del job Gmail con identificatore univoco:
- **Job UUID:** `1f983c97-90f5-43b9-a45b-4bed0bb4ea43`
- **Tabella applicativa:** [`import_jobs`](../backend/src/gbrain/domain/models.py:142)
- **Tabella di coda:** `procrastinate_jobs` (cfr. [`fetch_procrastinate_job_snapshot()`](../backend/src/gbrain/infrastructure/procrastinate_jobs.py:123))

---

## 2. Ispezione Diagnostica Read-Only Priva di PII

Prima di qualsiasi decisione operativa, l'operatore esegue su PostgreSQL lo script diagnostico seguente.
Lo script opera rigorosamente in una transazione `READ ONLY` con livello di isolamento `REPEATABLE READ`, si conclude con `ROLLBACK` e **non espone alcuna informazione personale o sensibile (PII)**:
- Vengono omessi indirizzi email, destinatari, mittenti, oggetti e corpi dei messaggi.
- Vengono omessi token OAuth (`oauth_token_ciphertext`), segreti e cursori di paginazione completi.
- Non viene estratto `error_detail` non sanitizzato, ma solo il codice standardizzato `error_code`.
- Dalla tabella `procrastinate_jobs.args` vengono estratti esclusivamente i campi tecnici `job_id`, `account_id` e `mode`.
- I dati importati, le lacune e lo stato di sincronizzazione sono aggregati a livello numerico e temporale.
- Tutte le interrogazioni sono rigorosamente limitate al target identificato da `:'target_job_id'::uuid`.

### Script psql Copiabile

```sql
\set ON_ERROR_STOP on
\set target_job_id '1f983c97-90f5-43b9-a45b-4bed0bb4ea43'

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

-- 1. Snapshot del lavoro applicativo (ImportJob) e metriche di avanzamento
SELECT
    ij.id AS import_job_id,
    ij.source_kind,
    ij.connector_account_id,
    ij.mode,
    ij.status::text AS import_job_status,
    ij.queue_job_id,
    ij.processed_items,
    ij.imported_items,
    ij.not_imported_items,
    ij.duplicate_items,
    ij.total_items,
    ij.retry_count,
    ij.error_code,
    ij.config_revision,
    ij.query_fingerprint,
    ij.cancel_requested_at,
    ij.started_at,
    ij.finished_at,
    ij.created_at,
    ij.updated_at
FROM import_jobs AS ij
WHERE ij.id = :'target_job_id'::uuid;

-- 2. Checkpoint operativo sicuro del job (solo fase e chiavi, nessun cursore o PII)
SELECT
    ij.id AS import_job_id,
    ij.checkpoint->>'phase' AS checkpoint_phase,
    ij.checkpoint->>'page_index' AS checkpoint_page_index,
    (SELECT jsonb_agg(k) FROM jsonb_object_keys(ij.checkpoint) AS k) AS checkpoint_keys,
    (SELECT jsonb_agg(k) FROM jsonb_object_keys(ij.policy_snapshot) AS k) AS policy_snapshot_keys
FROM import_jobs AS ij
WHERE ij.id = :'target_job_id'::uuid;

-- 3. Snapshot infrastrutturale della coda (Procrastinate) e heartbeat worker
SELECT
    pj.id AS procrastinate_job_id,
    pj.queue_name,
    pj.task_name,
    pj.status::text AS procrastinate_status,
    pj.priority,
    pj.lock,
    pj.queueing_lock,
    pj.attempts,
    pj.scheduled_at,
    pj.abort_requested,
    pj.worker_id,
    pw.last_heartbeat AS worker_last_heartbeat,
    clock_timestamp() AS db_clock,
    CASE
        WHEN pw.last_heartbeat IS NOT NULL THEN (clock_timestamp() - pw.last_heartbeat)
        ELSE NULL
    END AS worker_heartbeat_age,
    -- Coerenza args (estrazione selettiva sicura)
    pj.args->>'job_id' AS args_job_id,
    pj.args->>'account_id' AS args_account_id,
    pj.args->>'mode' AS args_mode
FROM import_jobs AS ij
JOIN procrastinate_jobs AS pj ON pj.id = ij.queue_job_id
LEFT JOIN procrastinate_workers AS pw ON pw.id = pj.worker_id
WHERE ij.id = :'target_job_id'::uuid;

-- 4. Eventi recenti di transizione Procrastinate (sanitizzati: solo tipo e timestamp)
SELECT
    pe.job_id,
    pe.type::text AS event_type,
    pe.at AS event_at
FROM import_jobs AS ij
JOIN procrastinate_events AS pe ON pe.job_id = ij.queue_job_id
WHERE ij.id = :'target_job_id'::uuid
ORDER BY pe.at DESC, pe.id DESC
LIMIT 20;

-- 5. Analisi Blocker Lock (rilevamento concorrenza sul lock di coda)
SELECT
    other_jobs.id AS blocker_procrastinate_id,
    other_jobs.queue_name,
    other_jobs.task_name,
    other_jobs.lock,
    other_jobs.status::text AS blocker_status,
    other_jobs.priority,
    other_jobs.scheduled_at,
    other_jobs.args->>'job_id' AS blocker_target_job_id
FROM import_jobs AS ij
JOIN procrastinate_jobs AS target_pj ON target_pj.id = ij.queue_job_id
JOIN procrastinate_jobs AS other_jobs ON other_jobs.lock = target_pj.lock
WHERE ij.id = :'target_job_id'::uuid
  AND other_jobs.id <> target_pj.id
  AND (
      other_jobs.status = 'doing'
      OR (
          other_jobs.status = 'todo'
          AND (
              other_jobs.priority > target_pj.priority
              OR (other_jobs.priority = target_pj.priority AND other_jobs.id < target_pj.id)
          )
      )
  );

-- 6. Stato generale dell'account connettore collegato (senza secret né email)
SELECT
    ca.id AS connector_account_id,
    ca.connector_kind,
    ca.status::text AS connector_status,
    ca.alias_set_version,
    ca.authorized_at,
    ca.revoked_at,
    ca.last_connection_test_at
FROM import_jobs AS ij
JOIN connector_accounts AS ca ON ca.id = ij.connector_account_id
WHERE ij.id = :'target_job_id'::uuid;

-- 7. Checkpoint persistente dell'account (fasi e revisioni operative, no token grezzi)
SELECT
    gc.connector_account_id,
    gc.version AS checkpoint_version,
    gc.config_revision,
    gc.query_fingerprint,
    gc.reconciliation_state::text AS reconciliation_state,
    gc.last_error_code,
    gc.coverage_after,
    gc.coverage_before,
    gc.last_sync_at,
    (gc.history_id IS NOT NULL) AS has_history_id,
    (gc.page_token IS NOT NULL) AS has_page_token
FROM import_jobs AS ij
JOIN gmail_checkpoints AS gc ON gc.connector_account_id = ij.connector_account_id
WHERE ij.id = :'target_job_id'::uuid;

-- 8. Stato della copertura e del backfill storico dell'account
SELECT
    'coverage' AS component,
    c.status AS state,
    c.config_revision,
    c.query_fingerprint,
    c.covered_after AS range_after,
    c.covered_before AS range_before,
    c.is_legacy,
    c.has_gaps,
    c.missing_count,
    c.last_completed_at
FROM import_jobs AS ij
JOIN gmail_coverages AS c ON c.connector_account_id = ij.connector_account_id
WHERE ij.id = :'target_job_id'::uuid
UNION ALL
SELECT
    'backfill' AS component,
    b.status AS state,
    b.config_revision,
    b.query_fingerprint,
    b.history_after AS range_after,
    b.history_before AS range_before,
    FALSE AS is_legacy,
    (b.items_not_imported > 0) AS has_gaps,
    b.items_not_imported AS missing_count,
    b.last_run_at AS last_completed_at
FROM import_jobs AS ij
JOIN gmail_backfill_states AS b ON b.connector_account_id = ij.connector_account_id
WHERE ij.id = :'target_job_id'::uuid;

-- 9. Diagnostica aggregata delle lacune (gmail_item_gaps) senza remote IDs
SELECT
    gap.phase,
    gap.reason_code,
    gap.status,
    gap.config_revision,
    COUNT(*) AS count_gaps,
    MIN(gap.occurred_at) AS earliest_occurred_at,
    MAX(gap.occurred_at) AS latest_occurred_at
FROM import_jobs AS ij
JOIN gmail_item_gaps AS gap ON gap.connector_account_id = ij.connector_account_id
WHERE ij.id = :'target_job_id'::uuid
GROUP BY gap.phase, gap.reason_code, gap.status, gap.config_revision;

-- 10. Metriche aggregate dei dati già importati (source_items / source_revisions)
SELECT
    si.source_kind,
    si.source_state::text AS source_state,
    COUNT(DISTINCT si.id) AS items_count,
    (
        SELECT COUNT(*)
        FROM source_revisions AS sr
        JOIN source_items AS si_sub ON si_sub.id = sr.source_item_id
        WHERE si_sub.connector_account_id = si.connector_account_id
          AND si_sub.source_kind = si.source_kind
          AND si_sub.source_state = si.source_state
    ) AS revisions_count,
    MIN(si.occurred_at) AS min_occurred_at,
    MAX(si.occurred_at) AS max_occurred_at,
    MIN(si.created_at) AS first_imported_at,
    MAX(si.created_at) AS last_imported_at
FROM import_jobs AS ij
JOIN source_items AS si ON si.connector_account_id = ij.connector_account_id
WHERE ij.id = :'target_job_id'::uuid
GROUP BY si.connector_account_id, si.source_kind, si.source_state;

-- 11. Rate limit condiviso dell'account (se attivo)
SELECT
    rl.scope_key,
    rl.next_slot_at,
    rl.blocked_until,
    rl.updated_at,
    clock_timestamp() AS db_clock,
    CASE
        WHEN rl.blocked_until IS NOT NULL AND rl.blocked_until > clock_timestamp() THEN (rl.blocked_until - clock_timestamp())
        ELSE NULL
    END AS remaining_blocked_interval
FROM import_jobs AS ij
JOIN gmail_rate_limits AS rl ON rl.connector_account_id = ij.connector_account_id
WHERE ij.id = :'target_job_id'::uuid;

ROLLBACK;
```

---

## 3. Matrice di Decisione Operativa (Decision Table)

In base alle evidenze restituite dalle query di sola lettura, applicare rigorosamente la seguente matrice decisionale.

| Stato Coda (`procrastinate_jobs`) | Stato Worker / Heartbeat | Stato Applicativo (`import_jobs`) | Diagnosi Runtime ([`derive_import_job_runtime()`](../backend/src/gbrain/application/import_job_runtime.py:170)) | Azione Operativa Richiesta |
| :--- | :--- | :--- | :--- | :--- |
| **`todo`** con `scheduled_at > now()` | N/A (non ancora assegnato) | `pending` o `running` | `SCHEDULED_RETRY` o `SCHEDULED_RUN` | **NON INTERVENIRE.** Il job rispetta un backoff transitorio o una finestra programmata per rate limit. Lasciare che la coda proceda spontaneamente. |
| **`todo`** con `scheduled_at <= now()` o `NULL` | N/A | `pending` o `running` | `QUEUED` oppure `BLOCKED` (se `blocker_status` presente) | **NON INTERVENIRE** se `QUEUED` e i worker sono in ascolto. Se `BLOCKED`, il job è in attesa del rilascio del lock da parte di un task precedente: attendere lo sblocco naturale. |
| **`doing`** | Worker presente con `heartbeat_age` < 60s | `running` o `pending` | `RUNNING` (o `CANCELLING` se `cancel_requested_at` valorizzato) | **NON INTERVENIRE ASSOLUTAMENTE.** Il job è attivamente elaborato da un worker vivo. Qualsiasi mutazione creerebbe rischio grave di *split-brain* e corruzione di stato. |
| **`doing`** | Worker `NULL` o `heartbeat_age` > 120s | `running` o `pending` | `RUNNING` (inferenza manuale da DB: stale execution / worker morto o non responsivo) | **STOP ED ESCALATION OPERATIVA.** La funzione [`derive_import_job_runtime()`](../backend/src/gbrain/application/import_job_runtime.py:170) non produce uno stato speciale per worker stallo (restituisce `RUNNING`). La diagnosi di stale execution è un'**inferenza manuale** derivata dall'assenza o anzianità del battito nello snapshot DB (`worker_heartbeat_age` > 120s). **NON eseguire cancellazioni automatiche**, **NON eseguire mutazioni DML su DB** e **NON forzare nuova sync**. Procedere con verifica dello stato del worker e dell'orchestratore (cfr. Sezione 4). |
| **`cancelled`** | N/A | `pending` o `running` | `TERMINAL_MISMATCH` | Incongruenza: la coda è già in stato `cancelled` ma l'applicazione è rimasta attiva (`pending`/`running`). Usare l'endpoint di annullamento [`cancel_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:387): il codice riconosce esplicitamente `snapshot.status == "cancelled"` e riconcilia `import_jobs.status` in `cancelled` con fase `cancelled_reconciled`. Solo dopo avvenuta riconciliazione sarà possibile avviare una nuova sincronizzazione ordinaria. |
| **`succeeded` / `failed` / `aborted`** | N/A | `pending` o `running` | `TERMINAL_MISMATCH` | **STOP ED ESCALATION OPERATIVA (MISMATCH NON RICONCILIABILE TRAMITE CANCEL).** La coda ha terminato l'elaborazione (`succeeded`, `failed`, `aborted`) ma l'applicazione è ancora in stato attivo. L'endpoint [`cancel_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:387) **NON riconcilia né muta l'applicazione** per stati di coda diversi da `cancelled` (lascia lo stato invariato e restituisce il mismatch). **NON avviare una nuova sincronizzazione** per evitare stati incoerenti o sovrapposizioni. **NON eseguire alcun DML manuale su DB**. **NON invocare cancel come riconciliazione**. Attendere un intervento applicativo formale o una procedura correttiva esplicitamente approvata prima di ripartire. |
| **`queue_job_id IS NULL`** (nessun job accodato) | N/A | `pending` o `running` | `MISSING_QUEUE` (`present = NULL`) | Usare l'endpoint di annullamento [`cancel_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:387): l'applicazione rileva che `queue_job_id` è nullo, esegue cancellazione diretta e imposta `import_jobs.status` a `cancelled` con fase/esito applicativo `cancelled_no_queue`. Esito terminale ma diagnosticamente distinto. Solo dopo sarà possibile avviare una nuova sincronizzazione. |
| **Record assente** in coda (`queue_job_id` presente ma assente in Procrastinate) | N/A | `pending` o `running` | `MISSING_QUEUE` (`present = false`) | Usare l'endpoint di annullamento [`cancel_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:387): l'applicazione rileva che il record in coda non esiste più e imposta `import_jobs.status` a `cancelled` con fase/esito applicativo `cancelled_missing_queue`. Esito terminale ma diagnosticamente distinto da `cancelled_no_queue`. Solo dopo sarà possibile avviare una nuova sincronizzazione. |
| **Record presente** ma con args/task discordanti | N/A | Qualsiasi | `TERMINAL_MISMATCH` (mismatch d'identità) | **NON INTERVENIRE CON API NÉ CON DML.** Possibile disallineamento d'identità. Attivare **Escalation Operativa** per indagine manuale d'integrità. |
| Qualsiasi stato | Qualsiasi | `completed`, `completed_with_warnings`, `failed`, `cancelled`, `superseded` | Terminale applicativo coerente | **NON TOCCARE IL VECCHIO JOB.** Il lavoro applicativo è concluso. Se non vi sono lock trattenuti o altri job attivi per l'account, è possibile avviare direttamente una nuova sincronizzazione ordinaria. |

### Quando NON Intervenire
1. **Heartbeat vivo:** se `procrastinate_jobs.status = 'doing'` e il worker aggiorna regolarmente il battito, ogni intervento manuale è vietato.
2. **Backoff programmato:** se il job è in `todo` con data futura, forzare l'esecuzione causerebbe il re-innesco immediato dei limiti Google API.
3. **Lock trattenuto da task legittimo:** non forzare la cancellazione del lock se un altro import storico o continuo è in corso sullo stesso account.
4. **Mismatch terminale non gestito (`succeeded`/`failed`/`aborted`):** non usare l'endpoint cancel per tentare una riconciliazione automatica né avviare nuove sincronizzazioni; fermarsi e attivare escalation.

---

## 4. Procedura di Recupero DOPO il Deploy della Correzione

Il recupero deve avvenire **esclusivamente dopo l'avvenuto deploy della correzione runtime** e solo previa verifica che il vecchio job non sia in esecuzione attiva.

### Regole Rigorose di Sicurezza del Database
- **Nessun DML manuale non convalidato:** non modificare manualmente lo stato da `failed` o `doing` a `todo` via SQL (`UPDATE import_jobs...` o `UPDATE procrastinate_jobs...` sono vietati).
- **Nessuna cancellazione di dati:** non eliminare righe da [`import_jobs`](../backend/src/gbrain/domain/models.py:142), [`source_items`](../backend/src/gbrain/domain/models.py:188), [`source_revisions`](../backend/src/gbrain/domain/models.py:226), [`gmail_checkpoints`](../backend/src/gbrain/domain/models.py:447) né svuotare la coda.
- **Nessun retry manuale del vecchio job:** non reinserire artificialmente il job terminato o fallito nella coda Procrastinate.
- **Conservazione e ripresa dai checkpoint:** la sincronizzazione Gmail del progetto è progettata per riprendere in modo incrementale dai cursori e dai record di copertura già memorizzati, garantendo idempotenza tramite vincoli unici [`uq_source_item_account_identity`](../backend/src/gbrain/domain/models.py:195) e [`uq_source_revision_hash`](../backend/src/gbrain/domain/models.py:230).

### Step 1: Gestione dello Stato e Chiusura del Vecchio Job Tramite API Reale
Utilizzare l'interfaccia utente (pulsante **Annulla** su `SourcesPage`) oppure effettuare una chiamata API autenticata all'endpoint reale di cancellazione implementato in [`cancel_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:387):

```bash
# Definizione delle variabili shell non ambigue (estratte dalla sessione autenticata dell'operatore)
GBRAIN_SESSION_TOKEN="<valore_cookie_sessione>"
GBRAIN_CSRF_TOKEN="<valore_token_csrf>"

# Richiesta di cancellazione cooperativa / riconciliazione del job
# Note di autenticazione e protezione CSRF:
# - Cookie di sessione: nome predefinito 'gbrain_session', personalizzabile esclusivamente tramite la variabile d'ambiente GBRAIN_SESSION_COOKIE_NAME (cfr. session_cookie_name in backend/src/gbrain/config.py:60).
# - Cookie CSRF: nome predefinito 'gbrain_csrf' (richiesto da require_csrf() in backend/src/gbrain/presentation/dependencies.py:68). Nel codice backend non esiste una variabile d'ambiente distinta per il nome del cookie CSRF: il nome 'gbrain_csrf' è cablato.
# - Header CSRF: l'header HTTP 'X-CSRF-Token' deve coincidere con il valore trasmesso nel cookie 'gbrain_csrf' e con l'hash CSRF della sessione.
curl -X POST "http://localhost:8000/api/gmail/jobs/1f983c97-90f5-43b9-a45b-4bed0bb4ea43/cancel" \
  -H "Accept: application/json" \
  -H "Cookie: gbrain_session=${GBRAIN_SESSION_TOKEN}; gbrain_csrf=${GBRAIN_CSRF_TOKEN}" \
  -H "X-CSRF-Token: ${GBRAIN_CSRF_TOKEN}"
```

Comportamento garantito dal codice dell'endpoint ([`cancel_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:387)):
- Se il job è in `todo`, la cancellazione è immediata anche sulla coda Procrastinate via `cancel_job_by_id_async`.
- Se il job è in `doing`, viene impostato durevolmente `cancel_requested_at` consentendo l'uscita cooperativa immediata del worker (cfr. [`poll_cancellation`](../backend/src/gbrain/application/gmail_jobs.py:797)).
- Se il job in coda è già `cancelled`, l'endpoint riconcilia coerentemente [`import_jobs.status`](../backend/src/gbrain/domain/models.py:155) su `cancelled` con fase `cancelled_reconciled`.
- Se `queue_job_id IS NULL`, l'endpoint esegue cancellazione diretta applicativa impostando [`import_jobs.status`](../backend/src/gbrain/domain/models.py:155) su `cancelled` con esito applicativo e fase `cancelled_no_queue`.
- Se `queue_job_id` è presente ma il record Procrastinate è assente (`snapshot is None`), l'endpoint riconcilia e imposta [`import_jobs.status`](../backend/src/gbrain/domain/models.py:155) su `cancelled` con esito applicativo e fase `cancelled_missing_queue`.
- **Distingui diagnostica vs terminalità:** Sia `cancelled_no_queue` sia `cancelled_missing_queue` portano allo stato terminale `cancelled` e consentono di ripartire, ma tracciano in modo diagnosticamente distinto la causa (assenza originaria del puntatore di coda vs riga cancellata/assente in coda dopo l'accodamento).
- **ATTENZIONE:** Se il job in coda si trova in `succeeded`, `failed` o `aborted` con l'applicazione ancora attiva, l'endpoint **NON** muta lo stato applicativo a `cancelled`. In questo scenario non insistere con cancel e non avviare nuove sincronizzazioni: fermarsi e seguire la procedura di escalation (cfr. Sezione 3).

### Step 2: Verifica dello Stato Terminale del Job
Verificare che il job sia passato a uno stato terminale (`cancelled` o `failed`) interrogando l'endpoint di stato [`get_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:367):

```bash
# Endpoint GET di sola lettura: richiede autenticazione di sessione ma non token CSRF
curl -X GET "http://localhost:8000/api/gmail/jobs/1f983c97-90f5-43b9-a45b-4bed0bb4ea43" \
  -H "Accept: application/json" \
  -H "Cookie: gbrain_session=${GBRAIN_SESSION_TOKEN}"
```

### Step 3: Avvio di una Nuova Sincronizzazione Pulita
Una volta verificato che non esistono job attivi o lock in conflitto per l'account, avviare la nuova sincronizzazione tramite interfaccia (pulsante **Aggiorna ora** su `SourcesPage`) oppure tramite l'endpoint reale dedicato in [`gmail_sync_continuous_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:727):

```bash
# Avvio sincronizzazione incrementale / continua via endpoint dedicato Gmail
curl -X POST "http://localhost:8000/api/gmail/sync/continuous" \
  -H "Accept: application/json" \
  -H "Cookie: gbrain_session=${GBRAIN_SESSION_TOKEN}; gbrain_csrf=${GBRAIN_CSRF_TOKEN}" \
  -H "X-CSRF-Token: ${GBRAIN_CSRF_TOKEN}"
```

*(In alternativa, per la sincronizzazione tramite Sources API generica:* [`trigger_source_sync()`](../backend/src/gbrain/presentation/sources_api.py:704) *su `POST /api/sources/{source_id}/sync`):*

```bash
SOURCE_ID="<identificatore_univoco_source>"

curl -X POST "http://localhost:8000/api/sources/${SOURCE_ID}/sync" \
  -H "Accept: application/json" \
  -H "Cookie: gbrain_session=${GBRAIN_SESSION_TOKEN}; gbrain_csrf=${GBRAIN_CSRF_TOKEN}" \
  -H "X-CSRF-Token: ${GBRAIN_CSRF_TOKEN}"
```

La nuova esecuzione:
1. Carica il checkpoint persistente da [`gmail_checkpoints`](../backend/src/gbrain/domain/models.py:447).
2. Se la cronologia risulta valida, esegue `incremental_sync` a partire da `last_history_id`.
3. Se la cronologia fosse scaduta (`gmail_history_expired`), il motore richiede riconciliazione automatica senza perdita di integrità (cfr. [`GmailSyncEngine.sync()`](../backend/src/gbrain/application/gmail_sync.py:344)).
4. Gli elementi già salvati non vengono duplicati.

### Gestione Varianti Patologiche ed Escalation
Qualora il job rimanesse bloccato in stato `doing` per via di un crash anomalo del worker (senza che l'heartbeat venga più aggiornato e senza che il segnale cooperativo possa essere elaborato) e l'endpoint di cancellazione non liberasse il lock di coda:
- **NON intervenire con cancellazioni DML dirette.**
- Non cancellare automaticamente lo stato `doing` stale.
- Procedere con la verifica del container/servizio del worker e dell'orchestratore.
- Effettuare un riavvio controllato del container worker:
  ```bash
  docker compose restart worker
  ```
- All'avvio, il framework Procrastinate esegue la manutenzione dei worker stallo (`procrastinate_prune_stalled_workers_v1`), ripulendo i worker deceduti e consentendo lo sblocco dei task orfani in modo trasparente e coerente con il protocollo infrastrutturale.

---

## 5. Verifiche Post-Recupero Read-Only

Dopo l'esecuzione del recupero e l'avvio della nuova sincronizzazione, eseguire su PostgreSQL il seguente script di controllo per confermare il successo delle operazioni.
Lo script è rigorosamente in sola lettura (`REPEATABLE READ READ ONLY`), termina con `ROLLBACK`, e opera esclusivamente con filtri mirati al job e all'account interessato (`:'old_job_id'::uuid`).

```sql
\set ON_ERROR_STOP on
\set old_job_id '1f983c97-90f5-43b9-a45b-4bed0bb4ea43'

BEGIN TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY;

-- V1. Conferma che il vecchio job sia terminale e non più in elaborazione
SELECT
    id AS old_job_id,
    status::text AS old_job_status,
    cancel_requested_at,
    finished_at,
    error_code,
    processed_items,
    imported_items
FROM import_jobs
WHERE id = :'old_job_id'::uuid;

-- V2. Ispezione dei nuovi job accodati per lo stesso account
SELECT
    ij.id AS new_job_id,
    ij.mode,
    ij.status::text AS new_job_status,
    ij.queue_job_id,
    ij.created_at,
    ij.started_at,
    ij.finished_at,
    pj.status::text AS queue_status,
    pj.attempts,
    pj.scheduled_at
FROM import_jobs AS ij
LEFT JOIN procrastinate_jobs AS pj ON pj.id = ij.queue_job_id
WHERE ij.connector_account_id = (
    SELECT connector_account_id FROM import_jobs WHERE id = :'old_job_id'::uuid
)
  AND ij.id <> :'old_job_id'::uuid
ORDER BY ij.created_at DESC
LIMIT 5;

-- V3. Verifica stato del checkpoint persistente
-- NOTA OPERATIVA SU MONOTONIA: una singola interrogazione puntuale non dimostra la monotonia.
-- L'operatore deve formalmente verificare che 'checkpoint_version' e 'last_sync_at' siano
-- maggiori o uguali alla baseline registrata durante l'ispezione diagnostica iniziale (Query 7 della Sezione 2),
-- oppure confrontare due campionamenti temporali successivi (checkpoint_version_t2 >= checkpoint_version_t1).
SELECT
    gc.connector_account_id,
    gc.version AS checkpoint_version,
    gc.config_revision,
    gc.reconciliation_state::text AS reconciliation_state,
    gc.last_error_code,
    gc.last_sync_at,
    gc.updated_at
FROM gmail_checkpoints AS gc
WHERE gc.connector_account_id = (
    SELECT connector_account_id FROM import_jobs WHERE id = :'old_job_id'::uuid
);

-- V4. Verifica integrità ed assenza duplicati sui dati importati
-- Il controllo rispetta la chiave unica reale ('connector_account_id', 'source_kind', 'external_id')
-- definita dal vincolo 'uq_source_item_account_identity'. Per evitare alterazioni cardinali derivanti
-- da join, il conteggio delle revisioni è isolato in una subquery. L'assenza di duplicati è verificata
-- tramite una sottoquery che raggruppa su tutte e tre le colonne della chiave composta e restituisce
-- esclusivamente il conteggio aggregato dei duplicati, senza esporre 'external_id' o alcuna PII.
SELECT
    si.connector_account_id,
    COUNT(*) AS total_items,
    COUNT(DISTINCT si.id) AS total_distinct_items,
    (
        SELECT COUNT(*)
        FROM source_revisions AS sr
        JOIN source_items AS si_sub ON si_sub.id = sr.source_item_id
        WHERE si_sub.connector_account_id = si.connector_account_id
    ) AS total_revisions,
    COALESCE((
        SELECT SUM(dup.duplicate_count)
        FROM (
            SELECT COUNT(*) - 1 AS duplicate_count
            FROM source_items AS si_dup
            WHERE si_dup.connector_account_id = si.connector_account_id
            GROUP BY si_dup.connector_account_id, si_dup.source_kind, si_dup.external_id
            HAVING COUNT(*) > 1
        ) AS dup
    ), 0) AS duplicate_items_count
FROM source_items AS si
WHERE si.connector_account_id = (
    SELECT connector_account_id FROM import_jobs WHERE id = :'old_job_id'::uuid
)
GROUP BY si.connector_account_id;

-- V5. Verifica infrastrutturale coda: assenza di lock trattenuti o stati 'doing' sul vecchio job e concorrenti
SELECT
    pj.id AS procrastinate_job_id,
    pj.queue_name,
    pj.task_name,
    pj.lock,
    pj.status::text AS procrastinate_status,
    pj.worker_id,
    pw.last_heartbeat AS worker_last_heartbeat,
    CASE
        WHEN pw.last_heartbeat IS NOT NULL THEN (clock_timestamp() - pw.last_heartbeat)
        ELSE NULL
    END AS worker_heartbeat_age,
    EXISTS (
        SELECT 1
        FROM procrastinate_jobs AS other_jobs
        WHERE other_jobs.lock = pj.lock
          AND other_jobs.id <> pj.id
          AND (
              other_jobs.status = 'doing'
              OR (
                  other_jobs.status = 'todo'
                  AND (
                      other_jobs.priority > pj.priority
                      OR (other_jobs.priority = pj.priority AND other_jobs.id < pj.id)
                  )
              )
          )
    ) AS is_blocked_by_lock
FROM import_jobs AS ij
JOIN procrastinate_jobs AS pj ON pj.id = ij.queue_job_id
LEFT JOIN procrastinate_workers AS pw ON pw.id = pj.worker_id
WHERE ij.id = :'old_job_id'::uuid;

-- V6. Verifica stato della copertura finale (gmail_coverages) in query separata
SELECT
    c.status AS coverage_status,
    c.has_gaps,
    c.missing_count,
    c.last_completed_at
FROM gmail_coverages AS c
WHERE c.connector_account_id = (
    SELECT connector_account_id FROM import_jobs WHERE id = :'old_job_id'::uuid
);

ROLLBACK;
```

---

## 6. Riferimenti di Codice e Architettura

- **Modelli Dati e Vincoli Unici:**
  - Definizione entità [`ImportJob`](../backend/src/gbrain/domain/models.py:142) e stati [`ImportJobStatus`](../backend/src/gbrain/domain/models.py:46)
  - Modello account [`ConnectorAccount`](../backend/src/gbrain/domain/models.py:351)
  - Checkpoint e stato sincronizzazione [`GmailCheckpoint`](../backend/src/gbrain/domain/models.py:447)
  - Stato backfill e copertura [`GmailBackfillState`](../backend/src/gbrain/domain/models.py:514) e [`GmailCoverage`](../backend/src/gbrain/domain/models.py:548)
  - Diagnostica lacune per-item [`GmailItemGap`](../backend/src/gbrain/domain/models.py:579)
  - Rate limiting condiviso [`GmailRateLimit`](../backend/src/gbrain/domain/models.py:664)
  - Dati importati e revisioni [`SourceItem`](../backend/src/gbrain/domain/models.py:188) e [`SourceRevision`](../backend/src/gbrain/domain/models.py:226)
- **Runtime e Integrazione Coda:**
  - Snapshot coerente di coda [`fetch_procrastinate_job_snapshot()`](../backend/src/gbrain/infrastructure/procrastinate_jobs.py:123)
  - Diagnosi runtime unificata [`derive_import_job_runtime()`](../backend/src/gbrain/application/import_job_runtime.py:170)
  - Controllo coerenza di identità [`is_queue_identity_coherent()`](../backend/src/gbrain/application/import_job_runtime.py:147)
  - Esecuzione worker e cooperatività [`execute_gmail_job()`](../backend/src/gbrain/application/gmail_jobs.py:756)
  - Motore di sincronizzazione e ripresa [`GmailSyncEngine`](../backend/src/gbrain/application/gmail_sync.py:225)
- **API Router Reali:**
  - Annullamento cooperativo job: [`cancel_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:387) (`POST /api/gmail/jobs/{job_id}/cancel`)
  - Lettura stato e runtime job: [`get_job_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:367) (`GET /api/gmail/jobs/{job_id}`)
  - Trigger sincronizzazione continua: [`gmail_sync_continuous_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:727) (`POST /api/gmail/sync/continuous`)
  - Trigger import storico: [`gmail_import_history_endpoint()`](../backend/src/gbrain/presentation/gmail_api.py:690) (`POST /api/gmail/history/import`)
  - Trigger sync generico: [`trigger_source_sync()`](../backend/src/gbrain/presentation/sources_api.py:704) (`POST /api/sources/{source_id}/sync`)
- **Documentazione Correlata:**
  - Modello dati relazionale e grafo: [`MODELLO_DATI.md`](MODELLO_DATI.md)
  - Specifiche piano Gmail Fase 2: [`PIANO_FASE2_GMAIL.md`](PIANO_FASE2_GMAIL.md)
  - Procedure operative e manutenzione LXC: [`OPERAZIONI_LXC.md`](OPERAZIONI_LXC.md)

# PROJECT STATUS — checkpoint ufficiale riprendibile

**Data checkpoint:** 11 ottobre 2026  
**Checkpoint:** Sottocompito E — Audit finale della Fase 2 Gmail, consolidamento test e checkpoint riprendibile  
**Fase corrente:** Fase 0 consolidata; Fondazione Fase 1 verificata staticamente/localmente; Fase 2 Gmail `SENT` completata a livello di contratto, nucleo, OAuth Web, cifratura, worker Procrastinate, FTS, API, UI Fonti e suite test di audit.  
**Giudizio di prontezza:** codice Fase 1 e Fase 2 pronto per il collaudo infrastrutturale e configurazione credenziali; non pronta per dichiarare integrazione reale con account personali o deployment in produzione.  
**Non è una dichiarazione di MVP finale né di integrazione con servizi personali.**

## Regola di lettura dello stato

Questo checkpoint distingue rigorosamente:

- **codice predisposto:** file, schemi, API e UI esistono, sono stati revisionati trasversalmente e allineati;
- **test deterministico superato:** un comando locale eseguito senza rete né servizi esterni ha restituito exit 0 (93 test superati);
- **test integrazione pendente:** test marcati esplicitamente con `@pytest.mark.skip` (3 test) che richiedono demone Docker Linux, PostgreSQL reale con estensioni o credenziali Google Cloud autorizzate;
- **integrazione reale non eseguita:** non eseguito collaudo runtime con account Google reale o container Docker su Linux host.

Un elemento predisposto o coperto da fixture/fake non equivale a un collaudo runtime. I rapporti precedenti ([`docs/RAPPORTO_COLLAUDO_FASE1_2026-10-11.md`](docs/RAPPORTO_COLLAUDO_FASE1_2026-10-11.md), sottocompiti A–D) rimangono storici; la sintesi ufficiale corrente della Fase 2 è questo documento.

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
| **UI Fonti Gmail** | **RAGGIUNTO** | Componente React [`GmailSourcesPage.tsx`](frontend/src/GmailSourcesPage.tsx) con indicatori di stato (non configurato / disconnesso / connesso), configurazione policy, preview, avanzamento job, gestione alias, ricerca e citazioni. Build verificata con Vite. |
| **Schema e Migrazioni DB** | **RAGGIUNTO** | Tre migrazioni Alembic incrementali e reversibili (`0001_initial_infrastructure`, `0002_gmail_sent_core`, `0003_gmail_operational`); nessun ritocco a migrazioni storiche; perfetta coerenza tra modelli ORM SQLAlchemy e DDL migrazioni. |
| **Collaudo runtime Docker/Linux reale** | **PENDENTE** | Richiede demone Docker Linux operativo su host di prova (in ambiente di sviluppo locale la named pipe Docker Desktop Linux era assente). |
| **Collaudo Gmail reale Google Cloud** | **PENDENTE** | Richiede credenziali Google Cloud di prova configurate e consenso esplicito su casella reale; intenzionalmente non eseguito per evitare uso di credenziali o dati non autorizzati. |
| **Esecuzione Procrastinate su DB reale** | **PENDENTE** | Richiede PostgreSQL reale con estensioni e migrazioni applicate per validare il lock advisory/tabella Procrastinate. |

---

## Difetti trovati e corretti durante l'audit

1. **Enforcement alias obbligatorio su avvio importazione (`start_job_endpoint` in [`backend/src/gbrain/presentation/gmail_api.py`](backend/src/gbrain/presentation/gmail_api.py:300)):**
   - *Problema:* L'endpoint `/api/gmail/jobs` verificava la corrispondenza della policy ma non controllava esplicitamente che `account.confirmed_aliases` fosse non vuoto prima di accodare un lavoro di importazione (`full`, `incremental`, `reconciliation`).
   - *Correzione:* Aggiunto controllo preventivo che solleva `GmailServiceError("alias_required", ...)` (HTTP 400) se nessun alias è confermato.
2. **Enforcement alias nel job worker (`execute_gmail_job` in [`backend/src/gbrain/application/gmail_jobs.py`](backend/src/gbrain/application/gmail_jobs.py:434)):**
   - *Problema:* Il worker procedeva all'inizializzazione del connettore anche con alias vuoti se invocato direttamente fuori dalla preview.
   - *Correzione:* Aggiunto controllo di sicurezza a difesa in profondità che solleva `GmailServiceError("alias_required", ...)` bloccando l'esecuzione dell'importazione.
3. **Completamento copertura rotte private nell'audit (`test_private_gmail_routes_require_auth_and_mutations_require_csrf` in [`backend/tests/test_gmail_operational.py`](backend/tests/test_gmail_operational.py:150)):**
   - *Problema:* Il test di sicurezza sulle route del router Gmail non includeva esplicitamente `/api/gmail/aliases/{alias}` (DELETE) tra le mutazioni protette da CSRF né `/api/gmail/sources/{source_item_id}/live-evidence` (GET) tra le route protette da Principal.
   - *Correzione:* Integrate entrambe le rotte nella matrice di controllo, verificando che tutte le 14 route registrate sul router Gmail applichino le dipendenze di autenticazione e CSRF prescritte.

---

## Risultati dei controlli eseguiti

Tutti i controlli deterministici sono stati eseguiti localmente con esito positivo:

- **Pytest:** `uv run pytest backend/tests -v`
  - **93 test superati**, **3 test marcati esplicitamente pendenti/skipped** (collaudo Docker/PostgreSQL reale, Procrastinate reale, Google Cloud reale).
  - 0 fallimenti, 0 errori.
- **Ruff linter:** `uv run ruff check backend`
  - **Superato:** 0 errori, 0 avvisi.
- **Ruff formatter:** `uv run ruff format --check backend`
  - **Superato:** 46 file formattati in modo conforme.
- **Mypy type check:** `uv run mypy backend/src backend/tests`
  - **Superato:** Successo su tutti i 42 file sorgente, 0 errori di tipo.
- **Frontend TypeScript & Build:** `npm --prefix frontend run build`
  - **Superato:** Type check `tsc -b` superato, build Vite completata in 83 ms senza errori.

---

## File modificati e creati nel Sottocompito E

- **Creati:**
  - [`backend/tests/test_gmail_audit_fase2.py`](backend/tests/test_gmail_audit_fase2.py) — Suite di audit consolidata con 28 nuovi test specifici per OAuth, cifratura, alias, SENT invariant, MIME, sync engine, API CSRF, budget e 3 test pendenti per integrazione reale.
- **Modificati:**
  - [`backend/src/gbrain/presentation/gmail_api.py`](backend/src/gbrain/presentation/gmail_api.py) — Correzione enforcement alias obbligatorio su avvio lavoro di importazione.
  - [`backend/src/gbrain/application/gmail_jobs.py`](backend/src/gbrain/application/gmail_jobs.py) — Correzione enforcement alias a difesa in profondità nel worker task.
  - [`backend/tests/test_gmail_operational.py`](backend/tests/test_gmail_operational.py) — Estensione copertura completa rotte private e mutazioni CSRF.
  - [`docs/PIANO_FASE2_GMAIL.md`](docs/PIANO_FASE2_GMAIL.md) — Aggiornamento stato sequenza di delivery, chiusura sottocompiti A–E e marcatura gate runtime pendenti.
  - [`docs/SICUREZZA.md`](docs/SICUREZZA.md) — Aggiornamento architettura di sicurezza con stato consolidato Fase 2.
  - [`docs/CONNETTORI.md`](docs/CONNETTORI.md) — Allineamento stato Fase 2 e contratti operativi.
  - [`README.md`](README.md) — Allineamento descrizione repository a Fase 1 + Fase 2 Gmail deterministica completata.
  - [`PROJECT_STATUS.md`](PROJECT_STATUS.md) — Checkpoint ufficiale conclusivo Fase 2.

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

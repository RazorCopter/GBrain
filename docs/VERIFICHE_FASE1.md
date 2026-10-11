# Verifiche tecniche Fase 1

Data audit originario: 10 ottobre 2026. Revisione finale indipendente e sottocompito A: 11 ottobre 2026.

> **Checkpoint sottocompito A.** Il daemon Docker Linux è risultato ancora non operativo (`docker info`: named pipe `dockerDesktopLinuxEngine` assente; `docker desktop status`: Docker Desktop non in esecuzione). Per esplicita richiesta non è stato avviato. Il collaudo runtime non è quindi dichiarato superato. Evidenze complete, capacità host, comandi ed esiti sono in [`RAPPORTO_COLLAUDO_FASE1_2026-10-11.md`](RAPPORTO_COLLAUDO_FASE1_2026-10-11.md); la procedura riproducibile è [`scripts/phase1_runtime_test.sh`](../scripts/phase1_runtime_test.sh).

## Perimetro

Audit limitato alla fondazione Fase 1: configurazione, secret file, autenticazione single-owner, sessioni/cookie/logout/CSRF, route private, schema e migrazione iniziale, health, worker Procrastinate, fixture demo, Dockerfile e profilo Compose `compact`. Non sono stati implementati connettori, memoria autobiografica o servizi ulteriori.

## Esito sintetico

| Area | Esito | Evidenza |
|---|---|---|
| Ruff lint | SUPERATO | exit 0, tutti i controlli superati |
| Ruff format check | SUPERATO DOPO CORREZIONE | exit 0, 27 file formattati |
| mypy strict | SUPERATO | exit 0, 20 file sorgente |
| pytest | SUPERATO | exit 0, 26 test; 1 warning dipendenza |
| TypeScript type-check | SUPERATO | exit 0 |
| Build Vite produzione | SUPERATO | exit 0, 16 moduli trasformati |
| Compose config profilo compact isolato | SUPERATO | exit 0, project `gbrain_phase1_a_20261011`, porte 18080/18081 libere |
| Docker daemon | NON DISPONIBILE | client 29.8.2 e Compose 5.5.1 presenti; `docker info` exit 1, named pipe Docker Desktop assente |
| Build immagine | NON ESEGUITO | richiede daemon Linux operativo |
| Integrazione PostgreSQL/Procrastinate | NON ESEGUITA | richiede daemon Linux operativo |
| Backup/restore e guasto controllato | NON ESEGUITO | script one-shot e hook test-only predisposti; richiede daemon Linux operativo |
| Misure container/immagini/volumi | NON ESEGUITE | server Docker non raggiungibile; nessun numero inventato |

## Difetti riprodotti e correzioni

1. **CSRF double-submit incompleto.** La dipendenza verificava l'header contro l'hash server-side, ma non richiedeva né confrontava il cookie leggibile dal frontend. È stato aggiunto il confronto constant-time header/cookie prima della verifica contro l'hash di sessione. I test coprono cookie assente, header assente e valori discordanti.
2. **Format check non deterministico/fallito.** La migrazione iniziale e altri file Python esistenti non rispettavano il formatter configurato. È stato applicato Ruff format all'albero backend; il check finale passa.
3. **Vincolo outbox senza nome coerente nel modello ORM.** La migrazione usa `uq_outbox_stable_key`, mentre `unique=True` nel modello produceva un vincolo anonimo nei metadati. Il modello ora dichiara esplicitamente lo stesso nome, senza modifica dello schema fisico.
4. **Configurazione produzione consentiva URL pubblico HTTP.** Oltre al cookie Secure già obbligatorio, la configurazione production ora rifiuta `GBRAIN_PUBLIC_URL` non HTTPS.
5. **Copertura Fase 1 insufficiente.** La suite è passata da 14 a 26 test e ora include hashing Argon2id, attributi cookie, cancellazione cookie, revoca logout server-side, CSRF double-submit, health live/ready, vincoli single-owner/outbox, redazione log, TTL sessione e URL HTTPS in produzione. Restano demandati a PostgreSQL reale bootstrap concorrente, migrazione e persistenza.
6. **Backup/restore incompatibile con project name isolati.** Il manifest era hardcoded su `gbrain`; ora [`scripts/backup.sh`](../scripts/backup.sh) registra `COMPOSE_PROJECT_NAME` e [`scripts/restore.sh`](../scripts/restore.sh) convalida `GBRAIN_RESTORE_SOURCE_PROJECT`, imponendo un target diverso dal progetto sorgente.
7. **Guasto controllato non iniettabile.** Aggiunto `GBRAIN_BACKUP_TEST_FAIL_AFTER_QUIESCE=true`, disabilitato per default: fallisce dopo la quiescenza e prima dei dump, esercitando il trap di recovery senza corrompere dati.

## Copertura deterministica presente

- Secret file: lettura, newline finale, sorgenti duplicate, secret corto e valori mancanti.
- Produzione: cookie Secure, URL HTTPS, demo vietata.
- Password/sessione: Argon2id verificabile, token sessione e CSRF memorizzati come digest, TTL configurato.
- CSRF: header, cookie double-submit e digest server-side devono concordare.
- Logout: revoca server-side, commit e scadenza di entrambi i cookie.
- Route privata/demo: accesso anonimo rifiutato e demo non elude autenticazione/CSRF.
- Demo: default off, rifiuto prima dell'accesso DB, dataset deterministico, seed idempotente, reset confinato e record non-demo preservato nel fake deterministico.
- Health: liveness indipendente; PostgreSQL indisponibile rende readiness 503; Neo4j è riportato separatamente senza rendere non-ready PostgreSQL.
- Schema statico: slot unico owner, UUID minimi outbox, lunghezza stable key e vincolo univoco nominato.
- Log: redazione di valori sensibili per messaggi strutturati a dizionario.

## Revisione Docker, Compose e Windows → Linux

- Immagini base e servizi infrastrutturali sono pinning per versione e digest; non risultano tag `latest`.
- Il frontend è compilato nello stage Node e copiato in `/app/frontend/dist`; il runtime non include un server dev Node.
- Runtime applicativo non-root UID/GID 10001; directory persistenti create e assegnate all'utente applicativo.
- `entrypoint.sh` ha shebang POSIX, usa `/bin/sh`, viene reso eseguibile nel Dockerfile e ha terminatori LF.
- I bind mount dati/cache/backup devono essere scrivibili da UID 10001 sull'host Linux/NAS; questa condizione non è verificabile su Windows e rimane un rischio operativo.
- Il mount import è read-only. Solo la porta web è pubblicata, per default su loopback; PostgreSQL e Neo4j restano sulla rete backend.
- Tutti i servizi hanno limiti CPU/memoria e healthcheck. La concorrenza worker è fissata a 1 sia dalla configurazione Compose sia dalla validazione applicativa.
- `compose config` conferma sintassi e interpolazione, ma non prova healthcheck, cgroup, permessi mount o disponibilità immagini.
- File YAML/Dockerfile possono avere CRLF nel checkout Windows, ma non sono eseguiti come script. `entrypoint.sh` è LF; lo script Python con CRLF è interpretato da Python e non avviato tramite shebang.

## Comandi realmente eseguiti

### Baseline

- `uv run ruff check backend && uv run ruff format --check backend && uv run mypy backend/src backend/tests && uv run pytest -q` → exit 1: lint superato, format fallito; la catena si è fermata prima di mypy/pytest.
- `uv run mypy backend/src backend/tests` → exit 0, 23 file.
- `uv run pytest -q` → exit 0, 14 test.
- `npm run typecheck && npm run build` → exit 0.
- `docker version && docker compose --profile compact config --quiet` → exit 1 su connessione daemon.
- `docker compose --profile compact config --quiet` eseguito separatamente → exit 0.
- `git status --short && git ls-files --eol ...` → exit 128: la directory fornita non contiene metadati Git.

### Correzione e finale

- `uv run ruff format backend` → exit 0.
- `uv run ruff check backend && uv run ruff format --check backend && uv run mypy backend/src backend/tests && uv run pytest -q` → exit 0: 26 file formattati, mypy su 24 file, 26 test passati; un warning Starlette su deprecazione `TestClient`/httpx.
- `npm run typecheck && npm run build` → exit 0; build Vite produzione riuscita, 16 moduli.
- `docker compose --profile compact config --quiet` → exit 0.
- `docker info` → exit 1: client presente, daemon Linux Docker Desktop non raggiungibile.

## Revisione finale indipendente — 11 ottobre 2026

### Giudizio

La fondazione è **pronta per prosecuzione controllata e test deterministici della Fase 2**, ma **non è integralmente collaudata né pronta per un deployment reale**. Codice predisposto, test deterministici e integrazione reale restano categorie separate. Il gate runtime Fase 1 rimane aperto finché migrazione, schema Procrastinate, persistenza, worker, healthcheck, limiti, mount e backup/restore non vengono provati con Docker su Linux/LXC reale.

### Esiti realmente rieseguiti

| Check | Esito reale | Evidenza |
|---|---|---|
| Ruff lint | SUPERATO | exit 0, tutti i controlli superati |
| Ruff format check | SUPERATO | exit 0, 26 file già formattati |
| mypy strict | SUPERATO | exit 0, 24 file sorgente |
| pytest | SUPERATO CON WARNING | exit 0, 26 test in 0,90 s; 1 warning di deprecazione Starlette/TestClient-httpx |
| TypeScript type-check | SUPERATO | exit 0 |
| Build Vite produzione | SUPERATO | exit 0, 16 moduli; output sotto `frontend/dist` ignorato |
| Compose config `compact` | SUPERATO | exit 0 |
| Parser POSIX `sh -n` | SUPERATO | exit 0 per entrypoint, backup e restore, via stdin alla shell WSL `docker-desktop`; non è una prova d'esecuzione |
| Coerenza documentale/statica | SUPERATO DOPO CORREZIONI | riferimenti Markdown locali, marcatori conflitto, assenza tag `latest`, assenza `*.tsbuildinfo`, LF degli script; controllo finale post-documentazione exit 0 |
| Docker daemon | NON DISPONIBILE | `docker info` exit 1; client 29.8.2/Compose 5.5.1 presenti, Server non raggiungibile |
| Build/avvio/runtime/backup/restore | NON ESEGUITO | daemon indisponibile; nessun container avviato |

Un primo controllo documentale aveva incluso dipendenze vendorizzate sotto `frontend/node_modules` e ha fallito su un link interno upstream di `fdir`; non è evidenza sul repository. Il controllo è stato corretto limitandolo ai documenti e file di progetto ed è poi passato.

### Correzioni della revisione finale

1. Rimossi `frontend/tsconfig.app.tsbuildinfo` e `frontend/tsconfig.node.tsbuildinfo`, artefatti generati già coperti da `.gitignore`; il build li rigenera localmente e il controllo finale li rimuove.
2. Corretto l'ADR storico che sovradichiarava `pg_dump` come già sufficiente a ricostruire Neo4j al 100%: sink e rebuild non esistono ancora, quindi il dump Neo4j resta necessario.
3. Marcato `MODELLO_DATI.md` come modello target Fase 0 e distinto dalle tabelle fisiche effettivamente presenti nella migrazione Fase 1.
4. Esplicitato nel README che il doppio `alembic upgrade head` è serializzato soltanto dall'attuale dipendenza app→worker e non autorizza repliche concorrenti senza coordinamento migrazioni.
5. Creato il backlog ristretto Gmail `SENT` in `docs/PIANO_FASE2_GMAIL.md`, senza implementazione né accessi reali.

### Rischi statici confermati, non corretti inventando runtime

- Il healthcheck worker verifica la vita di PID 1, non il consumo effettivo della coda.
- Il doppio entrypoint di migrazione dipende dall'ordinamento Compose corrente; l'idempotenza/concorrenza va provata su PostgreSQL reale.
- I bind mount richiedono UID/GID e permessi verificati sul target; la creazione directory nel layer immagine non garantisce i bind host.
- `forwarded_allow_ips="*"` è accettabile solo se la porta app è limitata a loopback/proxy fidato come documentato; non equivale a una configurazione proxy di produzione provata.
- Non sono implementati rate limiting/MFA/CSP/HSTS applicativi; i limiti CPU/memoria dichiarati in Compose non sono ancora osservati dal runtime.
- Backup/restore hanno difese statiche (conferma esatta, whitelist, checksum, lock, stack fermo), ma compatibilità CLI Neo4j, target realmente nuovo e isolamento restano da provare.

## Verifiche non eseguite

Per indisponibilità del daemon Docker non sono stati dichiarati né simulati:

- build multi-stage dell'immagine;
- avvio end-to-end dei quattro container;
- migrazione Alembic da PostgreSQL vuoto;
- presenza e funzionamento effettivo dello schema Procrastinate;
- persistenza dopo riavvio app/worker;
- consumo reale worker con concorrenza 1;
- seed/reset/idempotenza fixture su PostgreSQL reale;
- healthcheck osservati da Docker;
- limiti cgroup e permessi bind mount reali.

Sono inoltre fuori ambito o privi di accessi: NAS/LXC, Gmail, Immich, OmniRoute, Graphiti, backup/restore e compatibilità con dati personali.

## Rischi residui

1. Bootstrap unico e vincoli concorrenti sono progettati con advisory lock PostgreSQL e vincoli DB, ma la prova concorrente richiede integrazione reale.
2. Il filtro log redige dizionari con chiavi sensibili; testo libero prodotto da librerie terze non può essere garantito privo di segreti. L'applicazione disabilita l'access log Uvicorn e non registra payload di autenticazione.
3. L'healthcheck worker basato su segnale al PID 1 prova che il processo esiste, non che stia consumando job o aggiornando heartbeat.
4. La readiness considera PostgreSQL autorevole e Neo4j degradabile: scelta coerente con la fondazione, ma non prova la proiezione graph.
5. Rimane un warning di deprecazione Starlette relativo al backend `TestClient`; non è stato alterato il lockfile senza una correzione upstream coerente.

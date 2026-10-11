# PROJECT STATUS — checkpoint ufficiale riprendibile

**Data checkpoint:** 11 ottobre 2026
**Checkpoint:** sottocompito B — audit comparativo Gmail MCP vs client ufficiale e decisione vincolante sincronizzatore
**Fase corrente:** Fase 0 consolidata; fondazione Fase 1 verificata staticamente e localmente; collaudo Docker/Linux reale ancora pendente. Audit sincronizzatore Gmail completato; client ufficiale scelto.
**Giudizio di prontezza:** pronta per implementazione fixture-first del contratto Gmail Fase 2. Non pronta per dichiarare Fase 1 integralmente collaudata o per un deployment reale.
**Non è una dichiarazione di MVP finale né di integrazione con servizi personali.**

## Regola di lettura dello stato

Questo checkpoint distingue rigorosamente:

- **codice predisposto:** file e configurazione esistono e sono stati revisionati staticamente;
- **test deterministico superato:** un comando locale senza servizi personali ha restituito exit 0;
- **integrazione reale non eseguita:** richiede Docker daemon Linux operativo; il client era presente ma la named pipe `dockerDesktopLinuxEngine` era assente.

Un elemento predisposto o coperto da fake non equivale a un collaudo runtime. Il rapporto ufficiale del sottocompito è [`docs/RAPPORTO_COLLAUDO_FASE1_2026-10-11.md`](docs/RAPPORTO_COLLAUDO_FASE1_2026-10-11.md).

## Checkpoint sottocompito A

- Host osservato: Windows 11 x64, 16 CPU logiche, 51.206.234.112 byte RAM fisica; WSL2 2.7.3/kernel 6.6 disponibile.
- Docker client 29.8.2, Compose 5.5.1 e contesto `desktop-linux` presenti; server non raggiungibile. Per richiesta esplicita Docker Desktop non è stato avviato.
- Config Compose isolata validata con project `gbrain_phase1_a_20261011`, credenziali sintetiche e porte 18080/18081 libere. Nessun artefatto Docker creato.
- Check locali superati: Ruff lint/format, mypy strict, 26 pytest, TypeScript e build Vite.
- Corretto il project name hardcoded nel manifest backup/restore; il restore impone ora un progetto target diverso dal sorgente.
- Aggiunto hook test-only `GBRAIN_BACKUP_TEST_FAIL_AFTER_QUIESCE`, disabilitato per default.
- Aggiunto [`scripts/phase1_runtime_test.sh`](scripts/phase1_runtime_test.sh): build, stack source/restore separati, migrazioni, schema/job Procrastinate, auth/CSRF, demo idempotente, restart, misure, backup con fault injection, restore e cleanup limitato ai project `gbrain_phase1_*`.
- Build immagine, runtime applicativo, misure container e backup/restore restano **NON ESEGUITI**, non falliti né simulati.

## Perimetro implementato nel repository

- Immagine multi-stage: build React/TypeScript con Node e runtime Python/FastAPI non-root UID/GID `10001:10001`.
- Stack Compose `compact`: app, worker dalla stessa immagine, PostgreSQL 16.10 e Neo4j Community 5.26.12; soltanto il web è pubblicato per default su loopback.
- Reti edge/backend separate; PostgreSQL e Neo4j senza porte host; rete backend interna.
- Quattro bind mount applicativi distinti: data, cache, import read-only e backup; datastore in volumi nominati separati.
- Secret file Compose per PostgreSQL, autenticazione e Neo4j; configurazione rifiuta secret mancanti/duplicati e impone almeno 32 caratteri al secret auth.
- Autenticazione single-owner: bootstrap transazionale, Argon2id, sessioni server-side hashate, cookie HttpOnly/SameSite Strict, cookie CSRF double-submit e logout/revoca.
- API private protette da sessione; operazioni mutative correnti protette da CSRF. Swagger/OpenAPI pubblici disabilitati.
- UI italiana e SPA statica servita da FastAPI; asset limitati alla directory risolta e route private della SPA sottoposte a sessione.
- PostgreSQL autorevole con SQLAlchemy/Alembic, provenance/revisioni, import job/checkpoint, esclusioni, cancellazione logica, outbox e schema Procrastinate incluso nella migrazione iniziale.
- Worker Procrastinate a concorrenza uno: task heartbeat, pulizia sessioni e consumer outbox placeholder; nessun sink Neo4j e nessun Graphiti.
- Demo sintetica deterministica opt-in, vietata in production e confinata al namespace demo; non aggira auth/CSRF e non apre connessioni reali.
- Health live/ready: PostgreSQL determina readiness; Neo4j è riportato separatamente; Graphiti risulta `not_implemented`.
- Dipendenze bloccate in `uv.lock` e `frontend/package-lock.json`; immagini base/datastore pin per versione e digest, senza `latest`.
- Script POSIX backup/restore con quiescenza, dump PostgreSQL custom, dump Neo4j offline, manifest/checksum, conferma distruttiva, whitelist e stack fermo.
- Documentazione architetturale, sicurezza, deployment, LXC, fixture, dipendenze/licenze e backup/restore.

## Correzioni della revisione finale indipendente

1. Rimossi gli artefatti generati `frontend/tsconfig.app.tsbuildinfo` e `frontend/tsconfig.node.tsbuildinfo`; il pattern `frontend/*.tsbuildinfo` era già presente in `.gitignore`.
2. Corretto `docs/adr/001-architettura-iniziale.md`: la ricostruzione Neo4j da solo `pg_dump` è un target, non una capacità Fase 1; finché sink/rebuild mancano, il backup include Neo4j.
3. Annotato `docs/MODELLO_DATI.md` come modello target Fase 0, distinguendolo dalle tabelle fisiche Fase 1.
4. Chiarito in `README.md` lo stato non collaudato e il rischio del doppio `alembic upgrade head`, oggi serializzato solo dalla dipendenza Compose app→worker.
5. Creato `docs/PIANO_FASE2_GMAIL.md` con backlog fixture-first, audit dei candidati e gate prima di credenziali reali.

Non sono state introdotte nuove dipendenze, funzionalità Gmail, installazioni NAS o connessioni a servizi personali.

## Matrice sintetica dei criteri Fase 1

| Criterio | Stato | Evidenza/limite |
|---|---|---|
| Architettura e confini Fase 0 coerenti | RAGGIUNTO DOCUMENTALMENTE | PostgreSQL autorevole, Neo4j proiezione, connettori/Graphiti futuri; sovradichiarazione ADR corretta |
| Compose valido e servizi/porte/reti coerenti | TEST DETERMINISTICO SUPERATO | `docker compose --profile compact config --quiet` exit 0; nessun avvio runtime |
| Immagine multi-stage e runtime non-root | CODICE PREDISPOSTO | revisione Dockerfile; build immagine non eseguita |
| Pin immagini/dipendenze e assenza `latest` | RAGGIUNTO STATICAMENTE | manifest/lockfile e controllo testuale; SBOM/vulnerability scan pendenti |
| Segreti e validazione production | TEST DETERMINISTICO SUPERATO | test config, HTTPS/cookie Secure/demo production; custodia/rotazione reale pendenti |
| Autenticazione single-owner/sessioni | TEST DETERMINISTICO SUPERATO | hashing, TTL, revoca e vincoli statici; bootstrap concorrente su PostgreSQL reale pendente |
| Cookie e CSRF double-submit | TEST DETERMINISTICO SUPERATO | attributi cookie e confronto header/cookie/digest coperti |
| Protezione API e SPA privata | TEST DETERMINISTICO/REVISIONE STATICA | route correnti protette; proxy/TLS reale e rate limiting non implementati |
| Demo sintetica confinata | TEST DETERMINISTICO SUPERATO | dataset, idempotenza/reset con fake, auth/CSRF invariati; seed su PostgreSQL reale pendente |
| Migrazione Alembic e schema Procrastinate | CODICE PREDISPOSTO | migrazione contiene schema vendorizzato dal package; applicazione su PostgreSQL reale non eseguita |
| Persistenza PostgreSQL dopo riavvio | NON RAGGIUNTO | richiede Docker/Linux reale |
| Worker Procrastinate operativo | CODICE PREDISPOSTO | definizioni e type/test statici; consumo coda reale non provato; healthcheck controlla solo PID 1 |
| Healthcheck app/datastore | TEST UNITARIO + CODICE PREDISPOSTO | semantics ready testata; health Docker osservato non eseguito |
| Limiti CPU/memoria e concorrenza 1 | DICHIARATO, NON OSSERVATO | Compose/config coerenti; cgroup reali non verificati |
| Bind mount e permessi UID/GID | NON RAGGIUNTO | richiede filesystem/mapping Linux/LXC reale |
| Backup/restore e sicurezza distruttiva | CODICE PREDISPOSTO + PARSER SUPERATO | conferma/whitelist/checksum/lock revisionati; nessuna esecuzione reale, CLI Neo4j non provata |
| Separazione dati/cache/import/backup | RAGGIUNTO STATICAMENTE | mount distinti; politica file autorevoli futuri ancora da definire |
| Neo4j/Graphiti correttamente non dichiarati implementati | RAGGIUNTO | solo driver/health; sink, rebuild e Graphiti assenti |
| Deployment Linux/LXC end-to-end | NON RAGGIUNTO | daemon e target autorizzato mancanti |
| Fase 1 integralmente collaudata | NON RAGGIUNTO | gate runtime sopra ancora aperti |

## Check finali realmente rieseguiti — 11 ottobre 2026

- `uv run ruff check backend`: **superato**, exit 0.
- `uv run ruff format --check backend`: **superato**, exit 0, 26 file già formattati.
- `uv run mypy backend/src backend/tests`: **superato**, exit 0, 24 file sorgente.
- `uv run pytest -q`: **superato**, exit 0, 26 test in 0,90 s; un warning di deprecazione Starlette/TestClient-httpx.
- `npm run typecheck`: **superato**, exit 0.
- `npm run build`: **superato**, exit 0, Vite 8.3.4, 16 moduli trasformati; `frontend/dist` resta ignorato.
- `docker compose --profile compact config --quiet`: **superato**, exit 0.
- `sh -n` per entrypoint, backup e restore tramite shell WSL `docker-desktop` alimentata via stdin: **superato**, exit 0; è soltanto verifica parser.
- Controllo riferimenti Markdown locali, marcatori conflitto, tag `latest`, assenza `*.tsbuildinfo` e LF degli script: **superato** dopo correzioni.
- `docker info`: **fallito**, exit 1; client Docker 29.8.2 e Compose 5.5.1 presenti, daemon/Server non disponibile.
- Build immagine, pull, avvio container, migrazione reale, persistenza, worker, cgroup, mount e backup/restore: **non eseguiti**.

Dettagli e storico dei comandi sono in [`docs/VERIFICHE_FASE1.md`](docs/VERIFICHE_FASE1.md).

## Verifiche non eseguite / non dimostrate

- Build multi-stage e avvio end-to-end dei quattro container.
- Migrazione da database vuoto, schema Procrastinate, bootstrap concorrente, persistenza dopo restart e seed demo su PostgreSQL reale.
- Enqueue/consumo Procrastinate reale e verifica che il worker progredisca, non solo che PID 1 sia vivo.
- Healthcheck Docker, dipendenze startup, restart policy, limiti cgroup e comportamento sotto pressione.
- Proprietà/permessi reali dei bind mount con UID/GID `10001:10001` e mapping LXC.
- Esecuzione di `scripts/backup.sh` e `scripts/restore.sh`, compatibilità `neo4j-admin`, checksum di un set reale e restore isolato completo.
- DNS, TLS, reverse proxy, trust dei forwarded header, firewall e accesso diretto alla porta applicativa.
- SBOM, scansione vulnerabilità, verifica primaria completa di licenze/notice e licenza del repository.
- Accessi Gmail, Immich, OmniRoute, Graphiti, NAS o dati personali: intenzionalmente non effettuati.

## Rischi e decisioni pendenti

- **Blocco release/deployment:** senza runtime Linux/Docker non è possibile certificare Fase 1.
- **Migrazioni:** app e worker eseguono entrambi Alembic; l'ordinamento corrente riduce la concorrenza ma non sostituisce un coordinatore per repliche/deploy futuri.
- **Worker health:** il probe attuale dimostra soltanto la vita del processo.
- **Proxy:** Uvicorn accetta forwarded header da ogni peer; la sicurezza dipende dalla pubblicazione loopback e da un proxy/rete fidati, da verificare sul target.
- **Autenticazione:** non sono implementati rate limiting, MFA o lockout; l'esposizione deve restare privata e protetta dal proxy/firewall.
- **Backup:** i secret sono esclusi per scelta; serve un vault/config DR separato. Un backup storico può contenere dati poi esclusi/cancellati.
- **Neo4j:** PostgreSQL è autorevole per design, ma il rebuild non esiste; entrambi i dump restano necessari.
- **Storage:** cache non è backup; futuri `storage_ref` richiederanno una policy file coerente aggiuntiva.
- **Operazioni:** RPO/RTO, retention, off-site, capacità, monitoraggio e soglie backpressure richiedono decisioni ambientali.
- **Fase 2:** ~~i due MCP candidati non sono identificati nel repository; non va inventata una scelta.~~ Audit completato nel sottocompito B. Decisione: client ufficiale Gmail. Vedere [`docs/adr/003-sincronizzatore-gmail-fase2.md`](docs/adr/003-sincronizzatore-gmail-fase2.md).

## Accessi e input mancanti

- Runtime Linux/LXC x86_64 autorizzato con Docker Engine/Compose v2 e spazio per build/restore.
- Decisioni Proxmox/LXC: risorse, cgroup, mapping UID/GID, mount e rete, senza escalation automatica a privilegiato.
- Percorsi/filesystem/capacità effettivi dei quattro mount e del Docker data root.
- DNS, URL pubblico, reverse proxy, certificati TLS e policy firewall.
- Vault/configuration recovery, registry/digest immutabile, RPO/RTO, retention e destinazione off-site.
- ~~Identità verificabili dei due MCP Gmail candidati e criteri organizzativi per scegliere un solo sincronizzatore.~~ ✅ Risolto — sottocompito B.
- OAuth client, account Gmail di prova, owner/alias e autorizzazione esplicita; nessun secret va aggiunto prima del gate reale.

## Procedura di ripresa del gate Fase 1

1. Su un runtime Linux **di prova**, completare [`docs/OPERAZIONI_LXC.md`](docs/OPERAZIONI_LXC.md) e registrare host, risorse, mount, mapping e rete.
2. Costruire l'immagine e avviare lo stack senza servizi personali; verificare immagini/digest, utente non-root, health e limiti osservati.
3. Da PostgreSQL vuoto applicare la migrazione, verificare tabelle applicative/Procrastinate, bootstrap/login/CSRF e persistenza dopo restart.
4. Accodare e consumare task deterministici con concorrenza uno; verificare checkpoint/outbox e comportamento di riavvio.
5. Produrre un backup sintetico e verificare manifest/checksum.
6. Predisporre target isolato con volumi nuovi, eseguire integralmente [`docs/BACKUP_RIPRISTINO.md`](docs/BACKUP_RIPRISTINO.md) e verificare i criteri dati/applicativi.
7. Registrare risultati reali senza trasformare un check statico in prova runtime; correggere soltanto difetti riprodotti.

## Checkpoint sottocompito B — 11 ottobre 2026

- Audit comparativo completato tra MCP A (`jimsimoy/gmail-mcp`), MCP B (`klodr/gmail-mcp`) e client ufficiale Gmail.
- **Ispezione MCP:** non è stato possibile ispezionare il codice sorgente dei due MCP; tutti gli aspetti risultano SCONOSCIUTI. Limite dichiarato senza colmare i vuoti.
- **Client ufficiale:** capacità verificate dalla documentazione Google: `messages.list/get`, `history.list`, `sendAs.list`, paginazione, scope `gmail.readonly`, retry/429, OAuth device/web, refresh/revoca. Licenza Apache-2.0.
- **Decisione:** client ufficiale Gmail scelto come unico sincronizzatore. MCP esclusi dal runtime; candidati futuri previa ispezione sorgente.
- **ADR:** [`docs/adr/003-sincronizzatore-gmail-fase2.md`](docs/adr/003-sincronizzatore-gmail-fase2.md) — contiene matrice comparativa, contratto tipizzato, invarianti SENT, categorie errore, checkpoint/reconcile, 20 minacce e 30 test contrattuali.
- **File aggiornati:** `PIANO_FASE2_GMAIL.md`, `DIPENDENZE.md`, `COMPONENTI_LICENZE.md`, `PROJECT_STATUS.md`.
- **Nessun codice applicativo modificato**, nessuna dipendenza aggiunta, nessuna configurazione runtime alterata.

## Prossimo incremento ristretto — Fase 2 Gmail `SENT`

Il backlog ufficiale è [`docs/PIANO_FASE2_GMAIL.md`](docs/PIANO_FASE2_GMAIL.md). Ordine immediato:

1. ~~identificare e auditare i due MCP candidati e il client Gmail ufficiale;~~ ✅
2. ~~decidere tramite ADR un **solo sincronizzatore** e i pin, senza credenziali;~~ ✅
3. definire contratto e fixture/contract test deterministici; ← **prossimo passo**
4. modellare OAuth read-only, owner/alias e custodia token;
5. imporre `SENT` su list, fetch e history;
6. coprire MIME, limiti, paginazione, checkpoint, reconcile e idempotenza;
7. trattare prompt injection come contenuto non fidato;
8. aggiungere ricerca testuale PostgreSQL e citazioni riconducibili alla revisione;
9. mantenere il collaudo Gmail reale **pendente** fino ad autorizzazione e credenziali revocabili.

Immich, NAS, OmniRoute, Graphiti, sink Neo4j, LLM e altre espansioni restano fuori da questo incremento.

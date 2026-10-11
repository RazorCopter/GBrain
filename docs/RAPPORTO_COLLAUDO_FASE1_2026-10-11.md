# Rapporto collaudo fondazione Fase 1 — 11 ottobre 2026

## Esito rigoroso

Il collaudo Docker reale è **NON ESEGUITO** perché il daemon Docker Linux non era operativo. Non sono stati simulati risultati. Sono state completate la ricognizione non distruttiva, le verifiche statiche/unitarie disponibili, la configurazione Compose isolata e la preparazione di uno script one-shot per un host Linux con Docker operativo.

## Capacità runtime osservate

| Capacità | Evidenza reale | Esito |
|---|---|---|
| Host | Windows 11 Pro Insider Preview 10.0.26220 build 26220, x64, shell `cmd.exe` | RILEVATO |
| CPU host | 16 processori logici | RILEVATO |
| Memoria host accessibile | 51.206.234.112 byte totali; 22.594.199.552 byte liberi al campione | RILEVATO |
| Disco C: accessibile | 987.956.781.056 byte liberi; 1.010.309.255.168 byte usati al campione | RILEVATO |
| Docker client | 29.8.2, `windows/amd64`, contesto `desktop-linux` | RILEVATO |
| Docker Compose | v5.5.1 | RILEVATO |
| WSL | WSL 2.7.3.0, kernel 6.6.114.1-1, distribuzione predefinita `docker-desktop` | RILEVATO |
| Docker server Linux | `docker version`/`docker info`: named pipe `dockerDesktopLinuxEngine` assente | NON DISPONIBILE |
| Stato Docker Desktop | `docker desktop status`: `Could not retrieve status. Is Docker Desktop running?` | NON DISPONIBILE |
| Cgroup/limiti container | server non raggiungibile, quindi non osservabili | NON ESEGUITO |
| Spazio immagini/volumi | `docker system df -v` fallisce sulla medesima named pipe assente | NON ESEGUITO |

Prerequisito mancante: avviare o rendere raggiungibile un daemon Docker Linux compatibile con Compose v2. Per richiesta esplicita dell'utente Docker Desktop non è stato avviato.

## Isolamento predisposto

Identificatore staticamente validato: `gbrain_phase1_a_20261011`; porte verificate libere: `127.0.0.1:18080` e `127.0.0.1:18081`. La configurazione risolta con credenziali sintetiche ha restituito exit 0 e nomi isolati per reti, secret e volumi. Nessun container, volume, rete o immagine è stato creato.

Lo script [`scripts/phase1_runtime_test.sh`](../scripts/phase1_runtime_test.sh) genera a ogni esecuzione due progetti distinti:

- sorgente `gbrain_phase1_src_<RUN_ID>`;
- restore `gbrain_phase1_restore_<RUN_ID>`.

Usa esclusivamente directory `runtime/phase1-<RUN_ID>`, porte 18080/18081 modificabili, credenziali sintetiche e volumi nominati sotto i due project name. Il trap di cleanup accetta soltanto project name con prefisso `gbrain_phase1_` e rimuove soltanto i due stack/volumi e la directory runtime della prova.

## Matrice prove

| Prova | Esito | Evidenza |
|---|---|---|
| Ruff lint | SUPERATO | exit 0 |
| Ruff format check | SUPERATO | exit 0, 27 file formattati |
| mypy strict | SUPERATO | exit 0, 20 file sorgente |
| pytest | SUPERATO | 26 test, 1 warning di dipendenza |
| TypeScript type-check | SUPERATO | exit 0 |
| Build Vite | SUPERATO | 16 moduli, asset produzione generati |
| Compose config isolato | SUPERATO | exit 0 con project `gbrain_phase1_a_20261011` |
| Porte 18080/18081 | SUPERATO | nessun listener al campione |
| Build immagine Docker | NON ESEGUITO | daemon Linux assente |
| Avvio database vuoto e migrazione Alembic | NON ESEGUITO | daemon Linux assente |
| Schema Procrastinate | NON ESEGUITO | daemon Linux assente |
| Health/readiness | NON ESEGUITO | daemon Linux assente |
| Bootstrap/login/cookie/CSRF/API anonima | NON ESEGUITO | daemon Linux assente |
| Job Procrastinate reale completato | NON ESEGUITO | daemon Linux assente |
| Demo caricata due volte/idempotenza | NON ESEGUITO | daemon Linux assente |
| Persistenza dopo restart | NON ESEGUITO | daemon Linux assente |
| Campione CPU/RAM/limiti container | NON ESEGUITO | daemon Linux assente |
| Log senza segreti/payload personali | NON ESEGUITO runtime; script predisposto | daemon Linux assente |
| Backup/checksum/manifest | NON ESEGUITO runtime; script predisposto | daemon Linux assente |
| Guasto dopo quiescenza e recovery app/worker | NON ESEGUITO runtime; hook predisposto | daemon Linux assente |
| Restore in target nuovo | NON ESEGUITO runtime; script predisposto | daemon Linux assente |
| Verifica PostgreSQL/Neo4j/owner/job/checkpoint/exclusion/deletion/outbox/login | NON ESEGUITO runtime; script predisposto | daemon Linux assente |

## Difetti riprodotti e correzioni

1. **Project name hardcoded nel backup/restore.** Il backup scriveva sempre `compose_project=gbrain` e il restore accettava soltanto quel valore. Un backup di uno stack isolato non era quindi ripristinabile mantenendo la verifica del manifest. Correzione: [`scripts/backup.sh`](../scripts/backup.sh) registra il `COMPOSE_PROJECT_NAME` effettivo; [`scripts/restore.sh`](../scripts/restore.sh) richiede esplicitamente un target diverso da `GBRAIN_RESTORE_SOURCE_PROJECT` e valida il manifest contro il progetto sorgente.
2. **Nessuna iniezione sicura del guasto dopo quiescenza.** Aggiunto `GBRAIN_BACKUP_TEST_FAIL_AFTER_QUIESCE=true`, disabilitato per default. Il fallimento avviene prima dei dump e attiva il trap esistente che ripristina app/worker precedentemente attivi.
3. **Comando locale di verifica frontend inizialmente errato.** `npm run type-check` non esiste; il manifest espone `typecheck`. Il comando corretto è stato rieseguito con successo. Nessuna modifica al prodotto necessaria.

## Procedura one-shot Linux

Da root del repository, su Linux con Docker daemon operativo e Compose v2:

```sh
chmod 0555 scripts/phase1_runtime_test.sh
./scripts/phase1_runtime_test.sh
```

Lo script esegue build, avvio vuoto, migrazioni, schema Procrastinate, readiness, autenticazione/CSRF, job reale, doppio seed demo, restart e persistenza, campione risorse/limiti, guasto backup, backup valido, restore su progetto nuovo, verifiche PostgreSQL/Neo4j e scansione log. Produce `phase1-runtime-<RUN_ID>.log` nel repository e ripulisce sempre stack, volumi e runtime sintetici della prova. Il report va considerato valido soltanto se termina con `RESULT=SUPERATO` ed exit 0.

## Comandi realmente eseguiti

- `ver`, variabili shell/architettura, query CIM PowerShell per OS/CPU/RAM/disco.
- `docker version`, `docker info`, `docker compose version`, `docker context show`, `docker context ls`, `docker desktop status`, `docker system df -v`, `docker compose ... config --quiet/--format json`, `docker compose ... ps --all`.
- `wsl --status`, `wsl --version`.
- verifica listener PowerShell per 18080/18081.
- `uv run ruff check backend scripts`.
- `uv run ruff format --check backend scripts`.
- `uv run mypy backend/src`.
- `uv run pytest -q`.
- `npm --prefix frontend run typecheck`.
- `npm --prefix frontend run build`.
- interrogazione help CLI Procrastinate e firma `defer_async` con configurazione sintetica.

## Cleanup

Nessun artefatto Docker è stato creato perché il daemon non era disponibile. Non è stato eseguito alcun prune. Non sono stati toccati stack, volumi, mount o servizi esistenti. Gli asset frontend locali sono stati rigenerati dal build già previsto; restano nel workspace secondo il comportamento corrente del progetto.

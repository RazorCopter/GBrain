# GBrain — Fondazione Fase 1 e Integrazione Gmail Fase 2

GBrain è un second brain autobiografico self-hosted. Questo repository contiene la **fondazione Fase 1** consolidata e l'integrazione connettore **Fase 2 Gmail `SENT`** (nucleo sync/MIME, OAuth web server-side, cifratura AES-256-GCM, worker Procrastinate, ricerca testuale PostgreSQL FTS e pagina Fonti). Immich, NAS, OmniRoute, Graphiti (Fase 3), MCP e funzioni autobiografiche/LLM non sono ancora implementati. Il codice ha superato tutti i controlli deterministici e di audit (Ruff, Mypy, build React e 93 test unitari/funzionali superati), mentre il collaudo su demone Docker/Linux reale e l'accesso con credenziali Google Cloud reali rimangono intenzionalmente pendenti: lo stato ufficiale è in [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

## Prerequisiti

Per l'avvio supportato servono un host Linux x86_64, Git, Docker Engine e Docker Compose v2 (`docker compose`). Lo stack richiede circa 3,8 GiB di limiti memoria complessivi più margine per host, build e filesystem; i limiti effettivi vanno verificati sul runtime. Per LXC completare prima il runbook in [`docs/OPERAZIONI_LXC.md`](docs/OPERAZIONI_LXC.md). Per il deployment guidato tramite Portainer su Linux/LXC/NAS consultare la guida operativa dedicata in [`docs/DEPLOY_PORTAINER.md`](docs/DEPLOY_PORTAINER.md).

Su Windows è possibile eseguire controlli e sviluppo con Docker Desktop Linux, ma la destinazione operativa prevista resta Linux/LXC. Gli script di backup/ripristino richiedono una shell POSIX e utility GNU/Linux; non sono script nativi Windows.

## Clonazione e configurazione

```console
git clone <URL_REPOSITORY> gbrain
cd gbrain
cp example.env .env
```

Modificare `.env` senza committarlo. Almeno:

- sviluppo locale: `GBRAIN_ENVIRONMENT=development`, URL HTTP su loopback e cookie Secure disabilitato;
- deployment: `GBRAIN_ENVIRONMENT=production`, `GBRAIN_PUBLIC_URL=https://...`, `GBRAIN_SESSION_SECURE_COOKIE=true`, `GBRAIN_DEMO_MODE=false`;
- mantenere `COMPOSE_PROFILES=compact` e un tag applicativo immutabile/versionato in `GBRAIN_IMAGE_TAG`.

### Generazione e custodia dei segreti

Generare **tre valori distinti** con CSPRNG, almeno 32 caratteri per il secret autenticazione; ad esempio su Linux:

```console
openssl rand -hex 32
```

Inserire valori diversi in `GBRAIN_POSTGRES_PASSWORD`, `GBRAIN_AUTH_SECRET` e `GBRAIN_NEO4J_PASSWORD`. Compose li legge dall'ambiente e li presenta ai container come file sotto `/run/secrets`; `.env` resta comunque materiale sensibile sull'host. Impostare permessi stretti:

```console
chmod 600 .env
```

Non stampare i valori nei log, non copiarli nel backup dati e non versionarli. Archiviare separatamente, cifrati e con controllo accessi, `.env`/valori segreti, URL pubblico, record DNS/TLS/proxy e istruzioni di recupero. Il backup applicativo include configurazione non segreta e lockfile, non `.env`. La rotazione di password datastore non è automatizzata; il cambio di `GBRAIN_AUTH_SECRET` invalida di fatto le sessioni esistenti.

## Scelta sicura dei quattro mount

Impostare percorsi host **assoluti**, distinti e preesistenti; non vengono prescritti percorsi NAS:

| Variabile | Contenuto/policy | Accesso container |
|---|---|---|
| `DATA_DIR` | dati applicativi attivi futuri; persistente, non è il volume PostgreSQL | lettura/scrittura UID/GID `10001:10001` |
| `CACHE_DIR` | rigenerabile; **non è un backup** | lettura/scrittura UID/GID `10001:10001` |
| `IMPORT_DIR` | originali/staging forniti dall'operatore; proteggerli e conservarli secondo la fonte | sola lettura nell'app/worker |
| `BACKUP_DIR` | area applicativa per backup; non sostituisce copie offline/off-site | lettura/scrittura UID/GID `10001:10001` |

PostgreSQL e Neo4j usano invece volumi Docker nominati. Uno snapshot LXC può non includere bind mount o mount esterni. Evitare sovrapposizioni fra i quattro percorsi, mount instabili e filesystem senza semantica Linux adeguata; verificare spazio, proprietà e permessi come descritto nel runbook. In locale sono disponibili i default sotto `./runtime/`.

Esempio Linux, adattando percorsi e gruppo alla propria politica:

```console
install -d -m 0750 -o 10001 -g 10001 /percorso/data /percorso/cache /percorso/backup
install -d -m 0750 /percorso/import
```

Non applicare proprietari o permessi alla cieca su share esistenti: validare prima mapping UID/GID e filesystem.

## Build, avvio e migrazioni

Validare la configurazione risolta senza esporre i secret:

```console
docker compose --profile compact config --quiet
docker compose --profile compact build --pull
docker compose --profile compact up -d
docker compose --profile compact ps
```

Gli entrypoint di `app` e `worker` eseguono `alembic upgrade head` prima del processo; nel Compose corrente il worker attende che l'app sia healthy e quindi la seconda invocazione è normalmente un no-op. Non avviare repliche concorrenti né modificare l'ordine di startup senza introdurre un coordinamento esplicito delle migrazioni. Per una migrazione esplicita con i datastore disponibili:

```console
docker compose --profile compact run --rm app migrate
```

L'avvio iniziale crea i volumi nominati e richiede che i bind mount siano accessibili all'UID/GID `10001:10001`. Per un deployment LXC non pubblicare direttamente su tutte le interfacce: lasciare `WEB_BIND_ADDRESS=127.0.0.1` se il reverse proxy è locale; usare un indirizzo specifico soltanto dopo una decisione di rete esplicita. PostgreSQL e Neo4j non hanno porte host pubblicate.

## Bootstrap, login e demo

Aprire `GBRAIN_PUBLIC_URL` (in sviluppo, `http://localhost:8000`). Alla prima apertura la UI propone il bootstrap del solo proprietario. Creare credenziali robuste: username di 3–80 caratteri nel formato accettato e password di 14–256 caratteri. Il bootstrap si chiude dopo la creazione; effettuare poi il login dalla UI.

La riapertura è distruttiva per account e sessioni e richiede prima un backup verificato:

```console
docker compose --profile compact exec app python -m gbrain.cli reset-owner --confirm RESET-OWNER-GBRAIN
```

La demo è solo opt-in per sviluppo/test e **vietata in produzione**. Con `GBRAIN_DEMO_MODE=true`:

```console
docker compose --profile compact exec app python -m gbrain.cli demo-fixtures
docker compose --profile compact exec app python -m gbrain.cli demo-fixtures --reset
```

Il secondo comando resetta solo il namespace demo riconosciuto. La demo non crea account, non aggira autenticazione/CSRF e non contatta servizi reali. Dettagli in [`docs/FIXTURE_SINTETICHE.md`](docs/FIXTURE_SINTETICHE.md).

## Arresto, aggiornamento e rollback

Arresto ordinario, senza cancellare volumi:

```console
docker compose --profile compact stop
docker compose --profile compact down
```

Non aggiungere `--volumes` salvo cancellazione dati deliberata e documentata. Prima di aggiornare: leggere changelog, fissare nuovi tag/digest e lockfile, creare e verificare un backup, provare restore e migrazioni in ambiente isolato. Poi:

```console
docker compose --profile compact build --pull
docker compose --profile compact up -d
docker compose --profile compact ps
```

Il rollback dell'immagine è sicuro solo se lo schema resta compatibile. Non esiste un rollback automatico di schema: se una migrazione non è reversibile, ripristinare **insieme** PostgreSQL e Neo4j da un backup precedente in stack isolato, usando il codice/immagini registrati nel manifest. Procedure complete in [`docs/BACKUP_RIPRISTINO.md`](docs/BACKUP_RIPRISTINO.md).

## Diagnosi essenziale

```console
docker compose --profile compact ps
docker compose --profile compact logs --tail=200 app worker postgres neo4j
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
docker stats --no-stream
```

`/health/live` verifica il processo; `/health/ready` rende PostgreSQL autorevole e riporta Neo4j separatamente. Il healthcheck worker verifica solo che il processo PID 1 viva, non che consumi realmente le code. I log Docker sono ruotati (`10m`, 3 file per servizio), ma possono contenere metadati operativi: limitarne accesso e non usare `DEBUG` stabilmente in produzione.

Problemi comuni:

- configurazione rifiutata: controllare segreti obbligatori, lunghezza auth secret e requisiti HTTPS/cookie Secure in produzione;
- app non pronta: verificare PostgreSQL, migrazioni, spazio e permessi dei bind mount;
- Neo4j degradato: controllare relativo healthcheck e memoria; nella Fase 1 non esiste ancora un sink outbox→grafo;
- OOM o throttling: confrontare limiti dichiarati e cgroup effettivi; non alzare limiti senza verificare risorse host;
- disco pieno/backpressure: arrestare ingestion futura, non cancellare dati/volumi alla cieca, conservare dump verificati e analizzare volumi/log/code.

Runbook operativi: [`docs/DEPLOY_PORTAINER.md`](docs/DEPLOY_PORTAINER.md), [`docs/OPERAZIONI_LXC.md`](docs/OPERAZIONI_LXC.md), [`docs/SICUREZZA.md`](docs/SICUREZZA.md), [`docs/BACKUP_RIPRISTINO.md`](docs/BACKUP_RIPRISTINO.md), [`docs/COMPONENTI_LICENZE.md`](docs/COMPONENTI_LICENZE.md). Stato verificato e limiti: [`PROJECT_STATUS.md`](PROJECT_STATUS.md).

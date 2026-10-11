# Runbook operativo Linux/LXC — Fase 1

## Scopo e regole

Questa procedura prepara e verifica un Linux x86_64/LXC già predisposto dall'amministratore. Non modifica Proxmox, non crea mount, non installa sul NAS e non propone di trasformare automaticamente un container in privilegiato. Le scelte Proxmox (nesting, mapping UID/GID, storage, backup e rete) restano responsabilità dell'amministratore e devono essere validate fuori da questo repository.

Eseguire prima soltanto controlli non distruttivi. Registrare data, host/LXC, output e decisioni in un ticket operativo senza incollare secret.

## 1. Inventario non distruttivo dell'host/LXC

### Architettura e sistema

```console
uname -a
uname -m
cat /etc/os-release
getconf LONG_BIT
```

Criterio: architettura `x86_64`/64 bit coerente con il target dichiarato. Non dedurre compatibilità di tutte le immagini dalla sola architettura: verificarla durante pull/build controllati.

### Memoria, swap e disco

```console
free -h
swapon --show
findmnt -D
df -hT
df -i
```

Lo stack dichiara limiti massimi di 512 MiB app, 1 GiB worker, 768 MiB PostgreSQL e 1536 MiB Neo4j, oltre a 128 MiB shared memory PostgreSQL. Sono circa 3,8 GiB, esclusi Docker, build, page cache e host. Verificare margine reale; swap è una protezione da picchi, non un sostituto della RAM. Controllare sia blocchi sia inode e prevedere spazio per immagini, layer, volumi e almeno un backup temporaneo.

### Cgroup e limiti osservabili

```console
stat -fc %T /sys/fs/cgroup
cat /proc/self/cgroup
systemd-detect-virt 2>/dev/null || true
docker info
```

Dopo un avvio autorizzato:

```console
docker compose --profile compact ps
docker stats --no-stream
docker inspect gbrain-app-1 --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}'
docker inspect gbrain-worker-1 --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}'
docker inspect gbrain-postgres-1 --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}} {{.HostConfig.ShmSize}}'
docker inspect gbrain-neo4j-1 --format '{{.HostConfig.Memory}} {{.HostConfig.NanoCpus}}'
```

I nomi container possono differire: ricavarli da `docker compose ps -q <servizio>`. Il solo parsing Compose non dimostra che il kernel applichi i limiti; confrontare `docker inspect`, `docker stats` e cgroup reali. Se Docker fallisce in LXC, diagnosticare prerequisiti con l'amministratore Proxmox: non aggiungere privilegi o capability alla cieca.

## 2. Runtime Docker e Compose

```console
docker version
docker info
docker compose version
docker compose --profile compact config --quiet
```

Criteri:

- daemon raggiungibile dall'operatore autorizzato;
- Compose v2 (`docker compose`), non il binario legacy;
- driver logging e storage noti;
- nessun warning critico su cgroup/storage;
- configurazione Compose valida e profilo `compact` attivo;
- immagini pin per versione e digest, nessun `latest`.

L'accesso al socket Docker equivale sostanzialmente a privilegio amministrativo sull'host/LXC: limitarlo.

## 3. Docker data root e capacità

```console
docker info --format '{{.DockerRootDir}} {{.Driver}}'
docker system df -v
df -hT "$(docker info --format '{{.DockerRootDir}}')"
df -i "$(docker info --format '{{.DockerRootDir}}')"
```

Verificare che il data root sia su storage affidabile e con margine per immagini/build e volumi nominati `postgres_data`, `neo4j_data`, `neo4j_logs`. Non eseguire `docker system prune`, rimozioni di volume o pulizie automatiche come rimedio operativo: prima inventariare e fare backup.

## 4. Bind mount e filesystem

Scegliere percorsi assoluti e separati per `DATA_DIR`, `CACHE_DIR`, `IMPORT_DIR`, `BACKUP_DIR`; questo runbook non presume nomi né percorsi NAS.

Per ciascun percorso candidato:

```console
findmnt -T /percorso/scelto -o TARGET,SOURCE,FSTYPE,OPTIONS
stat -c '%n type=%F uid=%u gid=%g mode=%a' /percorso/scelto
df -hT /percorso/scelto
df -i /percorso/scelto
namei -l /percorso/scelto
```

Criteri:

- nessuna sovrapposizione o link simbolico ambiguo;
- mount disponibile già prima dell'avvio Docker, senza fallback silenzioso sulla directory locale sottostante;
- filesystem e opzioni compatibili con workload e semantica di permessi richiesti;
- `DATA_DIR`, `CACHE_DIR`, `BACKUP_DIR` scrivibili da UID/GID `10001:10001` nel namespace effettivo dell'LXC;
- `IMPORT_DIR` leggibile dallo stesso UID e montato read-only nei container;
- capacità e inode monitorabili;
- policy di backup distinta per ogni mount.

Una prova di scrittura va fatta soltanto in una sottodirectory dedicata e autorizzata, impersonando l'UID mappato; non cambiare proprietà ricorsivamente su mount condivisi. Con LXC unprivileged, verificare la traduzione UID/GID con l'amministratore. Non rendere l'LXC privilegiato come scorciatoia.

Politiche:

- `DATA_DIR`: dati attivi applicativi presenti/futuri, da includere esplicitamente nella protezione quando conterrà file autorevoli;
- `CACHE_DIR`: eliminabile/rigenerabile solo quando l'app lo consente; mai qualificato come backup;
- `IMPORT_DIR`: originali/staging, retention e immutabilità dipendono dalla fonte; oggi i connettori non sono implementati;
- `BACKUP_DIR`: destinazione di lavoro, da replicare successivamente su supporto separato/off-site con checksum;
- volumi database: backup logico/coordinato secondo [`BACKUP_RIPRISTINO.md`](BACKUP_RIPRISTINO.md), mai copia a caldo.

Gli snapshot LXC non garantiscono di includere bind mount o mount esterni e non sostituiscono il backup applicativo.

## 5. Porte, rete, DNS, TLS e proxy

Controlli non distruttivi:

```console
ss -lntup
getent hosts nome.pubblico.example
curl -I https://nome.pubblico.example 2>/dev/null || true
```

Decisioni richieste prima del deployment:

- `WEB_PORT` libero e `WEB_BIND_ADDRESS` esplicito;
- preferire loopback se il reverse proxy è nello stesso LXC; altrimenti usare un indirizzo/rete dedicata e firewallata, non `0.0.0.0` per comodità;
- nessuna pubblicazione host di PostgreSQL 5432, Neo4j Bolt 7687 o HTTP 7474;
- record DNS corretto, certificato TLS valido e rinnovo monitorato;
- reverse proxy che preservi host/proto e accetti traffico solo da proxy fidati;
- timeout/body limit coerenti con API attuali;
- `GBRAIN_PUBLIC_URL=https://...` e `GBRAIN_SESSION_SECURE_COOKIE=true` in produzione.

L'app abilita gli header proxy lato Uvicorn; è quindi importante impedire accesso diretto non fidato alla porta web. Non dichiarare TLS verificato finché non si prova il percorso client→proxy→app reale.

## 6. Discovery futura: Immich e OmniRoute

Immich e OmniRoute non sono implementati. Prima della Fase relativa, registrare come **input**, senza token:

- URL e risoluzione DNS interni/esterni;
- versione esatta, API/versione protocollo e policy di compatibilità;
- CA/TLS e modalità di autenticazione;
- account read-only/scopo token, rate limit e timeout;
- egress consentito, proxy e regole firewall;
- retention e proprietà dei dati restituiti.

Non collegare servizi reali durante la Fase 1 e non assumere endpoint o versioni.

## 7. Avvio controllato e criteri di accettazione

Dopo il completamento delle verifiche:

```console
docker compose --profile compact config --quiet
docker compose --profile compact build --pull
docker compose --profile compact up -d
docker compose --profile compact ps
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
docker stats --no-stream
```

Accettare solo se:

1. quattro servizi presenti e health coerenti;
2. sola porta web pubblicata all'indirizzo deciso;
3. limiti memoria/CPU osservati nel runtime;
4. mount corretti, senza directory fallback create per mount mancanti;
5. migrazione a head riuscita e log privi di secret/errori;
6. bootstrap/login provati via URL finale TLS in produzione;
7. `GBRAIN_DEMO_MODE=false` in produzione;
8. backup completato e prova restore isolata pianificata/eseguita.

## 8. Monitoraggio, spazio e backpressure

Monitorare almeno RAM/OOM, CPU throttling, spazio/inode del Docker root e dei quattro mount, stato health, restart count, dimensione log, connessioni PostgreSQL, import job/checkpoint e outbox per stato/età. Il worker corrente ha concorrenza uno e il suo healthcheck non dimostra consumo della coda.

Se spazio o backlog superano la soglia locale:

1. fermare nuove ingestion/job quando saranno implementate; in Fase 1 arrestare il worker se necessario;
2. conservare evidenza e creare un backup se c'è spazio sicuro sufficiente;
3. identificare crescita fra log, layer, volumi, backup, import e cache;
4. non cancellare originali, record di esclusione/cancellazione o volumi alla cieca;
5. espandere/ruotare secondo policy approvata e verificare readiness/worker dopo il rientro.

Le soglie dipendono da capacità, RPO/RTO e crescita reali: non esiste un valore universale.

## 9. Aggiornamento operativo

Usare release/tag applicativi immutabili e mantenere digest infrastrutturali. Prima dell'update: backup, checksum, copia protetta configurazione/secret, prova restore recente e verifica compatibilità schema. Aggiornare una variabile alla volta, eseguire build/pull controllati e osservare health/log/cgroup. Il downgrade dell'immagine non implica downgrade schema; per schema incompatibile usare restore coordinato in ambiente isolato.

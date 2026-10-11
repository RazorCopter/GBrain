# Guida Operativa al Deployment dello Stack GBrain su Portainer

Questa guida descrive la procedura completa per distribuire lo stack **GBrain** mediante **Portainer** (Community Edition o Business) in un ambiente Docker su Linux o all'interno di un container LXC su Proxmox VE, con storage persistente ospitato su Synology NAS (`/mnt/synology_docker/GBrain`).

---

## 1. Architettura dello Stack e Metodi di Deploy in Portainer

### 1.1 Differenza Fondamentale: Web Editor vs Repository (Il Confronto con Immich)

Quando si distribuisce uno stack su Portainer (tramite il pulsante **+ Add stack**), è fondamentale comprendere la differenza tra container preconfezionati e progetti con codice sorgente:

- **Perché con Immich si può usare direttamente il "Web editor"?**  
  Progetti come Immich pubblicano su registry pubblici (es. GitHub Container Registry `ghcr.io/immich-app/immich-server:release`) immagini Docker già compilate e pronte all'uso. Incollando il `docker-compose.yml` nel Web Editor di Portainer, il demone Docker deve unicamente effettuare il *pull* delle immagini.
- **Perché GBrain con `build: .` richiede il "Repository" (oppure un pre-build)?**  
  Il codice sorgente di GBrain risiede nel repository Git `https://github.com/RazorCopter/GBrain`. Nel file [`compose.yaml`](compose.yaml:52), i servizi `app` e `worker` specificano una direttiva di compilazione (`build: context: .`).  
  Se si incolla tale configurazione direttamente nel **Web editor**, Portainer non dispone dei file sorgente (frontend React, backend FastAPI, `Dockerfile`, file lock uv) e fallirà con un errore di tipo `build context does not exist or is not accessible`.

### 1.2 I Due Metodi di Deploy Disponibili in Portainer

| Caratteristica | Metodo A: Scheda "Repository" (Consigliato) | Metodo B: Scheda "Web Editor" (Stile Immich) |
| :--- | :--- | :--- |
| **Comandi shell manuali** | **Zero** sull'host/LXC | Richiede `docker build` manuale da shell una tantum |
| **Come si attiva in Portainer** | Scheda **Repository** con URL Git | Scheda **Web editor** incollando lo YAML pronto |
| **Compilazione** | Automatica eseguita da Portainer ad ogni deploy | Pre-compilata manualmente prima del deploy |
| **Aggiornamenti futuri** | Pulsante *Pull and redeploy* / Webhook automatico | Rilanciare build manuale e premere *Redeploy* |

---

## 2. Passo 1 — Preparazione del Filesystem sul NAS / Host / LXC

In un setup tipico con storage persistente su Synology NAS montato su `/mnt/synology_docker/GBrain`, ciascun container necessita di percorsi fisici con proprietà UID:GID e permessi POSIX dedicati.

### 2.1 Mappa degli UID/GID e Permessi dei Container
- **`app` e `worker`** (GBrain): girano come utente `gbrain` con **UID `10001`** e **GID `10001`** ([`Dockerfile`](Dockerfile:25)).
- **`postgres`**: l'immagine ufficiale `postgres:16.10-bookworm` ([`compose.yaml`](compose.yaml:113)) gira con **UID `999`** e **GID `999`**. PostgreSQL **rifiuta tassativamente l'avvio** se la propria cartella dati ha permessi aperti a group o other; richiede permessi POSIX `700` (`rwx------`).
- **`neo4j`**: l'immagine ufficiale `neo4j:5.26.12-community` ([`compose.yaml`](compose.yaml:138)) gira con **UID `7474`** e **GID `7474`** e permessi `750`.
- **`secrets`**: file di credenziali e token Gmail opzionali, leggibili da `10001:10001` con permessi `700` / `600`.

### 2.2 Scelta della Struttura Cartelle per PostgreSQL e Neo4j

Sul NAS / LXC sono tipicamente già create le cartelle applicative:
- `/mnt/synology_docker/GBrain/data`
- `/mnt/synology_docker/GBrain/cache`
- `/mnt/synology_docker/GBrain/import` (singolare)
- `/mnt/synology_docker/GBrain/backup` (singolare)

Per i database relazionali e a grafo esistono due opzioni:
1. **Opzione Raccomandata (Cartelle dedicate di primo livello)**:  
   Creare `/mnt/synology_docker/GBrain/postgres` e `/mnt/synology_docker/GBrain/neo4j`.  
   *Perché è preferibile*: garantisce il perfetto isolamento dei permessi POSIX. L'utente `10001` non può accidentalmente modificare i permessi `700` di PostgreSQL (cosa che causerebbe il blocco irreversibile del database).
2. **Opzione Sottocartelle interne a data**:  
   Posizionare i dati in `/mnt/synology_docker/GBrain/data/postgres` e `/mnt/synology_docker/GBrain/data/neo4j`.  
   *Attenzione*: se si sceglie questa opzione, la sottocartella `data/postgres` deve comunque avere proprietà `999:999` e permessi `700`, evitando comandi `chown -R 10001:10001 /mnt/synology_docker/GBrain/data` ricorsivi che corromperebbero la proprietà di PostgreSQL.
3. **Opzione Named Volume Docker**:  
   Se si preferisce non creare cartelle host per i database, si possono usare i named volume Docker gestiti dall'engine (`postgres_data` e `neo4j_data`).

### 2.3 Comandi Shell per Creare Cartelle e Assegnare i Permessi

Eseguire una tantum da terminale (SSH sull'host Linux o console del container LXC):

```bash
# 1. Creazione di tutte le cartelle (inclusi i database dedicati e secrets)
sudo mkdir -p /mnt/synology_docker/GBrain/data
sudo mkdir -p /mnt/synology_docker/GBrain/cache
sudo mkdir -p /mnt/synology_docker/GBrain/import
sudo mkdir -p /mnt/synology_docker/GBrain/backup
sudo mkdir -p /mnt/synology_docker/GBrain/postgres
sudo mkdir -p /mnt/synology_docker/GBrain/neo4j
sudo mkdir -p /mnt/synology_docker/GBrain/neo4j/logs
sudo mkdir -p /mnt/synology_docker/GBrain/secrets

# 2. Assegnazione della proprietà UID:GID corretta
# Applicazione e worker GBrain (10001:10001)
sudo chown -R 10001:10001 /mnt/synology_docker/GBrain/data \
                          /mnt/synology_docker/GBrain/cache \
                          /mnt/synology_docker/GBrain/import \
                          /mnt/synology_docker/GBrain/backup \
                          /mnt/synology_docker/GBrain/secrets

# PostgreSQL (999:999 per Debian Bookworm)
sudo chown -R 999:999 /mnt/synology_docker/GBrain/postgres

# Neo4j (7474:7474)
sudo chown -R 7474:7474 /mnt/synology_docker/GBrain/neo4j

# 3. Impostazione dei permessi POSIX
sudo chmod 750 /mnt/synology_docker/GBrain/data \
               /mnt/synology_docker/GBrain/cache \
               /mnt/synology_docker/GBrain/import \
               /mnt/synology_docker/GBrain/backup \
               /mnt/synology_docker/GBrain/neo4j \
               /mnt/synology_docker/GBrain/neo4j/logs

# PostgreSQL ESIGE permessi restrittivi 700:
sudo chmod 700 /mnt/synology_docker/GBrain/postgres

# Directory secrets: permessi restrittivi 700:
sudo chmod 700 /mnt/synology_docker/GBrain/secrets
```

> **Avvertenza per NFS da Synology**: se `/mnt/synology_docker` è una condivisione NFS montata da Synology DSM, nel Pannello di Controllo -> *Cartella condivisa* -> *Permessi NFS* impostare lo squashing su **"Nessuna mappatura" (`no_root_squash`)**. Con `all_squash` le chiamate `chown 999:999` fallirebbero e i container andrebbero in errore di permessi all'avvio.

### 2.4 Generazione dei Segreti Crittografici Obbligatori

Generare tre stringhe casuali sicure prima di configurare lo stack:

```bash
# Password per PostgreSQL
openssl rand -hex 24

# Secret per sessione/autenticazione (minimo 32 caratteri)
openssl rand -hex 32

# Password per Neo4j
openssl rand -hex 24
```

---

## 3. Metodo A (Consigliato) — Deploy tramite Scheda "Repository"

Questo è il metodo ufficiale più semplice su Portainer: non richiede di compilare manualmente le immagini né di usare la shell Docker.

### 3.1 Creazione dello Stack
1. Accedere a Portainer -> selezionare l'ambiente target (es. **local**).
2. Selezionare **Stacks** -> **+ Add stack**.
3. Nella riga **Build method**, cliccare sulla scheda **Repository**.
4. Compilare i seguenti campi:
   - **Name**: `gbrain`
   - **Repository URL**: `https://github.com/RazorCopter/GBrain`
   - **Repository reference**: `refs/heads/main` (o `main`)
   - **Compose path**: `compose.yaml` (⚠️ verificare di non lasciare il valore di default `docker-compose.yml`)
   - **Automatic updates** (opzionale): abilitare per aggiornare automaticamente lo stack ad ogni nuovo commit.

### 3.2 Variabili d'Ambiente per il Metodo A
Nella stessa pagina, scorrere alla sezione **Environment variables**, selezionare **Advanced mode** e incollare il blocco seguente (sostituendo le voci `CAMBIA_CON_...` e l'IP):

```bash
COMPOSE_PROFILES=compact
GBRAIN_ENVIRONMENT=development
GBRAIN_LOG_LEVEL=INFO
GBRAIN_PUBLIC_URL=http://192.168.1.150:8000
WEB_BIND_ADDRESS=0.0.0.0
WEB_PORT=8000

# Percorso radice su host / Synology
GBRAIN_HOST_STORAGE_PATH=/mnt/synology_docker/GBrain

# Volumi applicativi (singolare)
DATA_DIR=/mnt/synology_docker/GBrain/data
CACHE_DIR=/mnt/synology_docker/GBrain/cache
IMPORT_DIR=/mnt/synology_docker/GBrain/import
BACKUP_DIR=/mnt/synology_docker/GBrain/backup

# Volumi database
POSTGRES_DATA_DIR=/mnt/synology_docker/GBrain/postgres
NEO4J_DATA_DIR=/mnt/synology_docker/GBrain/neo4j
NEO4J_LOGS_DIR=/mnt/synology_docker/GBrain/neo4j/logs

# Database e segreti
POSTGRES_DB=gbrain
POSTGRES_USER=gbrain
GBRAIN_POSTGRES_PASSWORD=CAMBIA_CON_PASSWORD_POSTGRES_GENERATA
GBRAIN_AUTH_SECRET=CAMBIA_CON_SECRET_MIN_32_CARATTERI_GENERATO
GBRAIN_NEO4J_PASSWORD=CAMBIA_CON_PASSWORD_NEO4J_GENERATA
```

5. Cliccare su **Deploy the stack**. Portainer clonerà il repo, compilerà l'immagine tramite [`Dockerfile`](Dockerfile:1) e avvierà tutti i 4 servizi.

---

## 4. Metodo B — Deploy tramite Scheda "Web Editor" (Stile Immich)

Se si desidera incollare direttamente lo YAML nel **Web editor** di Portainer, esattamente come si fa con Immich, è sufficiente pre-compilare l'immagine sull'host una tantum oppure usare un compose che referenzia l'immagine compilata locale.

### 4.1 Passo Preliminare Shell (Build una tantum)
Sul container LXC o host Docker eseguire:

```bash
# Scarica il repository e compila l'immagine locale
sudo git clone https://github.com/RazorCopter/GBrain.git /opt/gbrain-build
cd /opt/gbrain-build
sudo docker build -t gbrain:0.1.0 .
```

Una volta eseguito questo comando, l'immagine `gbrain:0.1.0` sarà memorizzata nella cache locale di Docker del server.

### 4.2 Compose YAML Completo "Chiavi in Mano" per Web Editor

In Portainer -> **Stacks** -> **+ Add stack** -> selezionare la scheda **Web editor**, assegnare il nome `gbrain` e incollare il seguente Compose YAML auto-consistente.  
Tutti i volumi sono già puntati direttamente a `/mnt/synology_docker/GBrain/...`:

```yaml
name: gbrain

x-logging: &default-logging
  driver: json-file
  options:
    max-size: "10m"
    max-file: "3"

x-app-environment: &app-environment
  GBRAIN_ENVIRONMENT: ${GBRAIN_ENVIRONMENT:-development}
  GBRAIN_LOG_LEVEL: ${GBRAIN_LOG_LEVEL:-INFO}
  GBRAIN_PUBLIC_URL: ${GBRAIN_PUBLIC_URL:-http://192.168.1.150:8000}
  GBRAIN_DEMO_MODE: ${GBRAIN_DEMO_MODE:-false}
  GBRAIN_DATA_DIR: /var/lib/gbrain/data
  GBRAIN_CACHE_DIR: /var/lib/gbrain/cache
  GBRAIN_IMPORT_DIR: /var/lib/gbrain/import
  GBRAIN_BACKUP_DIR: /var/lib/gbrain/backup
  GBRAIN_FRONTEND_DIR: /app/frontend/dist
  GBRAIN_POSTGRES_HOST: postgres
  GBRAIN_POSTGRES_PORT: "5432"
  GBRAIN_POSTGRES_DB: ${POSTGRES_DB:-gbrain}
  GBRAIN_POSTGRES_USER: ${POSTGRES_USER:-gbrain}
  GBRAIN_POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
  GBRAIN_AUTH_SECRET_FILE: /run/secrets/auth_secret
  GBRAIN_SESSION_TTL_SECONDS: ${GBRAIN_SESSION_TTL_SECONDS:-43200}
  GBRAIN_SESSION_SECURE_COOKIE: ${GBRAIN_SESSION_SECURE_COOKIE:-false}
  GBRAIN_NEO4J_URI: bolt://neo4j:7687
  GBRAIN_NEO4J_USER: neo4j
  GBRAIN_NEO4J_PASSWORD_FILE: /run/secrets/neo4j_password
  GBRAIN_NEO4J_DATABASE: neo4j
  GBRAIN_WORKER_CONCURRENCY: "1"

x-app-volumes: &app-volumes
  - type: bind
    source: /mnt/synology_docker/GBrain/data
    target: /var/lib/gbrain/data
  - type: bind
    source: /mnt/synology_docker/GBrain/cache
    target: /var/lib/gbrain/cache
  - type: bind
    source: /mnt/synology_docker/GBrain/import
    target: /var/lib/gbrain/import
    read_only: true
  - type: bind
    source: /mnt/synology_docker/GBrain/backup
    target: /var/lib/gbrain/backup

services:
  app:
    image: gbrain:0.1.0
    command: [ "web" ]
    environment: *app-environment
    secrets:
      - postgres_password
      - auth_secret
      - neo4j_password
    volumes: *app-volumes
    ports:
      - "0.0.0.0:8000:8000"
    networks:
      - edge
      - backend
    depends_on:
      postgres:
        condition: service_healthy
      neo4j:
        condition: service_started
    healthcheck:
      test: [ "CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=4)" ]
      interval: 15s
      timeout: 5s
      retries: 5
      start_period: 30s
    mem_limit: 512m
    cpus: 0.50
    init: true
    restart: unless-stopped
    logging: *default-logging

  worker:
    image: gbrain:0.1.0
    command: [ "worker" ]
    environment: *app-environment
    secrets:
      - postgres_password
      - auth_secret
      - neo4j_password
    volumes: *app-volumes
    networks:
      - backend
    depends_on:
      app:
        condition: service_healthy
    healthcheck:
      test: [ "CMD", "python", "-c", "import os; os.kill(1, 0)" ]
      interval: 30s
      timeout: 5s
      retries: 3
      start_period: 20s
    mem_limit: 1024m
    cpus: 1.00
    init: true
    restart: unless-stopped
    logging: *default-logging

  postgres:
    image: postgres:16.10-bookworm@sha256:38471f330eb885e04de130b768d6db4e10469e2311879c7e5c699f6d2d8a1c74
    environment:
      POSTGRES_DB: ${POSTGRES_DB:-gbrain}
      POSTGRES_USER: ${POSTGRES_USER:-gbrain}
      POSTGRES_PASSWORD_FILE: /run/secrets/postgres_password
    secrets:
      - postgres_password
    volumes:
      - /mnt/synology_docker/GBrain/postgres:/var/lib/postgresql/data
    networks:
      - backend
    healthcheck:
      test: [ "CMD-SHELL", "pg_isready -U $${POSTGRES_USER} -d $${POSTGRES_DB}" ]
      interval: 10s
      timeout: 5s
      retries: 10
      start_period: 20s
    mem_limit: 768m
    cpus: 1.00
    shm_size: 128m
    restart: unless-stopped
    logging: *default-logging

  neo4j:
    image: neo4j:5.26.12-community@sha256:9f75e8df4325a24f00fdd7a8c0bcce650a58375049b1058e496e8b43d6c36b37
    entrypoint:
      - /bin/bash
      - -ec
      - export NEO4J_AUTH="neo4j/$$(cat /run/secrets/neo4j_password)"; exec /startup/docker-entrypoint.sh neo4j
    environment:
      NEO4J_server_memory_heap_initial__size: 256m
      NEO4J_server_memory_heap_max__size: 512m
      NEO4J_server_memory_pagecache_size: 512m
      NEO4J_server_default__listen__address: 0.0.0.0
    secrets:
      - neo4j_password
    volumes:
      - /mnt/synology_docker/GBrain/neo4j:/data
      - /mnt/synology_docker/GBrain/neo4j/logs:/logs
    networks:
      - backend
    healthcheck:
      test: [ "CMD-SHELL", "cypher-shell -u neo4j -p \"$$(cat /run/secrets/neo4j_password)\" 'RETURN 1' >/dev/null 2>&1" ]
      interval: 15s
      timeout: 10s
      retries: 10
      start_period: 45s
    mem_limit: 1536m
    cpus: 1.50
    restart: unless-stopped
    logging: *default-logging

secrets:
  postgres_password:
    environment: GBRAIN_POSTGRES_PASSWORD
  auth_secret:
    environment: GBRAIN_AUTH_SECRET
  neo4j_password:
    environment: GBRAIN_NEO4J_PASSWORD

networks:
  edge:
  backend:
    internal: true
```

### 4.3 Variabili d'Ambiente per il Metodo B (Web Editor)
Sotto l'editor YAML, nella sezione **Environment variables**, selezionare **Advanced mode** e inserire i soli valori applicativi e segreti:

```bash
GBRAIN_ENVIRONMENT=development
GBRAIN_PUBLIC_URL=http://192.168.1.150:8000
POSTGRES_DB=gbrain
POSTGRES_USER=gbrain
GBRAIN_POSTGRES_PASSWORD=CAMBIA_CON_PASSWORD_POSTGRES_GENERATA
GBRAIN_AUTH_SECRET=CAMBIA_CON_SECRET_MIN_32_CARATTERI_GENERATO
GBRAIN_NEO4J_PASSWORD=CAMBIA_CON_PASSWORD_NEO4J_GENERATA
```

Cliccare su **Deploy the stack**. L'avvio sarà istantaneo poiché l'immagine è già presente sul sistema.

---

## 5. Passo 3 — Bootstrap Iniziale dell'Account Owner

GBrain non include utenti predefiniti o password di fabbrica memorizzate nel codice. Il primo avvio attiva un flusso di registrazione esclusivo denominato **Bootstrap Owner**.

1. Aprire il browser all'indirizzo `GBRAIN_PUBLIC_URL` (es. `http://192.168.1.150:8000`).
2. Il sistema riconosce l'assenza di proprietari nel database e presenta la schermata:
   > **GBrain · archivio privato**  
   > **Crea il proprietario**  
   > *Configurazione iniziale unica. Non esiste una password predefinita.*
3. Compilare i tre campi richiesti:
   - **Nome utente (username)**: da 3 a 80 caratteri alfanumerici (es. `gianvito`).
   - **Nome visualizzato (display_name)**: nome completo visualizzato nella UI (es. `Gianvito`).
   - **Password**: password complessa di almeno 14 caratteri.
4. Cliccare su **Crea e accedi**.
5. Il backend calcola l'hash crittografico con Argon2id, memorizza il record su PostgreSQL, sigilla permanentemente il bootstrap e apre la dashboard di GBrain.

---

## 6. Verifica dello Stack e Manutenzione

### 6.1 Verifica dello Stato dei Container
In Portainer -> **Stacks** -> `gbrain`:
- Tutti i 4 container (`app`, `worker`, `postgres`, `neo4j`) devono essere nello stato **running**.
- I servizi `app` e `postgres` devono mostrare lo stato **healthy**.

### 6.2 Verifica della Pagina Fonti
Nella dashboard principale, cliccare sulla scheda **Fonti**. Se non sono state inserite credenziali Gmail in `secrets`, la scheda mostrerà l'integrazione Gmail nello stato **Non configurato**, confermando che lo stack è pienamente integro e operativo.

### 6.3 Aggiornamenti Futuri dello Stack
- **Se si è usato il Metodo A (Repository)**: in Portainer -> Stack `gbrain` -> premere **Pull and redeploy** attivando la spunta *Re-pull image and redeploy*. Portainer scaricherà i nuovi commit e ricompilerà automaticamente.
- **Se si è usato il Metodo B (Web editor)**: eseguire da shell `git pull && docker build -t gbrain:0.1.0 .` nella cartella di build e in Portainer premere **Redeploy**.

### 6.4 Backup e Ripristino dei Dati
I comandi e le procedure formali di backup dei volumi e dei dump database sono documentati in [`docs/BACKUP_RIPRISTINO.md`](docs/BACKUP_RIPRISTINO.md:1) ed eseguibili mediante [`scripts/backup.sh`](scripts/backup.sh:1) e [`scripts/restore.sh`](scripts/restore.sh:1).

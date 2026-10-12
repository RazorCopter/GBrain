# Architettura e Flussi di Sistema

## Panoramica a Livelli e Confini di Fiducia
- **Livello 1: PostgreSQL (Massima Fiducia).** È l'unica entità autorevole delegata ad archiviare lo stato grezzo, lo stato analizzato e la tracciatura delle job queues. Qualsiasi scrittura applicativa deve avvenire originariamente sullo schema relazionale.
- **Livello 2: Backend e Worker Asincroni.** Un container basato su FastAPI ospita le API; la stessa immagine, con comando diverso, esegue il worker Procrastinate sul medesimo PostgreSQL.
- **Livello 3: Proiezione a Grafo (Media Fiducia).** Neo4j Community è previsto come proiezione eventual-consistent alimentata da eventi PostgreSQL. Nella Fase 1 sono presenti connessione e health, ma il sink e la ricostruzione automatica non sono ancora implementati; il backup operativo include quindi anche Neo4j.
- **Livello 4: Servizi Esterni e Connettori (Bassa Fiducia).** Gmail, Immich, NAS, OmniRoute e relativi adattatori sono obiettivi futuri, non integrazioni implementate nella Fase 1. Dovranno essere isolati e protetti da limiti di rete quando verranno introdotti.

## Flusso Canonico Progettato (Pattern Eventuale)

Il seguente flusso è il target architetturale. Nella Fase 1 lo schema outbox esiste, ma il worker elabora solo l'evento placeholder infrastrutturale e non scrive Neo4j.

In base ai requisiti (evitare componenti intermedi quali Redis o Kafka, massimizzare robustezza):
1. **Ingestion transazionale futura:** API o worker riceveranno documenti/entità. In un'unica transazione PostgreSQL aggiorneranno `source_items`/`source_revisions` (ed entità future) e inseriranno un evento in `outbox_events`.
2. **Processazione code futura:** Procrastinate selezionerà gli eventi pendenti, costruirà operazioni idempotenti e contatterà Neo4j sulla rete backend.
3. **Proiezione futura su Neo4j:** query Cypher idempotenti modelleranno entità e relazioni; soltanto dopo il successo l'evento autorevole verrà marcato completato. La cancellazione dell'evento non è la policy corrente.
4. **Graphiti/motore semantico futuro:** un adattatore separato potrà interrogare PostgreSQL e Neo4j senza diventare autorità del dato. Graphiti non è una dipendenza della Fase 1.

## Catalogo Modulare Fonti e Modello Credenziali Cifrate

Con l'integrazione del **Source Connection Wizard**, il sistema introduce un'infrastruttura unificata per la gestione modulare dei connettori esterni e la conservazione crittografica delle credenziali d'accesso.

### 1. Tabella Relazionale `source_credentials`
La persistenza delle configurazioni delle fonti è demandata alla tabella PostgreSQL `source_credentials` (introdotta con la migrazione Alembic [`backend/migrations/versions/20261012_0004_source_credentials.py`](backend/migrations/versions/20261012_0004_source_credentials.py:1)):
- **`id` (UUID):** identificatore univoco del record di credenziali;
- **`source_id` (VARCHAR 64, UNIQUE):** identificativo logico del connettore (es. `gmail`, `immich`, `local_directory`);
- **`client_id` (TEXT, opzionale):** identificativo pubblico del client (es. Google Client ID o ID applicazione);
- **`encrypted_client_secret` (TEXT, opzionale):** secret crittografato (es. Google Client Secret o API Key);
- **`encrypted_extra_config` (TEXT, opzionale):** payload JSON serializzato e crittografato per parametri specifici del connettore (es. endpoint custom, directory root, opzioni di polling);
- **`auth_model` (VARCHAR 32):** modello di autorizzazione adottato (`oauth2_web`, `api_key`, `local_directory`, `webhook`, `none`);
- **`created_at`, `updated_at` (TIMESTAMPTZ):** audit temporale con fuso orario.

### 2. Modello Crittografico AES-256-GCM
Tutti i dati sensibili sono protetti crittograficamente prima della scrittura nel database tramite il modulo [`backend/src/gbrain/application/crypto.py`](backend/src/gbrain/application/crypto.py:1):
- **Algoritmo:** Crittografia autenticata **AES-256-GCM** (Galois/Counter Mode);
- **Chiave Master:** Derivata a 256 bit dalla variabile `GBRAIN_ENCRYPTION_MASTER_KEY` (o dal file secret `GBRAIN_GMAIL_TOKEN_ENCRYPTION_KEY_FILE`);
- **Vettore di Inizializzazione (IV):** Generato in modo crittograficamente sicuro a 96 bit (12 byte) per ogni singola operazione di cifratura;
- **Autenticazione e Integrità (AAD):** Tag di autenticazione a 128 bit calcolato con Additional Authenticated Data contestuale (`source_id`) per impedire attacchi di replay o scambio tra connettori differenti;
- **Encoding:** Payload finale serializzato in base64 URL-safe con prefisso versionato (`v1:...`).

### 3. Risoluzione Ibrida delle Credenziali e Fallback
Il repository [`backend/src/gbrain/infrastructure/source_credentials_repository.py`](backend/src/gbrain/infrastructure/source_credentials_repository.py:1) e il servizio OAuth [`backend/src/gbrain/application/gmail_oauth.py`](backend/src/gbrain/application/gmail_oauth.py:1) attuano una strategia a strati:
1. **Priorità Database:** Il sistema consulta prima la tabella `source_credentials` per individuare credenziali salvate e configurate via interfaccia web;
2. **Fallback Trasparente su Filesystem:** Se il database non contiene credenziali per la fonte richiesta, il sistema verifica l'eventuale presenza di file secret montati dall'ambiente (es. `/run/secrets/gmail_oauth_client`);
3. **Riservatezza Assoluta:** I secret cifrati non vengono mai esposti al frontend né restituiti nelle risposte API; le chiamate GET `/api/sources/catalog` e GET `/api/sources/{source_id}/credentials` riportano unicamente lo stato di configurazione (`is_configured: true/false`), il `client_id` pubblico mascherato e i metadati descrittivi.

## Nessun modello locale nella Fase 1

La Fase 1 non esegue modelli o embedding locali e non effettua chiamate LLM remote. Un eventuale adattatore OmniRoute/API remota appartiene a una fase successiva e dovrà essere introdotto con contratti, limiti e verifiche specifici.

# Sicurezza e operazioni — fondazione Fase 1

## Modello corrente e limiti

PostgreSQL è l'autorità del dato; contiene proprietario, sessioni hashate, stato import/checkpoint, revisioni, `deleted_at`, regole di esclusione, outbox e schema Procrastinate. Neo4j è una proiezione non autorevole, ma viene comunque incluso nel backup coordinato finché non esiste una procedura applicativa implementata per ricostruirlo da PostgreSQL. L'app e il worker condividono immagine/configurazione; il worker ha concorrenza fissata a uno.

Non sono implementati connettori Gmail/Immich/NAS, OmniRoute, Graphiti, MCP, sink outbox→Neo4j, ricostruzione automatica del grafo, gestione allegati/blob o automazione di rotazione secret. La demo è sintetica e non costituisce una pipeline reale.

## Superfici esposte e confini di fiducia

Per default Compose pubblica soltanto il web su `127.0.0.1:8000`. PostgreSQL e Neo4j sono nella rete interna `backend`; worker e database non hanno porte host. Il web appartiene anche alla rete `edge`. Non pubblicare 5432, 7474 o 7687.

Le superfici effettive sono:

- browser/UI e API FastAPI sulla porta web;
- reverse proxy/TLS e DNS, se configurati dall'operatore;
- socket Docker e account amministrativi host;
- `.env`, file sotto `/run/secrets` e supporto DR esterno;
- bind mount e volumi nominati;
- log Docker e output dei comandi operativi;
- originali presenti in `IMPORT_DIR`.

L'accesso al socket Docker permette di leggere/montare dati e secret del deployment: trattarlo come root. La rete `internal` riduce esposizione accidentale ma non sostituisce hardening host, firewall, aggiornamenti e controllo accessi.

## Segreti e permessi

Compose riceve tre secret dall'ambiente host e li monta come file: password PostgreSQL, secret autenticazione, password Neo4j. La configurazione rifiuta valori mancanti/duplicati; il secret autenticazione deve avere almeno 32 caratteri. I container app girano come UID/GID `10001:10001`; PostgreSQL e Neo4j usano gli utenti delle immagini ufficiali.

Regole operative:

1. generare valori distinti con CSPRNG e non riutilizzarli;
2. `.env` con proprietario amministrativo e modalità `0600`, mai in Git o ticket/log;
3. non passare secret sulla command line quando il processo li renderebbe visibili;
4. conservare la copia DR separatamente, cifrata, con accesso e recupero periodicamente verificati;
5. non inserire `.env` o secret nei backup applicativi: lo script li esclude intenzionalmente;
6. non eseguire debug/config dump in canali non protetti;
7. dopo sospetta esposizione, ruotare in finestra controllata e revocare sessioni; non esiste automazione completa di rotazione.

Il cambio di `GBRAIN_AUTH_SECRET` rende non verificabili i token/CSRF precedenti, ma le righe sessione restano finché revocate/scadute/pulite. La rotazione delle password dei datastore deve essere progettata e provata prima dell'uso in produzione.

## Bootstrap e autenticazione

Il bootstrap crea un unico proprietario e si chiude transazionalmente. La password è Argon2id; i token sessione e CSRF sono memorizzati come digest. Cookie sessione: HttpOnly, SameSite Strict; cookie CSRF leggibile dal frontend e confrontato con header e digest server-side.

Controlli:

- completare bootstrap solo dal percorso di rete fidato e sull'URL finale;
- password proprietario unica, lunga e custodita in un password manager;
- in produzione sono obbligatori URL HTTPS e cookie Secure;
- logout revoca server-side; monitorare sessioni inattese;
- `reset-owner` elimina account e sessioni ed è una procedura amministrativa distruttiva: usarla solo dopo backup verificato, da console controllata, con conferma esatta;
- il backup PostgreSQL contiene hash password, sessioni e dati personali: proteggerlo come il database vivo.

## TLS, cookie e reverse proxy

L'app non termina TLS. In produzione un reverse proxy deve fornire TLS moderno, rinnovo certificato e restrizione dell'accesso diretto alla porta app. Impostare `GBRAIN_PUBLIC_URL` su URL HTTPS canonico e `GBRAIN_SESSION_SECURE_COOKIE=true`.

Uvicorn accetta proxy header; pertanto soltanto proxy/reti fidati devono poter raggiungere il listener. Il proxy deve riscrivere/preservare `Host` e protocollo corretti, evitare cache delle API/autenticazione, imporre dimensioni e timeout appropriati e aggiungere header di sicurezza dopo test di compatibilità. Testare cookie e redirect dal browser reale. Non dichiarare TLS sicuro sulla sola base della configurazione statica.

## Modalità demo

`GBRAIN_DEMO_MODE` è false per default e la configurazione la rifiuta in `production`. È **vietato** abilitarla in produzione o usare fixture come dati reali. Il seed/reset è confinato a UUID, prefissi e marker deterministici, ma un backup può contenere fixture create in precedenza. Durante un restore verificare esplicitamente che dati demo non ricompaiano nell'ambiente destinato alla produzione.

## Log, audit e privacy

Il logging applicativo è strutturato e ha una redazione testata per alcuni valori sensibili; non equivale a una garanzia universale. Compose usa `json-file` con `max-size=10m` e `max-file=3`. Uvicorn access log è disabilitato.

- mantenere `INFO`/`WARNING` in produzione salvo debug temporaneo;
- restringere accesso a Docker log e all'eventuale aggregatore;
- non loggare header Cookie/Authorization, payload personali, secret, dump o contenuti importati;
- proteggere terminal history e output dei runbook;
- definire retention log locale coerente con privacy e indagine incidenti;
- registrare backup, restore, reset owner, aggiornamenti e modifiche di configurazione in un audit esterno senza segreti.

Non esiste ancora un audit trail applicativo completo.

## Aggiornamenti versionati e rollback

Le immagini infrastrutturali e base sono pin per versione/digest e le dipendenze sono lockate. La tag applicativa `0.1.0` è locale: per deployment costruire/promuovere artefatti immutabili e registrare digest, commit e manifest.

Procedura minima:

1. leggere advisory/changelog e verificare licenze/compatibilità;
2. creare backup coordinato e verificare checksum;
3. eseguire una prova restore isolata recente;
4. aggiornare pin/lockfile in revisione;
5. build e test deterministici;
6. applicare migrazioni in finestra controllata;
7. verificare readiness, login, owner/sessioni, job/checkpoint, exclusions/deletion records, outbox e worker.

Rollback applicativo: tornare all'immagine precedente solo se compatibile con lo schema già migrato. Rollback schema: non è automatico e le downgrade Alembic non sono una garanzia DR. Per modifiche incompatibili, ripristinare backup PostgreSQL+Neo4j e codice/immagini coerenti in ambiente isolato. Non mischiare stato pre/post migrazione.

## Backup, retention e cancellazioni

Seguire [`BACKUP_RIPRISTINO.md`](BACKUP_RIPRISTINO.md). Punti di sicurezza:

- nessuna copia a caldo dei file database;
- PostgreSQL con `pg_dump` consistente; Neo4j Community con fermo controllato e `neo4j-admin database dump` offline;
- checksum/manifest verificati prima del restore;
- restore distruttivo solo con flag esplicito, stack fermo e destinazione isolata;
- configurazione/secret DR separati e protetti;
- copie su supporti distinti/off-site secondo RPO/RTO locale;
- prove periodiche di restore su runtime reale.

La retention non ha un valore universale: dipende da capacità, RPO/RTO, obblighi e rischio. Un backup storico può contenere dati cancellati o esclusi dopo la sua creazione. Prima di rilasciare un restore occorre preservare/riapplicare lo stato autorevole più recente di `exclusion_rules`, `source_items.deleted_at` ed eventuali futuri deletion record, senza reimportare originali vietati. Non cancellare backup scaduti senza considerare questo rischio e la policy di cancellazione sicura.

## Spazio e backpressure

Monitorare filesystem, inode, Docker data root, log, volumi, `import_jobs`, tabelle Procrastinate e `outbox_events`. La concorrenza worker è uno. Il consumer outbox corrente gestisce soltanto eventi placeholder e non scrive Neo4j; un backlog reale e le policy di retry/dead-letter saranno lavoro futuro.

In esaurimento spazio/backpressure: sospendere produttori/worker in modo controllato, conservare evidenza, verificare un backup, identificare il contributore e aumentare capacità o applicare retention approvata. Non rimuovere volumi, originali, esclusioni o record di cancellazione come rimedio rapido. `CACHE_DIR` non è backup; la sua eliminabilità va confermata in base alle funzionalità future.

## Incidenti e funzioni mancanti

In caso di sospetta compromissione: isolare rete senza spegnimenti distruttivi, conservare log/metadati, revocare accessi, ruotare secret in ordine pianificato, invalidare sessioni e ripristinare solo da backup verificato. Le procedure di incident response, osservabilità, alert, MFA, rate limiting, CSP/HSTS del proxy, scansione vulnerabilità, crittografia at-rest e gestione chiavi non sono implementate dal repository e devono essere decise nell'ambiente operativo.

# Backup, ripristino e prova isolata — Fase 1

## Stato di verifica

Gli script [`scripts/backup.sh`](../scripts/backup.sh) e [`scripts/restore.sh`](../scripts/restore.sh) automatizzano il flusso coerente con [`compose.yaml`](../compose.yaml). In questo ambiente il daemon Docker non è disponibile: **non è stato eseguito né simulato alcun backup/restore reale**. Sintassi, riferimenti e config Compose sono oggetto delle verifiche registrate in [`PROJECT_STATUS.md`](../PROJECT_STATUS.md); la compatibilità effettiva di `neo4j-admin`, i permessi dei volumi e l'intero restore restano da provare su runtime Linux reale tramite [`scripts/phase1_runtime_test.sh`](../scripts/phase1_runtime_test.sh).

Per stack isolati il backup registra nel manifest il `COMPOSE_PROJECT_NAME` effettivo. Il restore richiede `GBRAIN_RESTORE_SOURCE_PROJECT` uguale al progetto nel manifest e rifiuta un `COMPOSE_PROJECT_NAME` target identico, così da impedire la sovrascrittura dello stack sorgente. Per il solo collaudo del recovery, `GBRAIN_BACKUP_TEST_FAIL_AFTER_QUIESCE=true` inietta un errore dopo lo stop di app/worker e prima dei dump; è disabilitato per default.

## Garanzie e confini

- PostgreSQL: dump logico custom con `pg_dump`, consistente per il database, non copia dei file a caldo.
- Neo4j Community 5.26.12: dump offline con `neo4j-admin database dump`; richiede una finestra di fermo.
- Coerenza applicativa: app e worker vengono fermati prima dei dump. PostgreSQL viene dumpato, poi Neo4j viene fermato e dumpato; il fermo Neo4j è limitato alla fase offline. Questo produce una coppia coerente con lo stato quiescente della Fase 1.
- Manifest: immagini, versioni server, revisione Alembic, commit Git quando disponibile, metodi e nomi file.
- Integrità: SHA-256 per manifest, dump e archivio configurazione; directory finale pubblicata solo dopo la verifica locale.
- Segreti: `.env` e valori segreti non vengono inclusi. L'archivio configurazione contiene Compose, Dockerfile, manifest/lockfile e migrazioni.
- Restore: richiede conferma distruttiva esatta, checksum validi, immagini/versioni compatibili, quattro container esistenti ma tutti fermi e un ambiente isolato predisposto con volumi nuovi.
- Lo script lascia i datastore fermi dopo il restore; non pubblica né avvia app/worker.

Limiti: non viene copiato automaticamente il contenuto host di `DATA_DIR`, `IMPORT_DIR` o `BACKUP_DIR`; `CACHE_DIR` è esclusa per definizione. Nella Fase 1 tali directory non sono l'autorità database, ma futuri file `storage_ref` richiederanno una policy e un backup file coerente. Neo4j è concettualmente ricostruibile da PostgreSQL, ma il sink/rebuild non è implementato: perciò si ripristina anche il dump Neo4j.

## Prerequisiti

Eseguire su Linux con shell POSIX, Docker Engine, Compose v2, `sha256sum`, `tar`, `awk`, `grep`, `tr` e spazio libero sufficiente. Lo stack deve essere quello del repository e usare le immagini pin. L'operatore deve poter controllare Docker e deve aver verificato `.env`, mount e secret senza stamparli.

Impostare permesso eseguibile dopo il checkout se necessario:

```console
chmod 0750 scripts/backup.sh scripts/restore.sh
```

Usare percorsi assoluti. La destinazione backup deve esistere, essere una directory reale (non symlink), scrivibile e su un filesystem affidabile. Evitare `BACKUP_DIR` sullo stesso fault domain dei volumi database come unica copia.

## Backup operativo

### Preflight

1. verificare health, spazio/inode e assenza di manutenzioni concorrenti;
2. registrare RPO desiderato e finestra di fermo;
3. accertarsi che app e worker possano essere fermati; lo script rifiuta lo stato anomalo worker-attivo/app-ferma;
4. controllare che la destinazione abbia spazio per dump PostgreSQL, Neo4j e archivio config;
5. notificare l'interruzione: app/worker restano fermi per entrambi i dump; Neo4j è fermato solo per il dump offline.

Esecuzione:

```console
./scripts/backup.sh --destination /percorso/assoluto/backup
```

Variabili opzionali:

```console
GBRAIN_BACKUP_DESTINATION=/percorso/assoluto/backup
GBRAIN_BACKUP_STOP_TIMEOUT=45
```

Non eseguire due backup contemporanei: lo script usa un lock nella destinazione. In caso di errore tenta di riavviare soltanto i servizi che erano attivi, rimuove staging/temp noti e non pubblica una directory finale incompleta. Verificare comunque lo stato:

```console
docker compose --profile compact ps
docker compose --profile compact logs --tail=200 app worker postgres neo4j
```

### Contenuto

Ogni directory `gbrain-<UTC>-<PID>` contiene esattamente:

- `postgresql.dump`: formato custom, include direttive clean/if-exists registrate al momento del dump;
- `neo4j.dump`: dump offline del database `neo4j`;
- `configuration.tar`: soli file di configurazione/migrazione non segreti necessari a identificare/riprodurre il software;
- `manifest.tsv`: metadati a righe chiave-tab-valore, senza password;
- `checksums.sha256`: checksum dei quattro file precedenti.

Verifica manuale non distruttiva:

```console
cd /percorso/assoluto/backup/gbrain-...
sha256sum --check --strict checksums.sha256
```

Copiare poi la directory completa su supporto separato/off-site mantenendo permessi e checksum; riverificare dopo la copia.

## Configurazione protetta per disaster recovery

Conservare **separatamente** dal backup dati, in vault/archivio cifrato:

- i tre secret correnti e la loro data/versione;
- `.env` o una scheda equivalente con URL, ambiente, tag immagine e quattro percorsi mount;
- configurazione reverse proxy, DNS/TLS e firewall;
- mapping UID/GID LXC e procedura di mount;
- policy di retention/RPO/RTO e contatti;
- eventuali credenziali del registry e digest immagine applicativa.

Non aggiungere questi valori a `configuration.tar`, manifest, ticket o log. Verificare periodicamente che il vault sia recuperabile da persone autorizzate. Un restore senza lo stesso `GBRAIN_AUTH_SECRET` non conserva l'utilizzabilità delle sessioni; password datastore e config devono essere coerenti con i container target.

## Policy dei dati e retention

Trattare separatamente:

- **volumi PostgreSQL/Neo4j**: coperti dai dump coordinati;
- **`DATA_DIR`**: oggi non contiene file autorevoli creati dalla fondazione, ma qualsiasi futuro dato attivo/storage reference va copiato con procedura consistente e verificato;
- **`IMPORT_DIR`**: originali/staging; decidere se la fonte resta recuperabile o se questi sono originali unici. Non cancellarli dopo un dump DB senza policy;
- **`CACHE_DIR`**: cache, mai backup; non includerla salvo requisito futuro documentato;
- **`BACKUP_DIR`**: area di appoggio, non unica copia e non necessariamente inclusa in snapshot LXC;
- **secret/config DR**: archivio separato e cifrato.

Retention: definire numero/frequenza in base a RPO/RTO, crescita, spazio, criticità e obblighi; non esiste un valore universale. Includere backup recenti, punti prima di update/migrazioni e almeno una copia fuori fault domain; effettuare restore test periodici prima di eliminare l'ultima copia nota buona.

**Cancellazioni pregresse:** un backup conserva lo stato al suo timestamp e può contenere dati eliminati o esclusi successivamente. Tenere un registro autorevole protetto dello stato recente di `exclusion_rules`, `source_items.deleted_at` ed eventuali futuri deletion record. Dopo il restore, confrontarlo e riapplicarlo prima di consentire import/worker o rete di produzione. La Fase 1 non fornisce un comando automatico di merge delle cancellazioni: l'operazione deve essere progettata e revisionata, non improvvisata con SQL distruttivo.

## Restore: preparazione obbligatoria

Il restore sovrascrive lo schema PostgreSQL e la destinazione Neo4j. Non puntarlo mai direttamente a un deployment personale/produzione.

1. predisporre host/LXC o progetto di prova isolato, senza route/reverse proxy/DNS di produzione e senza egress non necessario;
2. usare una copia checkout coerente con il manifest e una `.env` recuperata dal vault, con `WEB_BIND_ADDRESS=127.0.0.1`, `GBRAIN_DEMO_MODE=false` e percorsi mount di prova vuoti;
3. verificare i digest delle immagini registrate; non usare `latest`;
4. creare **nuovi volumi vuoti** inizializzando una volta i quattro container, senza bootstrap/login/demo e senza dati esterni:

```console
docker compose --profile compact up -d postgres neo4j
docker compose --profile compact create app worker
docker compose --profile compact stop
```

5. verificare che `docker compose --profile compact ps --status running --services` non elenchi alcun servizio;
6. non usare una directory/volumi condivisi con produzione. Lo script rifiuta stack attivi e immagini/versioni diverse dal manifest, ma non può dimostrare da solo l'isolamento di rete o che i volumi siano davvero nuovi.

Esecuzione, con percorso assoluto e frase esatta:

```console
./scripts/restore.sh \
  --backup /percorso/assoluto/backup/gbrain-... \
  --confirm RESTORE-GBRAIN-ERASE-TARGET
```

Il flusso:

1. valida lista file checksum, digest, manifest e nomi sicuri;
2. richiede immagini applicativa/PostgreSQL/Neo4j uguali al backup;
3. avvia soltanto PostgreSQL, controlla versione e assenza di connessioni, quindi applica il dump con pulizia esplicita incorporata;
4. verifica la revisione Alembic ripristinata e ferma PostgreSQL;
5. carica Neo4j offline con overwrite esplicito nella destinazione isolata;
6. avvia temporaneamente solo i datastore, esegue `app migrate` come container one-shot, ricontrolla la revisione;
7. ferma nuovamente i datastore. App e worker restano fermi.

Il restore non ripristina `.env`, secret, mount originali né pubblicazione proxy. Non tenta downgrade schema. Se fallisce, lascia/tenta di lasciare i datastore fermi: considerare il target contaminato e ricominciare da volumi nuovi, senza riutilizzarlo alla cieca.

## Runbook prova restore isolata

Tutti i punti seguenti sono **DA ESEGUIRE SU RUNTIME LINUX REALE** e da registrare con timestamp/esito. Finché non eseguiti non costituiscono garanzia.

### A. Integrità e isolamento

- [ ] checksum SHA-256 superati prima e dopo il trasferimento;
- [ ] manifest letto e confrontato con commit, immagini, PostgreSQL, Neo4j e revisione Alembic;
- [ ] target usa volumi/mount nuovi e nomi/progetto chiaramente non produzione;
- [ ] nessun collegamento a proxy, DNS, rete o credenziale di produzione;
- [ ] nessuna porta pubblica; se serve UI, bind esclusivo a loopback e tunnel amministrativo;
- [ ] egress bloccato/non necessario; nessun connettore reale configurato;
- [ ] `GBRAIN_DEMO_MODE=false`.

### B. Restore e schema

- [ ] script completato, datastore lasciati fermi;
- [ ] migrazioni a `head` senza errori e `alembic_version` attesa;
- [ ] tabelle applicative e Procrastinate presenti;
- [ ] conteggi/timestamp plausibili rispetto al backup;
- [ ] PostgreSQL autorevole e Neo4j leggibile con versioni attese;
- [ ] nessuna credenziale o secret presente nei file backup/manifest/log.

### C. Stato autorevole prima di avviare il worker

Con datastore avviati ma app/worker ancora non esposti, produrre query revisionate e in sola lettura per verificare:

- [ ] unico owner, username/display name e hash password presenti; nessuna password in chiaro;
- [ ] sessioni con expiry/revoca coerenti; decidere se revocarle tutte prima del rilascio secondo incidente/scopo;
- [ ] `import_jobs`: stati, contatori, errori e checkpoint coerenti, nessun job `running` orfano ignorato;
- [ ] `source_items.deleted_at` e qualsiasi deletion record futuro preservati;
- [ ] `exclusion_rules` complete e attive/scadute coerentemente;
- [ ] `outbox_events`: conteggi per stato, eventi `processing` orfani e retry/errori analizzati;
- [ ] code Procrastinate: job pendenti/falliti/attivi analizzati prima di consentire consumo;
- [ ] stato cancellazioni/esclusioni più recente del backup confrontato e riapplicato con procedura approvata;
- [ ] `IMPORT_DIR` di prova non contiene originali demo, esclusi o cancellati pronti a essere reimportati.

Non avviare il worker finché gli stati `running`/`processing` e le esclusioni non sono stati risolti. La Fase 1 non fornisce automazione generica per questa riconciliazione.

### D. Avvio controllato e funzionalità

Avviare prima `postgres` e `neo4j`, poi `app`; mantenere il worker fermo:

```console
docker compose --profile compact start postgres neo4j
docker compose --profile compact start app
curl -fsS http://127.0.0.1:8000/health/live
curl -fsS http://127.0.0.1:8000/health/ready
```

- [ ] liveness 200 e readiness 200; Neo4j riportato correttamente;
- [ ] login del proprietario riuscito con TLS/loopback di prova;
- [ ] cookie/sessione/CSRF funzionanti; sessioni da revocare non valide;
- [ ] schermate e conteggi import coerenti;
- [ ] nessun dato demo se il backup/ambiente non dovrebbe contenerlo;
- [ ] dati esclusi/cancellati non ricompaiono nella UI o tra i candidati all'import.

Poi avviare il worker solo dopo snapshot dei conteggi:

```console
docker compose --profile compact start worker
docker compose --profile compact logs --tail=200 worker app
```

- [ ] processo worker healthy e heartbeat/consumo controllato osservato;
- [ ] nessuna riesecuzione indesiderata di job/import;
- [ ] outbox non duplica né fa riapparire dati esclusi/cancellati;
- [ ] checkpoint avanza solo se previsto;
- [ ] nessun dato demo ricreato;
- [ ] restart controllato mantiene stato e readiness.

Il healthcheck worker da solo controlla soltanto la vita del processo e non soddisfa il criterio di consumo.

### E. Chiusura della prova

- [ ] conservare report con versioni, tempi RTO, esiti e anomalie, senza dati personali/secret;
- [ ] fermare lo stack di prova;
- [ ] mantenere isolato il target finché il responsabile non approva tutti i criteri;
- [ ] distruggere volumi di prova soltanto con autorizzazione esplicita e dopo aver preservato il report;
- [ ] aggiornare runbook/RPO/RTO in base ai tempi misurati.

Solo dopo il completamento documentato è lecito indicare quel backup come “restore provato” per quella combinazione di versioni. La prova non certifica backup futuri.

## Ripristino in emergenza e rilascio

In un incidente reale, preferire il restore in un nuovo ambiente isolato e lo switch controllato rispetto alla sovrascrittura in-place. Prima del rilascio:

1. completare i criteri A–E;
2. riapplicare esclusioni/cancellazioni successive al backup;
3. revocare sessioni/ruotare secret se richiesto dall'incidente;
4. verificare originali e mount senza collegarli prematuramente;
5. collegare proxy/rete soltanto alla fine;
6. osservare spazio, code, errori e login;
7. conservare il vecchio ambiente isolato finché scade la finestra di rollback.

Non presumere che snapshot LXC contengano mount esterni e non usare copie a caldo dei file database come fallback.

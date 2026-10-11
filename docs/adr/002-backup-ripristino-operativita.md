# ADR 0002: backup coordinato, restore isolato e confini operativi

- **Stato:** accettata per la fondazione Fase 1; prova runtime pendente
- **Data:** 11 ottobre 2026

## Contesto

PostgreSQL è l'autorità del dato e ospita anche sessioni, job/import checkpoint, esclusioni, cancellazioni logiche, outbox e code Procrastinate. Neo4j Community è una proiezione secondaria, ma in Fase 1 non esiste ancora un sink o comando di ricostruzione da PostgreSQL. I datastore risiedono in volumi Docker nominati; quattro bind mount applicativi hanno finalità distinte. Copie a caldo dei file database o snapshot LXC non garantiscono coerenza né inclusione dei mount esterni.

Serve una procedura conservativa che non contenga segreti, non sovrascriva target ambigui e non dichiari test non eseguiti.

## Decisione

1. **PostgreSQL:** backup logico custom con `pg_dump`, non copia del volume. App e worker vengono prima messi in quiescenza.
2. **Neo4j Community 5.26.12:** `neo4j-admin database dump` offline, con fermo controllato di Neo4j. Il fermo è accettato per compatibilità Community e riduzione del rischio di dump incoerente.
3. **Set coordinato:** un'unica directory immutabile contiene dump PostgreSQL, dump Neo4j, configurazione non segreta, manifest e SHA-256. La directory finale compare solo a completamento.
4. **Segreti:** esclusi dal backup e conservati con procedura DR separata, cifrata e controllata.
5. **Restore:** consentito soltanto con conferma esatta, checksum/manifest/versioni validi, stack interamente fermo e ambiente isolato predisposto con volumi nuovi. Il restore lascia i datastore fermi.
6. **Schema:** dopo il restore viene eseguito `alembic upgrade head`; non si usa un downgrade automatico come strategia DR. Il rollback incompatibile ripristina codice/immagini e datastore come insieme coerente.
7. **Autorità e cancellazioni:** PostgreSQL resta autorevole. Prima del rilascio si preservano o riapplicano exclusions/deletion state posteriori al backup; import, worker e rete produzione restano disabilitati fino alla verifica.
8. **Mount:** `CACHE_DIR` non è backup; `DATA_DIR`, `IMPORT_DIR` e `BACKUP_DIR` hanno policy distinte. Futuri file autorevoli richiederanno una procedura file coerente aggiuntiva.
9. **Ambiente LXC:** nessuna modifica automatica a Proxmox o escalation a LXC privilegiato. I limiti cgroup, mapping UID/GID, mount e spazio devono essere verificati sul runtime reale.

## Alternative respinte

- **Copia a caldo dei volumi PostgreSQL/Neo4j:** rischio di inconsistenza e procedura non supportata.
- **Solo snapshot LXC:** può escludere mount esterni e non assicura quiescenza applicativa/datastore.
- **Solo PostgreSQL:** architetturalmente auspicabile quando esisterà il rebuild del grafo, ma non operativamente sufficiente nella Fase 1 corrente.
- **Dump Neo4j online Enterprise:** non assunto perché l'immagine è Community e non si introducono funzionalità/licenze non presenti.
- **Restore in-place automatico:** rischio di overwrite del deployment corretto e ambiguità su connessioni/mount.
- **Includere `.env` nel backup:** aumenta esposizione e confonde ciclo di retention dati con quello dei segreti.

## Conseguenze

- Il backup comporta indisponibilità di app/worker e una finestra Neo4j offline; è semplice e verificabile.
- Servono spazio temporaneo nel volume Neo4j e nella destinazione host.
- Restore e versioni esatte devono essere provati periodicamente su Linux reale; la sola syntax/config validation non li certifica.
- Un backup storico può reintrodurre dati poi cancellati: il rilascio richiede riconciliazione delle cancellazioni/esclusioni.
- RPO/RTO e retention sono decisioni locali misurate, non costanti imposte dal repository.
- Quando sarà implementata una ricostruzione Neo4j deterministica, questa ADR potrà essere rivista per rendere facoltativo il dump grafo.

## Evidenze e runbook

Implementazione: [`scripts/backup.sh`](../../scripts/backup.sh), [`scripts/restore.sh`](../../scripts/restore.sh). Procedura e criteri di prova: [`docs/BACKUP_RIPRISTINO.md`](../BACKUP_RIPRISTINO.md). Preflight LXC: [`docs/OPERAZIONI_LXC.md`](../OPERAZIONI_LXC.md). Stato reale delle verifiche: [`PROJECT_STATUS.md`](../../PROJECT_STATUS.md).

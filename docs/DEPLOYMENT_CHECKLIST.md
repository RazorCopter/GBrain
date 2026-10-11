# Checklist deployment Linux/LXC — fondazione Fase 1

Questa checklist non sostituisce il runbook dettagliato [`OPERAZIONI_LXC.md`](OPERAZIONI_LXC.md), le regole in [`SICUREZZA.md`](SICUREZZA.md) o la procedura [`BACKUP_RIPRISTINO.md`](BACKUP_RIPRISTINO.md). Non autorizza modifiche Proxmox, LXC privilegiati o installazioni NAS.

## Discovery e prerequisiti

- [ ] Host/LXC Linux x86_64, versione OS/kernel registrata.
- [ ] Memoria, swap, CPU, disco e inode misurati con margine oltre i circa 3,8 GiB di limiti container dichiarati.
- [ ] cgroup disponibile e limiti verificabili nel runtime, non solo nel file Compose.
- [ ] Docker Engine e Compose v2 operativi; versione/storage driver/data root registrati.
- [ ] Spazio Docker data root sufficiente per immagini, build, volumi e backup temporanei.
- [ ] Nessuna necessità di rendere l'LXC privilegiato; mapping UID/GID e prerequisiti risolti con l'amministratore.

## Mount

- [ ] Percorsi assoluti, distinti e non simbolici scelti per `DATA_DIR`, `CACHE_DIR`, `IMPORT_DIR`, `BACKUP_DIR`.
- [ ] `findmnt`, filesystem/opzioni, capacità/inode e disponibilità al boot verificati.
- [ ] UID/GID `10001:10001` può scrivere data/cache/backup e leggere import nel namespace effettivo.
- [ ] `IMPORT_DIR` è read-only nei container.
- [ ] Cache non trattata come backup; originali import e dati attivi hanno policy esplicita.
- [ ] Snapshot LXC non assunto come copertura dei mount esterni.
- [ ] Nessun percorso NAS inventato o modifica NAS effettuata.

## Configurazione e segreti

- [ ] `.env` fuori Git, modalità `0600`, backup DR cifrato e separato.
- [ ] Tre secret CSPRNG diversi; auth secret almeno 32 caratteri.
- [ ] `GBRAIN_ENVIRONMENT=production`, `GBRAIN_DEMO_MODE=false`.
- [ ] `GBRAIN_PUBLIC_URL` HTTPS canonico e cookie Secure attivo.
- [ ] Tag/digest versionati; nessun `latest`.
- [ ] Configurazione risolta con `docker compose --profile compact config --quiet` senza pubblicare output sensibile.

## Rete, proxy e TLS

- [ ] Porta web libera e bind esplicito; loopback se proxy locale.
- [ ] PostgreSQL e Neo4j non pubblicati sull'host.
- [ ] DNS, certificato/rinnovo TLS, firewall e reverse proxy verificati end-to-end.
- [ ] Accesso diretto alla porta app limitato a proxy/reti fidati per l'uso dei forwarded header.
- [ ] Nessuna connessione a Gmail, Immich, OmniRoute, NAS o Graphiti in Fase 1.
- [ ] Versioni/API/URL Immich e OmniRoute registrati soltanto come input futuri, senza token.

## Avvio e accettazione

- [ ] Build/pull completati sul runtime reale.
- [ ] Quattro servizi avviati; health e restart count coerenti.
- [ ] Migrazione a head completata su PostgreSQL reale.
- [ ] Limiti CPU/memoria/shm osservati con inspect/stats/cgroup.
- [ ] Bootstrap single-owner e login provati sul percorso TLS finale.
- [ ] Worker a concorrenza uno avviato; ricordare che il suo healthcheck prova solo la vita del processo.
- [ ] Nessuna fixture demo presente in produzione.
- [ ] Log ispezionati senza secret; retention/permessi definiti.

## Operazioni e continuità

- [ ] Monitoraggio spazio/inode/OOM/restart/readiness e backlog job/outbox definito.
- [ ] Soglie e risposta a backpressure definite localmente.
- [ ] Backup coordinato completato con checksum e copia fuori fault domain.
- [ ] Secret/configurazione DR recuperabili separatamente.
- [ ] Retention definita da RPO/RTO, senza valore universale imposto.
- [ ] Prova restore isolata completata sul runtime reale, senza rete produzione.
- [ ] Owner/sessioni, import job/checkpoint, exclusions/deletion state, outbox, login/readiness/worker verificati.
- [ ] Processo per riapplicare cancellazioni/esclusioni successive al backup approvato.
- [ ] Rollback applicativo/schema pianificato; nessun downgrade implicito.

## Informazioni ancora mancanti

Prima di un deployment reale restano da acquisire: host/LXC e policy Proxmox effettivi; filesystem e percorsi dei mount; capacità/RPO/RTO; DNS/proxy/certificati; registry/digest dell'immagine applicativa; monitoring/alert; procedura di vault; versioni e contratti futuri di Gmail, Immich e OmniRoute. L'assenza di questi input non deve essere colmata con assunzioni nel repository.

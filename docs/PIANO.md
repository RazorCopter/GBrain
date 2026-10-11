# Piano Incrementale D'Implementazione GBrain

## FASE 0: Concettuale e d'Inventario (storica)
- In assenza di script fisici, formalizzazione contrattuale dei servizi e delle intavolazioni primarie dei database.
- Registrazione assenza di file pregresse (repo pulita).
- Generazione strutturata dello schema mentale PostgreSQL -> Outbox -> Grafo.

## FASE 1: Prima Fondazione Eseguibile (codice completato, runtime reale pendente)

**Obiettivo:** prima iterazione Compose verificabile in sviluppo.

Esistono immagine condivisa app/worker, migrazione infrastrutturale PostgreSQL/outbox/schema Procrastinate, autenticazione, UI, health e fixture demo. OmniRoute, estrazione semantica, sink Neo4j e connettori non sono implementati. Backup/restore e operatività sono documentati e automatizzati, ma richiedono prova su runtime Linux reale come indicato in [`PROJECT_STATUS.md`](../PROJECT_STATUS.md).

## FASE 2: Gmail `SENT`

Il backlog operativo, i gate di sicurezza e i criteri di accettazione sono in [`PIANO_FASE2_GMAIL.md`](PIANO_FASE2_GMAIL.md). L'ordine vincolante è: audit dei due MCP candidati e del client Gmail ufficiale; decisione di un solo sincronizzatore; contratto e fixture senza rete; enforcement `SENT`, MIME, paginazione/checkpoint/reconcile/idempotenza e prompt injection; ricerca testuale con citazioni; soltanto infine OAuth read-only e collaudo reale autorizzato. Nessun accesso Gmail è stato effettuato nella Fase 1.

## FASE 3: Neo4j Sink e Graphiti
1. Il worker legge il registro differenziale (Outbox) PG.
2. Formatta record compatibili per UPSERT di nodi.
3. Inietta in locale a un container generico Neo4j Community.
4. Applicazione layer adapter Graphiti retrocompatibile.

## FASE 4: Aggancio Produzione
1. Implementazione di *Secret Management* reali sulle route di Connettore.
2. Lettura NAS effettivo per l'indexer documentale (non simulato).
3. Test sui rate-limits fisici verso API d'inferenza esterne.

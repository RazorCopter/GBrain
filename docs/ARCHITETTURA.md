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

## Nessun modello locale nella Fase 1

La Fase 1 non esegue modelli o embedding locali e non effettua chiamate LLM remote. Un eventuale adattatore OmniRoute/API remota appartiene a una fase successiva e dovrà essere introdotto con contratti, limiti e verifiche specifici.

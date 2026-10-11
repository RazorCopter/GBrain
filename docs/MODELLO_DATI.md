# Modello Dati Concettuale Minimo

> **Stato:** modello target concettuale della Fase 0. Nella fondazione Fase 1 esistono `owner_accounts`, `auth_sessions`, `system_state`, `import_jobs`, `source_items`, `source_revisions`, `outbox_events`, `exclusions`, `deletion_records` e le tabelle Procrastinate. Il sottocompito C Gmail aggiunge `connector_accounts`, `source_policies`, `gmail_checkpoints` e campi Gmail/provenienza/evidence/stato alle source item/revisioni tramite migrazione incrementale. I nomi `Source`, `RawMaterial`, `Artifact` ed `EpistemicNode` sotto restano concetti futuri e non provano tabelle o funzionalità ulteriori.

Il Modello definisce un approccio duale tra l'immagazzinamento solido e coerente (PostgreSQL) e l'interrogazione associativa-semantica (Grafo, tramite Neo4j).

## 1. Strutture canoniche target su PostgreSQL
Le tabelle relazionali gestiscono stato, temporalità e provenienza inequivocabile:
- **`Source`**: Cataloghi macro di integrazione (es. "Account Utente: X@gmail.com", "NAS: /media/backup").
- **`RawMaterial`**: L'evento o il documento salvato integralmente. Payload JSON persistente, timestamp sorgente e flag di processamento.
- **`Artifact`**: Elemento omogeneizzato e convalidato (es. Email parsatta con corpo Testo-In-Chiaro pulito o Meta-data EXIF Immich unificato). Collegate all'ingresso RawMaterial, formano base stabile.
- **`EpistemicNode`**: Entità astratte scoperte (es, Persone, Luoghi, Eventi storiche, Aziende, Progetti). Estratti mediante l'intelligenza semantica del layer LLM.
- **`OutboxEvents`**: Tabella di coda effimera utilizzata unicamente per la coordinazione asincrona (insert generante un UUID e un payload che descrive l'evento CRUD su un nodo/artefatto da inoltrare verso il grafo).

## 2. Proiezione su Grafo (Neo4j)
Per non sovraffaticare la risorsa Neo4j, vengono archiviati _solo le relazioni e proprietà di meta-navigazione_:
- **Nodi Neo4j**: Rappresentano esclusivamente riferimenti agli `EpistemicNode` o agli `Artifact` del database PostgreSQL, ospitando label generici. Il grafo **non replica testi lunghi, HTML o blobs**.
- **Archi / Relazioni Neo4j**: Descrivono l'aderenza ontologica tra istanze (es. "PARTECIPANTE_A", "LOCATO_IN", "IN_REPLY_TO"). 
- **Graph RAG / Identità**: Qualora un adapter per _Graphiti_ richieda manipolazioni complesse o proprietà addizionali temporanee per l'algoritmo semantico di discovery RAG, tale layer manterrà tali metriche di peso confinato al volume di storage Neo4j, restando derivate e disposte ad eventuale cancellazione secolare senza intaccare l'archivio testuale sorgente (che risiede sempre in PG).

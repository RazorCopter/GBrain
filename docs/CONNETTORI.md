# Contratto prospettico dei connettori

> **Non implementato nella Fase 1.** Questa è una specifica per le fasi successive, non documentazione di API o MCP disponibili. Il primo passo previsto è Gmail `SENT` con fixture/stub deterministici; Immich, NAS, MCP e accessi reali restano fuori dal codice corrente.

L'interfaccia futura verso Gmail, Immich e Network Attached Storage dovrà usare logica pull, batch controllati, idempotenza e checkpoint persistenti per riprendere pipeline lunghe dopo un arresto.

## Operazioni e Interfacce

Ogni futuro connettore dovrà implementare la seguente specifica funzionale minima:

1. `test_connection() -> status`  
   Ping a un endpoint protetto utile per misurare l'identità o stabilire permessi.

2. `inspect_capabilities() -> dict`  
   Restituisce i massimali (rate-limit contrattualizzati, estensioni supportate, capacità di ordinamento nativo e paginazione).

3. `preview_selection(criteria) -> metrics`  
   Analizza quanto un pool logico restituirà per estrarre statistiche pre-consumo.

4. `enumerate_page(cursor) -> [OutputLocator]`  
   Recupera un pool limitato e scalabile di puntatori a eventi/documenti (es. IDs delle transazioni, hash dei commit).

5. `fetch_item(locator) -> RawData`  
   Richiede il transito fisico del payload o del contenuto di un ID precedentemente ritornato. Assicura ritentativi passivi (exponential backoffs).

6. `normalize_item(raw_data) -> NormalizedSchema`  
   Decosta il formato proprietario, emettendo la controparte allineata col [MODELLO DATI](./MODELLO_DATI.md) applicativo (schema Pydantic stringente).

7. `checkpoint(state_locator) -> persistence`  
   Richiamato dalla pipeline superiore, fissa l'alto corso di scaricamento su Data Base per permettere riprese lineari all'effettuarsi di un riavvio imprevisto dell'applicativo (es. Out Of Memory).

8. `reconcile() -> report_diffs`  
   Verifica il discostamento fra lo snapshot posseduto attualmente e un set modificato dalla sorgente (fondamentale per intercettare soft deletes o tombstone non comunicate attivamente tramite callback).

## Idempotenza
Nessun connettore sa nulla di Neo4j. L'assunzione dei connettori è d'inserimento o merge condizionato in base a chiavi uniche (hash sha256 + ID sistema) verso gli endpoints di ingestione centrali di PostgreSQL.

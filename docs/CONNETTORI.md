# Contratto dei connettori

> **Stato 11 ottobre 2026:** contratto, nucleo e integrazione Gmail `SENT` (OAuth web server-side, token cifrati AES-256-GCM, worker Procrastinate, API private con CSRF, ricerca FTS, pagina Fonti UI e audit consolidato Sottocompito E con 93 test deterministici) sono implementati e verificabili senza rete. Collaudi reali con Docker/Linux e Google Cloud restano pendenti; nessuna credenziale reale è stata usata.

Il contratto in [`backend/src/gbrain/domain/connectors.py`](../backend/src/gbrain/domain/connectors.py) usa protocolli e dataclass generici senza esporre oggetti dinamici Google. Gmail specializza selezione, cursor, locator, raw message, checkpoint e risultato in [`backend/src/gbrain/domain/gmail.py`](../backend/src/gbrain/domain/gmail.py). Setup operativo e restrizioni sono in [`docs/GMAIL_OAUTH_SETUP.md`](GMAIL_OAUTH_SETUP.md).

## Operazioni e Interfacce

Ogni connettore deve implementare la seguente specifica funzionale minima:

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

## Gmail `SENT` implementato fixture-first

Il nucleo implementato comprende:

- scope esatto `https://www.googleapis.com/auth/gmail.readonly`; l'interfaccia non contiene send, modify, drafts o threads;
- gateway ufficiale in [`backend/src/gbrain/infrastructure/gmail_gateway.py`](../backend/src/gbrain/infrastructure/gmail_gateway.py), con `labelIds=["SENT"]`, query temporale costruita internamente, fetch `full`/`metadata`, verifica post-fetch della label e history sempre rifetchata;
- preview senza fetch/persistenza/checkpoint, paginazione `nextPageToken`, limite elementi, budget richieste/elementi, stato completo/parziale/troncato e concorrenza dichiarata uno;
- motore full/incrementale/reconcile in [`backend/src/gbrain/application/gmail_sync.py`](../backend/src/gbrain/application/gmail_sync.py), con batch/pagina transazionale, replay idempotente, history scaduta marcata per riconciliazione e rimozione logica senza cancellare Gmail;
- parser MIME limitato in [`backend/src/gbrain/application/gmail_mime.py`](../backend/src/gbrain/application/gmail_mime.py): multipart, base64url, charset, HTML convertito in testo senza fetch remoti, metadata allegati e separazione euristica deterministica tra nuovo testo, firma, citazione e inoltro;
- attribuzione certa soltanto quando il solo mittente coincide con un alias owner confermato; il solo `From` non registra nuovi alias;
- contenuti marcati non fidati: URL e prompt injection restano dati citabili e non influenzano policy o strumenti.

L'OAuth UX, la creazione/refresh/revoca reale delle credenziali e il collegamento al worker sono punti di integrazione successivi. Nessun token reale è presente o collaudato.

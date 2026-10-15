# ADR-004 — Integrazione Immich read-only

**Stato:** Accettata e implementata nel working tree  
**Data:** 2026-10-15

## Decisione

GBrain integra Immich esclusivamente come fonte metadata-only. URL e API key sono cifrati a riposo; nessun segreto entra nei payload Procrastinate o nei log. Originali, thumbnail e binari restano in Immich. Non sono previste chiamate mutative, LLM vision o proiezioni nel grafo.

Il percorso verticale è: configurazione URL/API key → verifica autenticazione, versione e capacità → selezione album XOR periodo UTC → anteprima senza scritture → import background di massimo 25 asset → consultazione dei metadati persistiti.

## Contratto API e version gating

La famiglia API supportata usa:

- `GET /api/server/version` per la versione; solo se risponde `404` viene tentato il percorso legacy `GET /api/server-info/version`;
- `GET /api/users/me` per autenticazione e identità;
- `GET /api/albums` e `GET /api/albums/{id}` per album e relativi asset;
- `POST /api/search/metadata` per ricerca temporale read-only;
- `GET /api/people` per la capability persone.

L'header di autenticazione è `x-api-key`; le risposte sono richieste come JSON. Le versioni formalmente supportate e verificate sui contratti ufficiali sono:

- la famiglia legacy testata: Immich `1.106.0`–`1.140.0`;
- la release reale verificata: Immich `3.3.3` (tag ufficiale `v3.3.3` di `immich-app/immich`).

Non viene presunta alcuna compatibilità automatica con versioni intermedie o successive: una versione differente produce esplicitamente `unsupported_server_version`.

Per Immich 3.3.3:
- `/api/server/version` è l'endpoint ufficiale canonico (il fallback legacy su `/api/server-info/version` viene tentato esclusivamente su `404`);
- `/api/users/me` restituisce l'utente autenticato;
- `/api/albums` e `/api/albums/{id}?withoutAssets=false` mantengono la lista album e gli asset dell'album;
- `/api/search/metadata` adotta la nuova struttura Zod v3 (`size`, `filter: { takenAt: { gte, lt } }`, `orderBy: { field: "localDateTime", direction: "desc" }`), disaccoppiata dai campi flat deprecati in v3.2;
- `/api/people` continua a rispondere con il wrapper `{ "people": [...], "total": N }`.

Endpoint di capability assente significa capability non disponibile, mentre auth, timeout e rete mantengono categorie distinte e non vengono degradati a “unsupported”.

## Selezione e limiti

Una selezione contiene un `album_id` oppure almeno un bound temporale, mai entrambi. L'intervallo temporale usa istanti timezone-aware e semantica UTC `[after, before)`. Il fingerprint è SHA-256 su rappresentazione canonica di tipo selezione, ID album, bound UTC e limite.

Anteprima e import applicano rigidamente `1..25` nel dominio, negli endpoint Immich e nel gateway. La preview interroga soltanto Immich e non crea item o job. Un job completato descrive esclusivamente il campione selezionato e non implica copertura completa della libreria.

## Dominio e persistenza

La normalizzazione è una funzione pura di dominio. Conserva:

- ID asset stabile;
- tipo `IMAGE`, `VIDEO` o `OTHER`;
- data selezionata e provenienza esplicita (`localDateTime`, `dateTimeOriginal`, `fileCreatedAt`, `updatedAt`);
- album, descrizione e persone già presenti nel payload, se disponibili;
- provenance sanitizzata e flag `metadata_only`.

Non vengono conservati path, nomi file, URL, thumbnail o byte. Le tabelle generiche `source_items`, `source_revisions` e `import_jobs` sono sufficienti: non è richiesta una migrazione Immich. L'identità è `(connector_account_id, source_kind, external_id)`; l'hash canonico evita revisioni semanticamente identiche.

La persistenza dell'asset e l'avanzamento del checkpoint avvengono nella stessa transazione. Il checkpoint contiene fingerprint, indice successivo e ultimo ID esterno; un retry con lo stesso fingerprint riprende dall'indice salvato. Errori gateway transitori vengono rilanciati a Procrastinate senza terminalizzare prematuramente il job; errori non retryable diventano terminali. Errori DB o bug non sono occultati come errori per elemento.

## Sicurezza

- HTTP è ammesso soltanto per host che risolvono interamente a indirizzi privati/loopback; HTTPS è richiesto altrove.
- Userinfo, query e fragment nell'URL base sono vietati; redirect automatici disabilitati; timeout espliciti.
- API key e URL completi non sono loggati.
- Il payload del task contiene soltanto `job_id`, `account_id` e modalità.
- Nessuna chiamata di upload, update, delete, archivio o modifica album/persona.

## UI e consultazione

Il wizard mostra versione e capability, supporta selezione album o periodo, visualizza al massimo 25 asset e riepiloga il job. La pagina fonti consulta `GET /api/sources/immich/items`, mostrando totale autorevole separato dal limite di visualizzazione, data/provenienza, tipo, descrizione, album e persone. Non costruisce URL per originali.

## Collaudo locale/server

Il collaudo remoto non è stato eseguito durante l'implementazione e non richiede credenziali nel repository.

1. Avviare GBrain e un server Immich compatibile in rete locale o HTTPS.
2. Solo nella UI di esecuzione inserire URL e API key read-only; non salvarli in file, shell history o fixture.
3. Verificare versione/capability e l'assenza di URL/chiave nei log e nel payload Procrastinate.
4. Provare separatamente un album e un periodo UTC; confermare XOR e rifiuto di limite 26.
5. Eseguire preview e verificare che item/job non aumentino.
6. Avviare import, interrompere il worker dopo almeno un asset, riavviarlo e verificare ripresa dal checkpoint senza duplicati.
7. Verificare totale e metadati nella consultazione; confermare che originali e thumbnail non siano serviti da GBrain.
8. Revocare la chiave e verificare errore auth distinto da timeout/rete/versione unsupported.

## Conseguenze

La scelta privilegia un contratto ristretto e verificabile rispetto a compatibilità implicita con versioni non testate. Per estendere l'intervallo supportato occorre aggiungere un contratto versionato esplicito e fixture dei payload ufficiali corrispondenti.

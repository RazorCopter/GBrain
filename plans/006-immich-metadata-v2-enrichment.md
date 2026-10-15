# Piano 006 — Immich metadata v2 e «Forza analisi completa»

> **Stato:** decisione architetturale pronta per implementazione, con un gate bloccante sul contratto OpenAPI Immich 3.3.3 descritto nella sezione 3.5.
>
> **Perimetro:** progettazione soltanto. Nessun codice applicativo, migrazione, commit, push o deploy è stato eseguito.
>
> **Vincoli invariati:** integrazione Immich read-only e metadata-only; originali e miniature restano in Immich; nessun ML/LLM vision; credenziali cifrate AES-256-GCM; nessun segreto in job, checkpoint o log; batch remoto massimo 25; concorrenza 1; compatibilità Gmail e multi-connettore preservata.

## 1. Decisione sintetica

Introdurre una pipeline semantica `immich-meta-v2` e un hash canonico `immich-canonical-v2`, mantenendo la persistenza nelle colonne JSONB e nelle tabelle già esistenti. La modalità applicativa `enrichment` sarà esposta in UI come **«Forza analisi completa»** e avrà queste proprietà:

1. una nuova scansione parte sempre dall'inizio della selezione, con cursore nullo, anche se un import v1 era rimasto parziale;
2. riesamina gli asset già presenti e continua oltre i circa 50.000 esistenti;
3. il batch remoto resta di 25 elementi e la concorrenza resta 1;
4. ogni batch di asset, i conteggi e il cursore successivo sono salvati nella stessa transazione;
5. una revisione nasce solo quando il contenuto semantico normalizzato cambia; il semplice cambio di pipeline non basta;
6. l'assenza di limite asset è rappresentata esplicitamente da `null`, non da un numero sentinella; un budget richieste numerico produce una tranche riprendibile, mentre `null` significa scansione senza tetto arbitrario;
7. cancellazione e budget sono controllati tra batch; il replay dell'ultimo batch è idempotente;
8. la rilevazione degli asset mancanti è autorizzata solo dopo un'enumerazione completa e dimostrabilmente esaustiva dello scope. In tutti gli altri casi è dichiarata `not_evaluated` e non modifica lo stato degli item.

Non è richiesta una migrazione DB per il nucleo della soluzione: [`ImportJob.checkpoint`](../backend/src/gbrain/domain/models.py:165), [`ImportJob.policy_snapshot`](../backend/src/gbrain/domain/models.py:166), [`SourceItem.canonical_hash`](../backend/src/gbrain/domain/models.py:202) e i JSONB di [`SourceRevision`](../backend/src/gbrain/domain/models.py:226) sono sufficienti. Anche lo stato `removed` esiste già in [`SourceItemState`](../backend/src/gbrain/domain/models.py:85). Una tabella di staging per album non è giustificata nella prima iterazione; si rivaluterà soltanto con misure reali di memoria o librerie con un numero eccezionale di album.

## 2. Evidenze del working tree

### 2.1 Implementazione attuale verificata

- Il dominio v1 conserva soltanto ID, data scelta, tipo, album, descrizione, persone, hash e provenance in [`ImmichNormalizedAsset`](../backend/src/gbrain/domain/immich.py:120).
- La selezione impone oggi un tetto rigido di 50.000 asset tramite [`IMMICH_MAX_ITEMS_LIMIT`](../backend/src/gbrain/domain/immich.py:13) e [`ImmichSelection.__post_init__()`](../backend/src/gbrain/domain/immich.py:84).
- La search v3 invia `withExif=true`, `withPeople=true`, dimensione massima 25 e ordinamento per `localDateTime` in [`ImmichGateway.search_period_assets()`](../backend/src/gbrain/infrastructure/immich_gateway.py:472).
- Lo stesso metodo richiama [`ImmichGateway.get_server_version()`](../backend/src/gbrain/infrastructure/immich_gateway.py:482) a ogni pagina prima della search: il conteggio job attuale registra una sola richiesta, ma la rete ne esegue due. Prima dell'enrichment il version gating deve essere eseguito una volta e memorizzato nel gateway per la durata del job, oppure tutte le richieste devono passare da un budget centrale.
- Gli album vengono ottenuti inline con una singola chiamata di dettaglio in [`ImmichGateway.fetch_album_assets()`](../backend/src/gbrain/infrastructure/immich_gateway.py:442), mentre la search generale non associa album agli asset.
- Il repository crea già una nuova revisione soltanto quando l'hash cambia in [`AsyncPostgresImmichRepository.persist_asset_batch()`](../backend/src/gbrain/infrastructure/immich_repository.py:27), ma il contatore `duplicate_count` comprende sia aggiornati sia invariati e quindi non soddisfa il nuovo contratto.
- La transazione del repository aggiorna item, revisione e checkpoint insieme in [`AsyncPostgresImmichRepository.persist_asset_batch()`](../backend/src/gbrain/infrastructure/immich_repository.py:43).
- Il job riparte oggi dal cursore del proprio checkpoint, si ferma a `item_limit` o `request_budget` e controlla la cancellazione fra batch in [`execute_immich_job()`](../backend/src/gbrain/application/immich_jobs.py:173).
- L'API forza attualmente ogni job Immich a modalità `full` e inizializza cursore e contatori in [`trigger_source_sync()`](../backend/src/gbrain/presentation/sources_api.py:1092).
- Lo schema condiviso limita `item_limit` a 50.000 e `request_budget` a 2.000, e non ammette `enrichment`, in [`SourceSyncRequest`](../backend/src/gbrain/presentation/schemas.py:551).
- La UI offre soltanto import `all` o `range`, invia modalità `full` e calcola l'avanzamento contro un limite numerico in [`ImmichConfigPanel.startImport()`](../frontend/src/ImmichConfigPanel.tsx:61).
- Le credenziali Immich vengono cifrate a riposo con AES-256-GCM nel ramo dedicato di [`save_source_credentials()`](../backend/src/gbrain/presentation/sources_api.py:571); la derivazione a 256 bit e l'AEAD sono definiti in [`derive_credentials_key()`](../backend/src/gbrain/application/crypto.py:30) e [`encrypt_payload()`](../backend/src/gbrain/application/crypto.py:42).

### 2.2 Contratti e fixture locali effettivamente esaminati

Sono stati esaminati:

- il test del contratto v3.3.3 [`test_immich_gateway_supports_verified_v3_3_3()`](../backend/tests/test_immich_gateway.py:203);
- la fixture inline di normalizzazione [`test_immich_asset_normalization_and_provenance()`](../backend/tests/test_immich_gateway.py:401);
- i test di paginazione, replay, resume, budget e cancellazione in [`test_immich_cursor_pagination_job_and_idempotent_replay()`](../backend/tests/test_immich_vertical.py:254) e [`test_immich_resume_budget_and_cooperative_cancel_contracts()`](../backend/tests/test_immich_vertical.py:322);
- le decisioni già documentate in [`docs/adr/004-valutazione-immich-integration.md`](../docs/adr/004-valutazione-immich-integration.md) e [`plans/005-immich-pagination-config-panel.md`](005-immich-pagination-config-panel.md).

Nel repository non è presente una copia dell'OpenAPI Immich 3.3.3, un pacchetto SDK Immich bloccato alla stessa versione o una fixture JSON esaustiva di `AssetResponseDto`. Di conseguenza l'evidenza locale conferma soltanto il sottoinsieme seguente, non l'intero DTO ufficiale:

| Provenienza | Campi confermati nel working tree | Note |
|---|---|---|
| Risposta search | `assets.count`, `assets.items`, `assets.nextCursor` | Wrapper e cursore sono esercitati dai test e dal parser. |
| Asset search | `id`, `type`, `fileCreatedAt`, `localDateTime`, `originalFileName` | Presenti nella fixture v3.3.3 del test gateway. |
| EXIF inline con `withExif` | `exifInfo.dateTimeOriginal`, `exifInfo.description` | Presenti nella fixture v3.3.3. La fixture di normalizzazione contiene anche `exifInfo.model`, ma da sola non prova il contratto ufficiale completo. |
| Persone inline con `withPeople` | `people[].id`, `people[].name` | Presenti nella fixture v3.3.3. |
| Album | `id`, `albumName`, `assets[]` | Confermati da gateway, ADR e test; gli album non risultano nel payload search locale. |
| Date tollerate dal normalizzatore | `dateTimeOriginal` top-level, `updatedAt` | Sono fallback applicativi, non prova che la search 3.3.3 li restituisca sempre. |

Questa distinzione è intenzionale: i campi non presenti nelle fonti locali non vengono dichiarati «sicuramente restituiti» soltanto perché sono plausibili o presenti in altre release Immich.

## 3. Contratto Immich 3.3.3 da congelare

### 3.1 Campi sicuramente restituiti dalla search secondo le evidenze locali

Con la richiesta costruita da [`ImmichGateway.search_period_assets()`](../backend/src/gbrain/infrastructure/immich_gateway.py:485), il piano può considerare garantiti localmente:

- identità e tipo: `id`, `type`;
- date base: `fileCreatedAt`, `localDateTime`;
- nome: `originalFileName`;
- con `withExif=true`: almeno `exifInfo.dateTimeOriginal` e `exifInfo.description` quando esistono;
- con `withPeople=true`: `people[]` con almeno `id` e `name` quando esistono;
- paginazione: `assets.items` e `assets.nextCursor` nullo/assente a fine scansione.

Nullabilità già osservabile o da trattare difensivamente: `exifInfo` può essere nullo/assente, `people` può essere assente o vuoto, descrizione e date EXIF possono essere nulle, e `nextCursor` può essere nullo o assente. Un elemento senza `id` o senza alcuna data utilizzabile è `invalid`; non deve bloccare l'intera pagina.

### 3.2 Inventario candidato da estrarre dall'OpenAPI, non ancora approvato

Prima di implementare il modello v2, Code deve estrarre dal tag ufficiale Immich `v3.3.3` la definizione effettivamente referenziata da `POST /api/search/metadata` e trasformare l'elenco seguente in una fixture contrattuale. L'elenco è un requisito di verifica, non un'affermazione che ogni campo esista nel DTO:

- base asset: ID, tipo, checksum remoto, filename, path originale, MIME, dimensioni, durata, timestamp di creazione/modifica/aggiornamento/locale;
- stato utente: rating, favorite, archive, trash e visibility;
- EXIF: data originale, data modifica, timezone, GPS, make, model, lens model, exposure time, ISO, focal length, aperture/f-number, orientation, dimensioni EXIF, dimensione file e descrizione;
- relazioni: persone, stack, duplicate ID, live-photo video ID;
- provenienza remota: owner, library, device e device-asset ID;
- flag tecnici: offline, resized, external, read-only, has-metadata e qualsiasi thumbhash/preview reference.

Per ogni proprietà la fixture congelata deve registrare: nome JSON esatto, tipo, required/opzionale, nullabilità, enum, contenitore che la restituisce e effetto di `withExif`/`withPeople`. Le differenze di naming fra DTO, per esempio `model` rispetto a un eventuale `cameraModel`, o `fNumber` rispetto ad `aperture`, non vanno risolte per intuizione: il normalizzatore accetta esclusivamente i nomi dimostrati dalla fixture 3.3.3.

### 3.3 Classificazione e costo richieste

| Classe | Decisione | Costo remoto dopo il caching della versione |
|---|---|---|
| Campi presenti nell'asset search | Acquisirli direttamente con `withExif=true` e `withPeople=true`. | Una search ogni massimo 25 asset: `ceil(N/25)`. |
| Persone | Usare soltanto le persone inline; non chiamare `GET /api/people` per ogni scan. | Nessun costo aggiuntivo per asset. L'endpoint persone resta solo capability/configurazione. |
| Appartenenza a tutti gli album | La search locale non la dimostra. Enumerare gli album una volta e leggere il dettaglio inline di ciascun album, costruendo una mappa asset→album prima della scansione. | Una richiesta lista album più una richiesta per ogni album, quindi `1 + A`; zero richieste per asset. Per una selezione di un solo album: una richiesta dettaglio. |
| Campo assente dalla search ma presente nel dettaglio asset | Non abilitarlo automaticamente. Un dettaglio per asset moltiplicherebbe il costo per `N`; deve essere una policy esplicita e giustificata da un campo indispensabile. | Fino a `N` richieste aggiuntive, quindi proibitivo sui 50.000+ asset. |
| Binari, originali, thumbnail, face crop | Non richiederli. | Zero. |

Il costo baseline di una scansione completa diventa `1 + ceil(N/25)` richieste, dove `1` è il version gating eseguito una volta per job. Con enrichment album completo diventa `2 + A + ceil(N/25)`. Capability probe, retry e risposte 429/5xx devono essere conteggiati separatamente come tentativi HTTP reali; il checkpoint deve esporre sia richieste logiche riuscite sia tentativi totali.

### 3.4 Dati da non persistere

Anche se presenti nel DTO, non persistere:

- path originale assoluto: rivela struttura filesystem, mount e nomi directory; conservare soltanto `original_filename` se confermato e utile alla consultazione;
- device ID e device asset ID: identificatori operativi non necessari alla semantica GBrain;
- email, profilo o altri attributi dell'owner; un eventuale owner ID pseudonimo è ammesso soltanto nella provenance per verificare lo scope account;
- thumbnail path, thumbhash, URL firmati, token, API key, header e parametri di autenticazione;
- coordinate/box dei volti, embedding biometrici, crop, thumbnail delle persone; per le persone conservare al massimo ID e nome già previsti dal prodotto;
- flag di trasformazione tecnica come resized/offline/has-metadata se non necessari alla diagnosi;
- payload remoto grezzo completo: aumenta esposizione e rende l'hash dipendente da campi non controllati;
- valori di retry, cursori o timestamp di acquisizione nel payload semantico e nell'hash.

### 3.5 Gate bloccante sul contratto ufficiale

Prima del codice applicativo è obbligatorio aggiungere una fixture di test derivata dall'OpenAPI del tag ufficiale Immich `v3.3.3`, con URL sorgente, tag/commit e SHA-256 del file annotati nella fixture o nel test. Fonte da recuperare: repository ufficiale [immich-app/immich, tag v3.3.3](https://github.com/immich-app/immich/tree/v3.3.3), area OpenAPI del tag.

Il gate è superato quando sono provati:

1. DTO esatto della search e DTO esatto del dettaglio asset;
2. effetto reale di `withExif` e `withPeople`;
3. presenza, naming e nullabilità di dimensioni, checksum, timestamp, GPS, camera, lente, esposizione, ISO, focale, apertura, durata, rating, favorite/archive/visibility, stack, duplicate, live-photo e owner/device;
4. filtri e default di visibility/archive/trash, indispensabili per poter chiamare una scansione «completa»;
5. capacità o meno di ordinare e filtrare per `updatedAt` per il futuro incremental;
6. forma e costo degli endpoint album.

**Rischio bloccante attuale:** senza questo artefatto non è verificabile che una scansione search includa archiviati/nascosti/locked o che i campi candidati siano realmente inline. L'implementazione può procedere sul sottoinsieme già testato, ma non può pubblicizzare copertura completa né attivare missing/tombstone.

## 4. Modello normalizzato v2

### 4.1 Contratto logico

Estendere [`ImmichNormalizedAsset`](../backend/src/gbrain/domain/immich.py:120) con una struttura tipizzata equivalente a questa, usando dataclass annidate soltanto dove riducono ambiguità:

| Gruppo persistito in `normalized_metadata` | Campi v2 | Regole |
|---|---|---|
| Versione | `schema_version=2`, `hash_version=immich-canonical-v2` | La pipeline è registrata anche in `SourceRevision.pipeline_version`, ma non entra da sola nell'hash. |
| Identità | `external_id`, `asset_type`, `remote_checksum`, `original_filename`, `mime_type` | Path escluso. Checksum e MIME solo se confermati dalla fixture. |
| Dimensioni/media | `width_px`, `height_px`, `duration_ms`, `rating` | Interi non negativi; durata derivata soltanto da formato ufficialmente documentato. |
| Timestamp | `file_created_at`, `file_modified_at`, `remote_updated_at`, `local_datetime`, `exif_datetime_original`, `exif_modify_date`, `exif_timezone`, `occurred_at`, `occurred_at_source` | Vedere sezione 4.2. |
| Camera | `make`, `model`, `lens_model`, `exposure_time`, `iso`, `focal_length_mm`, `aperture_f_number`, `orientation` | Stringhe trim; numeri normalizzati come stringhe decimali canoniche. |
| Posizione | `latitude`, `longitude`, `location_precision`, `city`, `state`, `country` | Soggetti alla policy GPS. |
| Stato | `favorite`, `archived`, `visibility` | Includere soltanto proprietà confermate. Trash è utile allo scope/reconciliation, ma non va confuso con missing. |
| Relazioni | `albums[]`, `people[]`, `stack_id`, `stack_primary_asset_id`, `duplicate_id`, `live_photo_video_id` | Solo campi confermati; array ordinati e deduplicati. |
| Provenance minima | `provider`, `external_id`, `owner_id`, `metadata_only`, `contract_version` | Nessun segreto, path o payload grezzo. `owner_id` escluso dalla UI e persistito solo se serve a validare lo scope. |

`normalized_content` resta una proiezione consultabile, non una seconda fonte autorevole: ID, tipo, filename, descrizione, nomi album/persone e riepilogo camera. I metadati completi restano in `normalized_metadata`; l'evidence contiene soltanto provenance e versione del contratto.

### 4.2 Date e timezone

L'attuale [`_parse_immich_datetime()`](../backend/src/gbrain/domain/immich.py:132) assegna UTC alle date senza offset. Questo è errato per `localDateTime`, che rappresenta un orario locale della foto.

Regole v2:

1. timestamp con offset o `Z`: convertire a UTC e serializzare RFC 3339 con precisione a microsecondi e suffisso `Z`;
2. timestamp locale senza offset: conservarlo come local datetime canonico senza inventare UTC;
3. se data EXIF locale e timezone EXIF sono entrambe valide, derivare un istante UTC; se la timezone è assente o invalida, non derivarlo;
4. `occurred_at` usa in ordine: EXIF originale convertibile a istante, data file creazione, data file modifica; `localDateTime` senza zona non deve essere reinterpretato come UTC;
5. `remote_updated_at` è watermark/provenance, non data autobiografica;
6. valore non valido: `null` nel campo opzionale e diagnostica `invalid_field`; l'asset è interamente `invalid` solo senza ID o senza alcuna data sufficiente al contratto `SourceItem.occurred_at`.

Questa correzione può modificare `occurred_at` di record v1 che avevano trattato un orario locale come UTC; è un cambiamento semantico reale e deve creare una revisione v2.

### 4.3 Numeri, null e collezioni

- Mai hashare float binari. Latitudine, longitudine, focale e apertura passano da parsing decimale e vengono serializzate in stringa decimale senza esponente e senza zeri terminali superflui.
- ISO, rating, dimensioni e durata sono interi nel modello normalizzato quando il contratto lo consente.
- Exposure time può restare stringa canonica se il DTO ufficiale ammette frazioni; non convertirla con perdita.
- Campo remoto assente, nullo o stringa vuota converge a `null` per gli scalari opzionali.
- Gli array esistono sempre: vuoti anziché nulli.
- Album ordinati per `(id, name)` e persone per `(id, name)`; deduplica per ID con regola deterministica: preferire il nome non vuoto, poi il minimo lessicografico normalizzato.
- Normalizzazione testo Unicode NFC, trim ai bordi, nessuna modifica case ai nomi visualizzati.

### 4.4 Privacy GPS

Aggiungere alla policy Immich `gps_policy` con tre valori:

- `omit` — default raccomandato: coordinate non persistite e non hashate; località testuali escluse per coerenza privacy;
- `coarse` — coordinate arrotondate a tre decimali, circa 110 metri alla latitudine dell'equatore, e `location_precision=coarse`;
- `exact` — coordinate fino alla precisione confermata dal DTO, con massimo sei decimali nel payload canonico.

La policy fa parte dello snapshot e dell'identità della scansione. Cambiarla richiede una nuova enrichment dall'inizio. Il raw GPS non deve comparire in log, error detail, checkpoint o evidence; soltanto il valore trasformato può entrare in `normalized_metadata` e nell'hash.

## 5. Hash canonico v2 e migrazione v1→v2

### 5.1 Pipeline canonica

Sostituire l'attuale funzione v1 [`compute_immich_canonical_hash()`](../backend/src/gbrain/domain/immich.py:224) con funzioni versionate, mantenendo la funzione v1 disponibile per la migrazione:

1. payload remoto → parser DTO 3.3.3 tollerante ai campi opzionali;
2. DTO → modello normalizzato v2 applicando timezone, decimal, Unicode, GPS e ordinamento;
3. modello → oggetto canonico con chiavi v2 note, scalari null espliciti e array sempre presenti;
4. serializzazione UTF-8 JSON con chiavi ordinate, separatori compatti e numeri già rappresentati in forma canonica;
5. SHA-256 → hash esadecimale lowercase di 64 caratteri.

Escludere dall'oggetto hash:

- `pipeline_version`, `captured_at`, `last_observed_at`, cursore, request count e scan ID;
- `remote_updated_at`, se è soltanto un watermark e gli altri metadati non cambiano;
- campi tecnici volatili o non persistiti;
- ordine remoto di album e persone;
- nomi/path di thumbnail e riferimenti temporanei.

Includere i valori semantici effettivamente persistiti: checksum remoto, filename, tipo, date sostanziali, dimensioni, durata, descrizione, camera, GPS già trasformato, stato utente stabile e relazioni confermate.

### 5.2 Prima osservazione v2 di un item v1

Il cambio `immich-meta-v1`→`immich-meta-v2` non deve produrre da solo una revisione. Per un latest revision v1:

1. calcolare dal nuovo asset anche la proiezione canonica v1;
2. verificare che coincida con [`SourceItem.canonical_hash`](../backend/src/gbrain/domain/models.py:202);
3. confrontare la proiezione semantica v2 con la proiezione del payload v1 portato allo schema v2 con tutti i nuovi campi null/vuoti;
4. se non cambia alcun valore semantico, classificare `unchanged`, lasciare intatta la revisione v1 e aggiornare soltanto `last_observed_at`;
5. se compare un nuovo metadato significativo o cambia un valore esistente, creare esattamente una revisione v2, aggiornare l'hash dell'item e classificare `updated`;
6. dopo la prima revisione v2, confrontare esclusivamente hash v2.

Ne consegue che molti dei 50.000 asset produrranno legittimamente una revisione perché dimensioni, checksum o EXIF vengono realmente aggiunti; non produrranno invece revisioni per semplice riordinamento, timestamp di scansione o cambio del nome pipeline.

Il repository deve restituire un result object con `inserted`, `updated`, `unchanged` e `invalid`; non riutilizzare la tupla ambigua attuale. Per compatibilità dei campi legacy job: `imported_items=inserted`, `duplicate_items=unchanged`, `not_imported_items=invalid`; i conteggi completi e autorevoli v2 sono nel checkpoint. Nessun campo Gmail cambia significato.

## 6. Modalità enrichment e stato operativo

### 6.1 Avvio, restart e resume

Distinguere esplicitamente:

- **nuova «Forza analisi completa»**: crea un nuovo `scan_id`, `cursor=null`, contatori a zero e `scan_started_at`; non eredita mai il checkpoint dell'import parziale precedente;
- **resume della stessa scansione**: conserva `scan_id`, scope, policy, cursore successivo già committato, watermark iniziale e contatori cumulativi; azzera soltanto il budget della nuova tranche;
- **replay tecnico** dopo crash: può rielaborare l'ultimo batch, ma vincolo unico item + confronto hash impediscono duplicati/revision churn.

Non riaprire in-place un job terminale. Il resume crea un nuovo `ImportJob` collegato tramite `resume_of_job_id` e copia solo il checkpoint validato; questo conserva audit e si adatta agli stati esistenti. API e repository devono rifiutare resume se account, pipeline, scope o policy GPS non coincidono.

### 6.2 Limiti e librerie oltre 50.000

Per `mode=enrichment`:

- `item_limit=null` significa nessun limite asset ed è il valore della UI «Forza analisi completa»;
- `request_budget=null` significa nessun tetto richieste; il job continua fino a fine cursore, cancel o errore;
- un `request_budget` numerico è un limite per tranche, non cumulativo di tutta la scansione;
- al budget esaurito con cursore residuo, stato `completed_with_warnings`, fase `budget_paused`, `coverage_complete=false` e azione UI «Continua analisi completa»;
- il resume parte dal cursore committato e dispone di un nuovo budget di tranche, mantenendo il totale cumulativo;
- batch remoto 25 e concorrenza 1 non sono configurabili;
- `item_limit` numerico resta disponibile soltanto alla modalità bounded esistente e non può essere confuso con copertura completa.

Questo rimuove il tetto arbitrario di 50.000 senza obbligare l'operatore a un job illimitato: può scegliere un run senza budget o tranche riprendibili. Il budget deve contare tutte le richieste HTTP reali, comprese versione, album, retry e dettaglio; mantenere anche contatori per categoria.

### 6.3 Checkpoint v2

Contratto JSONB indicativo:

```json
{
  "schema_version": 2,
  "scan_id": "uuid",
  "mode": "enrichment",
  "phase": "scanning",
  "scope": {"selection": "all", "visibility_partitions": []},
  "pipeline_version": "immich-meta-v2",
  "hash_version": "immich-canonical-v2",
  "gps_policy": "omit",
  "scan_started_at": "UTC RFC3339",
  "cursor": "next-uncommitted-page-or-null",
  "album_phase": {"status": "complete", "albums_seen": 0},
  "counts": {
    "seen": 0,
    "inserted": 0,
    "updated": 0,
    "unchanged": 0,
    "invalid": 0,
    "missing": 0
  },
  "requests": {
    "tranche_limit": null,
    "tranche_used": 0,
    "scan_used": 0,
    "attempts_total": 0
  },
  "coverage_complete": false,
  "missing_evaluation": "not_evaluated",
  "last_committed_at": "UTC RFC3339"
}
```

Il cursore salvato è sempre quello della pagina successiva. La transazione contiene tutte le mutazioni degli asset validi, il conteggio degli invalidi, eventuali diagnostiche bounded e il checkpoint. Se la transazione fallisce, nulla avanza. Il cancel viene verificato prima della richiesta successiva e subito dopo una fase album costosa; mai interrompere a metà commit.

Non inserire nel checkpoint API key, URL con credenziali, payload asset, nomi persone, filename, GPS o dettagli d'errore sensibili.

### 6.4 Invalid e diagnostica

Il gateway deve restituire la pagina raw e il cursore; la normalizzazione avviene elemento per elemento nel job. Un elemento malformato non invalida gli altri 24. Persistenza atomica del batch comprende:

- asset validi;
- incremento `invalid`;
- al massimo un campione bounded di reason code e ID remoto se l'ID è valido;
- cursore successivo.

Messaggi e log contengono job ID, categoria, fase e conteggio, mai payload remoto o credenziali.

### 6.5 Missing e tombstone

Una rilevazione missing sicura usa `scan_started_at` e [`SourceItem.last_observed_at`](../backend/src/gbrain/infrastructure/immich_repository.py:75), con esclusione reciproca di job Immich per account:

1. durante la scansione ogni asset osservato aggiorna `last_observed_at` anche se unchanged;
2. soltanto dopo fine naturale di tutte le partizioni di scope, senza item limit, budget paused, cancel, invalidità di pagina o gap di visibilità, avviare reconciliation;
3. gli item attivi dello stesso account/scope con `last_observed_at < scan_started_at` sono candidati missing;
4. se la semantica v3.3.3 di visibility/trash non è provata, impostare `missing_evaluation=not_evaluated`, `missing=0` e non cambiare stato;
5. quando il gate è superato, impostare lo stato esistente `removed` e contare `missing`; una ricomparsa successiva riattiva l'item e produce revisione solo se l'hash cambia.

Questa è la base tombstone prevista dal piano. Non dichiarare in UI o documentazione che i tombstone sono attivi finché test di copertura, reconciliation e riattivazione non passano.

## 7. Album senza doppio churn

Per una scansione completa, preparare prima una mappa immutabile asset→album:

1. lista album;
2. dettaglio inline di ogni album, in ordine stabile per ID;
3. mappa deduplicata con solo ID e nome;
4. search asset, normalizzazione e hash già completi delle appartenenze album.

Così ciascun asset produce al massimo una revisione per run, invece di una revisione base seguita da una revisione album. La fase album è read-only e non modifica item. Se viene cancellata o fallisce, la mappa viene scartata e al resume la fase riparte; questa ripetizione è preferibile a una tabella di staging prematura. Il checkpoint registra costi e stato, non l'intera mappa.

Se misure reali mostrano memoria non accettabile, la seconda iterazione potrà introdurre staging temporaneo per `scan_id`; non fa parte della decisione attuale.

## 8. Base per incremental futuro

Non implementare né pubblicizzare incremental in questa modifica. Rendere però disponibili:

- `remote_updated_at` normalizzato come watermark, ma escluso dall'hash se nessun contenuto cambia;
- massimo `updatedAt` osservato e ID di tie-break nel checkpoint finale;
- high-water time fissato all'avvio del run;
- deduplica per external ID e hash;
- versione del contratto di ordering/filter nello snapshot.

Un futuro incremental sarà autorizzato soltanto se l'OpenAPI 3.3.3 prova filtro e ordinamento stabile per `updatedAt`. Dovrà interrogare da `watermark - overlap_window` fino all'high-water iniziale, con overlap configurato e idempotenza per assorbire clock skew e pareggi. Se l'API non garantisce ordinamento composto `updatedAt + id`, si usa il cursore opaco della query e l'overlap, non un cursore inventato.

L'incremental non può dimostrare cancellazioni o esclusioni dallo scope: la tombstone richiede comunque reconciliation completa periodica.

## 9. Contratto API e UI

### 9.1 API

Estendere in modo backward-compatible [`SourceSyncRequest`](../backend/src/gbrain/presentation/schemas.py:551):

- ammettere `mode=enrichment` soltanto nel ramo Immich;
- rendere `item_limit` e `request_budget` nullable per Immich enrichment, senza cambiare default o validazione Gmail;
- aggiungere `gps_policy=omit|coarse|exact`, default `omit`;
- ignorare/rifiutare `batch_size != 25` e `concurrency != 1` nel ramo Immich;
- esporre un resume endpoint dedicato al job Immich o un campo `resume_job_id`, preferendo endpoint dedicato perché evita di confondere start-from-zero e continuation;
- restituire nel job/checkpoint i contatori distinti e `coverage_complete`, `missing_evaluation`, costo richieste e fase;
- prevenire due scansioni attive dello stesso account con lock/controllo già coerente con il contratto multi-connettore.

Non riutilizzare `mode=full` per enrichment: il nome distinto impedisce che un import bounded venga rappresentato come copertura completa. Il codice Gmail deve continuare ad accettare soltanto le sue modalità attuali.

### 9.2 UI

In [`ImmichConfigPanel`](../frontend/src/ImmichConfigPanel.tsx:15):

- aggiungere un'azione separata con label esatta **«Forza analisi completa»**;
- mostrare prima della conferma: «Riesamina tutti gli asset dall'inizio, inclusi quelli già importati. Può richiedere molto tempo e almeno una richiesta ogni 25 asset, oltre alle richieste per album. Non scarica foto o video.»;
- offrire `GPS: non salvare` come default, con scelte coarse/exact accompagnate da avviso privacy;
- offrire «senza limite richieste» oppure budget per tranche; spiegare che il budget non è un limite totale della libreria;
- durante run senza item limit mostrare conteggio assoluto, batch e richieste, non una percentuale falsa;
- mostrare separatamente inserted, updated, unchanged, invalid e missing;
- se `budget_paused`, mostrare «Continua analisi completa»; se cancel, conservare checkpoint ma non chiamare il run completo;
- mostrare «Copertura completa» soltanto con `coverage_complete=true`; mostrare «Asset mancanti non valutati» negli altri casi;
- mantenere il polling e il cancel esistenti, senza cambiare schermate Gmail.

## 10. Ordine di implementazione

1. **Congelare il contratto 3.3.3:** acquisire OpenAPI ufficiale, fixture esaustiva e test di naming/nullabilità/visibility; risolvere il gate della sezione 3.5.
2. **Dominio e gateway:** introdurre DTO/normalizzazione/hash v2, policy GPS, caching versione, pagina raw per invalid isolati e contatore centralizzato delle richieste; mantenere adapter v1 per migrazione.
3. **Repository:** result object con cinque esiti, confronto v1→v2, persistenza JSONB, checkpoint atomico e reconciliation protetta; nessuna migrazione DB iniziale.
4. **Job:** start-from-zero enrichment, album map, budget nullable/a tranche, resume, cancel e replay; lock per account e checkpoint v2.
5. **API/UI:** modalità distinta, resume, contatori e label italiana con avvisi; preservare contratti Gmail.
6. **Collaudo:** fixture contract, unit test dominio/gateway/repository, job multi-batch, test API/UI e prova PostgreSQL reale della transazione.

## 11. Piano test e criteri di accettazione

### 11.1 Contratto e normalizzazione

- Fixture proveniente dal tag ufficiale 3.3.3 con hash e fonte tracciati.
- Test con tutti i campi confermati, campi opzionali assenti, nulli e valori zero/false.
- Date con `Z`, offset, local time senza timezone, timezone EXIF valida/invalida e precisioni diverse.
- Decimal canonico, exposure frazionario, durata, dimensioni, rating e checksum senza perdita.
- Album/persone permutati e duplicati producono lo stesso hash.
- Campo volatile, ordine JSON o timestamp di acquisizione non cambiano hash.
- Modifica di checksum, descrizione, camera, GPS trasformato, stato stabile o relazione cambia hash.
- Policy GPS omit/coarse/exact produce soltanto i dati autorizzati; nessun GPS raw in log/checkpoint/evidence.
- Path, device ID, thumbhash, face data e payload grezzo non risultano persistiti.

### 11.2 Repository e migrazione

- Nuovo asset: `inserted=1`, una revisione v2.
- Asset v2 identico: `unchanged=1`, zero revisioni nuove, `last_observed_at` aggiornato.
- Asset v2 modificato: `updated=1`, esattamente una revisione nuova.
- Asset v1 semanticamente identico e senza enrichment: unchanged, nessuna revisione per il solo cambio pipeline.
- Asset v1 con nuovi metadati: updated, una sola revisione v2.
- Due writer/replay sullo stesso asset non producono revisioni duplicate.
- Asset invalido non blocca gli asset validi della pagina.
- Batch, contatori e cursore fanno commit o rollback insieme su PostgreSQL reale.

### 11.3 Job, budget e copertura

- Una nuova enrichment ignora il checkpoint del vecchio import parziale e chiama la prima pagina con cursore nullo.
- Riesamina asset esistenti e prosegue oltre 50.000 senza item cap.
- Batch remoto mai oltre 25 e nessuna sovrapposizione concorrente.
- Budget numerico ferma la tranche sul confine batch, lascia `budget_paused` e riprende dal cursore committato con un nuovo job collegato.
- Budget nullo arriva a cursore terminale.
- Il contatore include version, album, search, retry e detail; la versione non viene richiesta a ogni pagina.
- Cancel richiesto durante un batch diventa effettivo prima del batch seguente e non lascia checkpoint anticipato.
- Crash dopo commit e prima dell'ack: replay unchanged, nessuna revisione extra.
- Album e persone sono completi al momento del primo hash del run; nessun doppio churn.
- Missing resta non valutato per budget, cancel, limite, errore, invalidità di pagina o scope non provato.
- A copertura provata e fine naturale, reconciliation marca solo gli item non osservati dello stesso account/scope; ricomparsa riattiva correttamente.

### 11.4 API/UI e regressioni

- Payload enrichment accetta null per i due limiti soltanto nel ramo Immich; payload Gmail invariati.
- Due job Immich attivi per lo stesso account vengono rifiutati o serializzati.
- La UI mostra la label **«Forza analisi completa»**, avviso durata/richieste e default GPS omit.
- Nessuna percentuale viene mostrata con totale ignoto.
- I cinque contatori e la copertura sono coerenti con il checkpoint.
- Resume non viene presentato come nuova scansione e nuova scansione non eredita cursori.
- Suite Gmail, multi-connector, auth/CSRF, cancel e worker resta verde.
- Ispezione log/task dimostra assenza di API key, credential payload, GPS raw e metadati sensibili non necessari.

### 11.5 Criteri osservabili di completamento

La modifica può dirsi completata quando:

1. il contratto OpenAPI 3.3.3 è congelato e tutti i campi persistiti sono riconducibili a fixture/test;
2. una libreria sintetica di oltre 50.000 asset termina o prosegue per tranche senza limite asset implicito;
3. i circa 50.000 item v1 vengono riesaminati dall'inizio e classificati senza revisioni dovute al solo cambio versione;
4. un secondo run identico produce solo unchanged e zero nuove revisioni;
5. una singola modifica remota produce un solo updated e una sola nuova revisione;
6. checkpoint e batch sono atomicamente verificati su PostgreSQL;
7. budget, cancel, crash/replay e resume mantengono lo stesso scan scope e non saltano pagine;
8. `coverage_complete` non è mai true e missing non è mai applicato su una scansione parziale;
9. nessun binario Immich viene richiesto o salvato e i vincoli di sicurezza restano rispettati;
10. nessuna regressione è rilevata nei connettori Gmail e nel contratto multi-connettore.

## 12. Rischi e mitigazioni

| Rischio | Severità | Mitigazione/decisione |
|---|---:|---|
| OpenAPI 3.3.3 non cacheata localmente; campi e visibility non verificabili | Bloccante | Gate della sezione 3.5 prima di implementare il set esteso o dichiarare copertura completa. |
| Budget attuale sottostima le richieste perché la versione è letta a ogni pagina | Alta | Version gating una volta per job e budget centralizzato su ogni tentativo HTTP. |
| Search predefinita esclude archiviati/nascosti/locked/trash | Bloccante per missing | Partizionare solo secondo filtri ufficialmente provati; altrimenti missing not_evaluated. |
| Album enrichment costa `1 + A` e usa memoria | Media | Mappa immutabile pre-scan, contata nel budget; staging DB soltanto dopo misure reali. |
| Dettaglio per asset moltiplica richieste di 50.000+ | Alta | Vietato di default; usare i soli campi search e motivare ogni opt-in. |
| Correzione timezone cambia date v1 | Media | È una modifica semantica esplicita, revisionata una volta e coperta da test. |
| GPS e persone aumentano sensibilità | Alta | GPS omit default, nessun raw GPS/face data, payload minimizzato, accessi applicativi invariati. |
| Job illimitato molto lungo | Media | Checkpoint ogni 25, cancel cooperativo, budget opzionale a tranche e resume auditabile. |
| Cursor opaco cambia se la libreria muta durante scan | Media | Idempotenza per ID/hash, high-water futuro, copertura/missing solo con garanzie contrattuali; documentare che il cursore non è uno snapshot se Immich non lo garantisce. |
| Revision storm da migrazione | Media | Confronto semantico v1→v2; pipeline e campi volatili esclusi dall'hash; una sola revisione quando esiste enrichment reale. |

## 13. File applicativi interessati dalla futura implementazione

- [`backend/src/gbrain/domain/immich.py`](../backend/src/gbrain/domain/immich.py) — DTO normalizzato v2, policy, date/decimal, hash v1/v2 e risultato normalizzazione.
- [`backend/src/gbrain/infrastructure/immich_gateway.py`](../backend/src/gbrain/infrastructure/immich_gateway.py) — contratto 3.3.3, versione cacheata, pagina raw, budget HTTP, album map e nessun download.
- [`backend/src/gbrain/application/immich_jobs.py`](../backend/src/gbrain/application/immich_jobs.py) — enrichment, start/resume, tranche, cancel, contatori e reconciliation.
- [`backend/src/gbrain/infrastructure/immich_repository.py`](../backend/src/gbrain/infrastructure/immich_repository.py) — classificazione esiti, migrazione v1→v2, revisione condizionale e checkpoint atomico.
- [`backend/src/gbrain/presentation/schemas.py`](../backend/src/gbrain/presentation/schemas.py) — request/response nullable e nuovi campi Immich senza regressione Gmail.
- [`backend/src/gbrain/presentation/sources_api.py`](../backend/src/gbrain/presentation/sources_api.py) — creazione scansione da zero, resume validato e lock account.
- [`frontend/src/api.ts`](../frontend/src/api.ts) — tipi enrichment/checkpoint/contatori.
- [`frontend/src/ImmichConfigPanel.tsx`](../frontend/src/ImmichConfigPanel.tsx) — azione, avvisi, GPS, progress e continuation.
- [`backend/tests/test_immich_gateway.py`](../backend/tests/test_immich_gateway.py) — fixture ufficiale e contratto DTO/costi.
- [`backend/tests/test_immich_vertical.py`](../backend/tests/test_immich_vertical.py) — repository, job, migrazione, >50k simulati, replay e reconciliation.
- [`backend/tests/test_multi_connector_job_contract.py`](../backend/tests/test_multi_connector_job_contract.py) — regressione del contratto condiviso.

Non è prevista una migration Alembic nella prima implementazione. Se durante Code emergesse la necessità di staging album persistente o query indicizzate sui singoli metadati JSONB, tale cambiamento richiederà una decisione separata basata su misure, non va introdotto preventivamente.

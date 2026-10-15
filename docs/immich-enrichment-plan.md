# Piano di Arricchimento Metadati Immich v2 («Forza analisi completa») — Fase 0

> **Documento Formale di Specifica e Decisione Architetturale**  
> **Stato:** Approvato per l'integrazione e collaudo (Fase 0)  
> **Riferimenti di Progetto:** [`plans/006-immich-metadata-v2-enrichment.md`](plans/006-immich-metadata-v2-enrichment.md), [`docs/adr/004-valutazione-immich-integration.md`](docs/adr/004-valutazione-immich-integration.md), [`backend/tests/fixtures/immich_openapi_v3.3.3.json`](backend/tests/fixtures/immich_openapi_v3.3.3.json)

---

## 1. Obiettivi e Principi Guida

Il presente documento definisce la specifica tecnica e architetturale dell'estensione **Immich Metadata v2** e della modalità operativa **«Forza analisi completa»** (`enrichment`).

I principi cardine invarianti della soluzione sono:
1. **Metadata-Only & Zero-Binary:** Nessun trasferimento né archiviazione locale di file binari originali, miniature, thumbnail raster o face crops.
2. **Read-Only Non-Invasivo:** L'integrazione opera con credenziali aventi privilegi strettamente limitati alla lettura; nessuna scrittura, mutazione, tagging o rimozione viene invocata verso le istanze Immich.
3. **Idempotenza e Zero Revision Churn:** Nessuna nuova revisione viene generata in assenza di variazioni semantiche effettive dei metadati normalizzati. Il semplice upgrade da pipeline v1 a v2 non crea revisioni fittizie.
4. **Resilienza e Scalabilità:** Paginazione bulk guidata da cursore (`POST /api/search/metadata`), batch atomici di 25 elementi, persistenza transazionale coordinata e ripresa nativa delle scansioni per tranche di budget.
5. **Privacy By Default:** Esclusione delle coordinate geografiche grezze (`gps_policy: omit`), dei percorsi interni al filesystem host del server Immich e di qualunque dato biometrico.
6. **Backward Compatibility Rigorosa:** Totale isolamento rispetto al connettore Gmail e alle pipeline preesistenti, preservando i contratti delle API REST e dei modelli di persistenza.

---

## 2. Matrice dei Campi Metadati v2

La tabella seguente specifica in modo esaustivo il mapping tra il payload remoto Immich (`v1.106+` / testato e congelato su spec `v3.3.3`), il modello di dominio [`ImmichNormalizedAsset`](backend/src/gbrain/domain/immich.py:172) e il payload persistito `normalized_metadata`.

| Nome Campo Canonico | Endpoint Immich di Origine | JSON Path nel Payload Immich | Tipo TypeScript / Python | Nullabilità | Versione Minima Immich | Trattamento Privacy & Idempotenza |
|---|---|---|---|---|---|---|
| `external_id` | `POST /api/search/metadata` | `id` | `string` / `str` | Not Null (obbligatorio) | v1.106+ (testata 3.3.3) | UUID identificativo univoco; validato strict; incluso nell'hash semantico. |
| `asset_type` | `POST /api/search/metadata` | `type` | `'IMAGE' \| 'VIDEO' \| 'OTHER'` / `ImmichAssetType` | Not Null | v1.106+ (testata 3.3.3) | Enum normalizzato; incluso nell'hash semantico. |
| `remote_checksum` | `POST /api/search/metadata` | `checksum` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Base64 SHA-1 calcolato da Immich; incluso nell'hash semantico v2. |
| `original_filename` | `POST /api/search/metadata` | `originalFileName` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Nome file originario sanificato; incluso nell'hash semantico v2. |
| `mime_type` | `POST /api/search/metadata` | `originalMimeType` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | MIME type; incluso nell'hash semantico v2. |
| `width_px` | `POST /api/search/metadata` | `width` / `exifInfo.exifImageWidth` | `number \| null` / `int \| None` | Nullable | v1.106+ (testata 3.3.3) | Intero positivo normalizzato; incluso nell'hash semantico v2. |
| `height_px` | `POST /api/search/metadata` | `height` / `exifInfo.exifImageHeight` | `number \| null` / `int \| None` | Nullable | v1.106+ (testata 3.3.3) | Intero positivo normalizzato; incluso nell'hash semantico v2. |
| `duration_ms` | `POST /api/search/metadata` | `duration` | `number \| null` / `int \| None` | Nullable | v1.106+ (testata 3.3.3) | Durata video in ms; intero non negativo; incluso nell'hash semantico v2. |
| `rating` | `POST /api/search/metadata` | `exifInfo.rating` | `number \| null` / `int \| None` | Nullable | v1.106+ (testata 3.3.3) | Rating intero (1..5); incluso nell'hash semantico v2. |
| `file_created_at` | `POST /api/search/metadata` | `fileCreatedAt` | `string \| null` / `datetime \| None` | Nullable | v1.106+ (testata 3.3.3) | Timestamp ISO RFC 3339 UTC con microsecondi; incluso nell'hash v2. |
| `file_modified_at` | `POST /api/search/metadata` | `fileModifiedAt` | `string \| null` / `datetime \| None` | Nullable | v1.106+ (testata 3.3.3) | Timestamp di filesystem; **escluso dall'hash semantico** (volatile). |
| `remote_updated_at` | `POST /api/search/metadata` | `updatedAt` | `string \| null` / `datetime \| None` | Nullable | v1.106+ (testata 3.3.3) | Timestamp aggiornamento Immich; **escluso dall'hash semantico** (watermark). |
| `local_datetime` | `POST /api/search/metadata` | `localDateTime` | `string \| null` / `datetime \| None` | Nullable | v1.106+ (testata 3.3.3) | Orario locale senza fuso; non reinterpretato forzatamente in UTC; incluso nell'hash v2. |
| `exif_datetime_original` | `POST /api/search/metadata` | `exifInfo.dateTimeOriginal` | `string \| null` / `datetime \| None` | Nullable | v1.106+ (testata 3.3.3) | Orario EXIF originario normalizzato; incluso nell'hash v2. |
| `exif_modify_date` | `POST /api/search/metadata` | `exifInfo.modifyDate` | `string \| null` / `datetime \| None` | Nullable | v1.106+ (testata 3.3.3) | Timestamp modifica EXIF; **escluso dall'hash semantico** (volatile). |
| `exif_timezone` | `POST /api/search/metadata` | `exifInfo.timeZone` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Nome o offset fuso orario EXIF; incluso nell'hash v2. |
| `make` | `POST /api/search/metadata` | `exifInfo.make` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Produttore fotocamera; stringa trim; inclusa nell'hash v2. |
| `model` | `POST /api/search/metadata` | `exifInfo.model` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Modello fotocamera; stringa trim; inclusa nell'hash v2. |
| `lens_model` | `POST /api/search/metadata` | `exifInfo.lensModel` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Modello obiettivo; stringa trim; inclusa nell'hash v2. |
| `exposure_time` | `POST /api/search/metadata` | `exifInfo.exposureTime` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Tempo esposizione (es. `"1/250"`); preservato canonico; incluso nell'hash v2. |
| `iso` | `POST /api/search/metadata` | `exifInfo.iso` | `number \| null` / `int \| None` | Nullable | v1.106+ (testata 3.3.3) | Sensibilità ISO intera; inclusa nell'hash v2. |
| `focal_length_mm` | `POST /api/search/metadata` | `exifInfo.focalLength` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Decimale normalizzato (es. `"50.0"`); mai float binario; incluso nell'hash v2. |
| `aperture_f_number` | `POST /api/search/metadata` | `exifInfo.fNumber` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Decimale normalizzato (es. `"1.8"`); mai float binario; incluso nell'hash v2. |
| `orientation` | `POST /api/search/metadata` | `exifInfo.orientation` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Orientamento EXIF; stringa trim; inclusa nell'hash v2. |
| `latitude` | `POST /api/search/metadata` | `exifInfo.latitude` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | **Privacy Policy:** Escluso di default (`omit`). Con `coarse` arrotondato a 3 decimali (~110m). Con `exact` fino a 6 decimali. |
| `longitude` | `POST /api/search/metadata` | `exifInfo.longitude` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | **Privacy Policy:** Trattamento identico a `latitude`. |
| `location_precision` | Calcolato dal Domain | N/A | `'omit' \| 'coarse' \| 'exact'` / `str` | Not Null | v1.106+ (testata 3.3.3) | Indicatore di policy applicata; **escluso dall'hash semantico**. |
| `city` | `POST /api/search/metadata` | `exifInfo.city` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | **Privacy Policy:** Escluso se `gps_policy: omit`; incluso se consentito. |
| `state` | `POST /api/search/metadata` | `exifInfo.state` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | **Privacy Policy:** Escluso se `gps_policy: omit`; incluso se consentito. |
| `country` | `POST /api/search/metadata` | `exifInfo.country` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | **Privacy Policy:** Escluso se `gps_policy: omit`; incluso se consentito. |
| `favorite` | `POST /api/search/metadata` | `isFavorite` | `boolean` / `bool` | Not Null | v1.106+ (testata 3.3.3) | Booleano; incluso nell'hash v2. |
| `archived` | `POST /api/search/metadata` | `isArchived` | `boolean` / `bool` | Not Null | v1.106+ (testata 3.3.3) | Booleano; incluso nell'hash v2. |
| `trashed` | `POST /api/search/metadata` | `isTrashed` | `boolean` / `bool` | Not Null | v1.106+ (testata 3.3.3) | Booleano; monitorato per il ciclo di vita (non confuso con missing). |
| `visibility` | `POST /api/search/metadata` | `visibility` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | Visibilità (`'timeline'` ecc.); inclusa nell'hash v2. |
| `duplicate_id` | `POST /api/search/metadata` | `duplicateId` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | UUID gruppo duplicati; incluso nell'hash v2. |
| `live_photo_video_id` | `POST /api/search/metadata` | `livePhotoVideoId` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | UUID stream video accoppiato Live Photo; incluso nell'hash v2. |
| `stack_id` | `POST /api/search/metadata` | `stack.id` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | UUID dello stack; incluso nell'hash v2. |
| `stack_primary_asset_id` | `POST /api/search/metadata` | `stack.primaryAssetId` | `string \| null` / `str \| None` | Nullable | v1.106+ (testata 3.3.3) | UUID asset primario dello stack; incluso nell'hash v2. |
| `people[]` | `POST /api/search/metadata` | `people[].{id, name}` | `Array<{id, name}>` / `tuple[ImmichPerson, ...]` | Not Null (array vuoto se assente) | v1.106+ (testata 3.3.3) | Solo ID e nome (niente face crops, bounding box o embeddings); array ordinato deterministico per `(id, name)`; incluso nell'hash v2. |
| `albums[]` | Pre-calcolo o `GET /api/albums/:id` | Pre-fetching mappa `asset_id -> albums` | `Array<{id, name}>` / `tuple[ImmichAlbumInfo, ...]` | Not Null (array vuoto se assente) | v1.106+ (testata 3.3.3) | Array ordinato per `(id, name)`; incluso nell'hash v2. |
| *Dati scartati dal DTO* | `POST /api/search/metadata` | `originalPath`, `ownerId`, `thumbhash`, ecc. | N/A | Scartati a priori | Tutte | **Non persistere mai**: path filesystem server, account info, coordinate volti, raw payload. |

---

## 3. Stima del Costo in Richieste per 50.000+ Asset

### 3.1 Confronto Architetturale: Bulk Search a Cursore vs Chiamate Naïf

La progettazione ha valutato rigorosamente due scenari di acquisizione per una libreria di grandi dimensioni ($N = 50.000$ asset):

| Dimensione di Analisi | Architettura Naïf (Chiamate Dettaglio Singole) | Architettura GBrain Adottata (Bulk Search Paginata a Cursore) | Risparmio Richieste / Efficienza |
|---|---|---|---|
| **Meccanismo di Invocazione** | 1 chiamata search di elenco + $N$ chiamate `GET /api/assets/:id` | Paginazione a cursore `POST /api/search/metadata` con `withExif=true` e `withPeople=true` | Eliminazione totale delle chiamate punto-punto per asset. |
| **Dimensione Pagina (Batch)** | 25 asset (limitazione pagination) | 25 asset fissi (`IMMICH_API_BATCH_SIZE = 25`) | Conforme al tetto massimo raccomandato da Immich. |
| **Richieste HTTP di Elenco** | $\lceil 50.000 / 25 \rceil = 2.000$ req | $\lceil 50.000 / 25 \rceil = 2.000$ req | Cursore opaco univoco restituito da `assets.nextCursor`. |
| **Richieste HTTP di Dettaglio Asset** | $50.000$ req individuali | **0 req** (tutti i metadati EXIF e persone sono inline) | **-50.000 chiamate HTTP** |
| **Richieste Version Gating** | 1 (all'avvio del job con caching) | 1 (all'avvio del job con caching gateway) | Caching a livello gateway per tutta la durata del job. |
| **Richieste Mappa Album** | $1 + A$ (dove $A$ è il numero di album, es. 20) | $1 + A$ (pre-acquisizione con join in-memory) | Zero chiamate per asset; pre-calcolo atomico. |
| **Richieste Download Binari/Miniature** | 0 req (metadata-only) | **0 req** (nessun binario) | Zero saturazione storage/I/O locale. |
| **Totale Richieste HTTP ($N=50.000, A=20$)** | **52.021 richieste** | **2.022 richieste** | **Riduzione del 96.1% nel traffico HTTP di rete** |

### 3.2 Calcolo Esatto per 50.000 Asset
$$N_{\text{richieste}} = N_{\text{version\_gating}} + (1 + A_{\text{album}}) + \left\lceil \frac{N_{\text{asset}}}{\text{batch\_size}} \right\rceil$$
Per una libreria senza filtri album ($A=0$) o con album già noti:
$$\frac{50.000}{25} = 2.000 \text{ richieste HTTP bulk}$$
Aggiungendo la verifica di versione iniziale cached ($1$ req), il totale ammonta a **2.001 richieste HTTP** per l'intera libreria.

### 3.3 Analisi del Tempo Stimato, Rate Limiting Cooperativo e Budget per Tranche

* **Latenza di Rete e Overhead Transazionale:**  
  Ipotizzando una rete LAN/Home-Lab o VPN verso il server Immich con latenza media $T_{\text{req}} \approx 60\text{ ms}$ (elaborazione server + trasferimento JSON) e un overhead transazionale locale su PostgreSQL di $T_{\text{db}} \approx 15\text{ ms}$, ogni batch da 25 asset richiede circa $75\text{ ms}$.  
  Senza delay artificiale: $2.000 \times 75\text{ ms} = 150\text{ s} \approx 2.5\text{ minuti}$.
* **Rate Limiting Cooperativo:**  
  Al fine di preservare le risorse CPU/DB del server Immich (spesso installato su hardware NAS casalingo o mini-PC) ed evitare throttling 429, l'integrazione adotta un modello sequenziale strict (**concorrenza = 1**) con intervallo cooperativo opzionale.  
  Con un delay cautelativo di $50\text{ ms}$ tra batch: tempo medio per batch $\approx 125\text{ ms} \implies 2.000 \times 0.125\text{ s} \approx 250\text{ s}$ (**4 minuti e 10 secondi** per 50.000 asset).
* **Budget per Tranche e Sospensione Cooperativa:**  
  Per contesti con vincoli stringenti di disponibilità o connessioni instabili, l'operatore può impostare un `request_budget` numerico (es. 100, 200 o 500 richieste per tranche):
  * **Tranche da 200 richieste:** 200 richieste elaborano esattamente $200 \times 25 = 5.000$ asset. Durata tranche: $\approx 25\text{ secondi}$.  
    All'esaurimento delle 200 richieste, il job committa l'ultimo cursore valido, imposta lo stato `completed_with_warnings`, la fase `incomplete` e cede il controllo.
  * **Scansione Illimitata (`request_budget: null`):** Il job procede continuativamente attraverso tutti i 50.000+ asset fino al completo esaurimento del cursore remoto (`has_more = False`), consentendo il completamento unattended della libreria.

---

## 4. Permessi Minimi API Key e Gestione Sicurezza

### 4.1 Principio del Privilegio Minimo (Read-Only)
L'integrazione GBrain verso Immich richiede esclusivamente privilegi di **sola lettura**:
* **Nessun account amministratore richiesto:** L'integrazione non necessita del flag `isAdmin` né di permessi di configurazione server Immich. Può operare con la chiave API di un utente standard per indicizzare i soli asset a cui tale utente ha accesso.
* **Operazioni Inibite a Livello Architetturale:**
  * Nessun upload di file (`POST /api/assets`).
  * Nessuna modifica o aggiornamento di metadati/tag/album su Immich (`PUT /api/assets`, `PATCH /api/assets`).
  * Nessuna eliminazione o spostamento nel cestino (`DELETE /api/assets`).
  * Nessun download di stream binari originari (`GET /api/assets/:id/original`).
* **Endpoint Immich Utilizzati:**
  1. `GET /api/server/version` (Read-only version check).
  2. `POST /api/search/metadata` (Bulk metadata read query).
  3. `GET /api/albums` e `GET /api/albums/:id` (Read-only album structure).
  4. `GET /api/people` (Read-only capabilities check).

### 4.2 Crittografia a Riposo e Protezione Segreti
1. **Cifratura AES-256-GCM:**  
   La chiave API di Immich viene cifrata a riposo nella tabella [`source_credentials`](backend/src/gbrain/infrastructure/source_credentials_repository.py:1) utilizzando `AES-256-GCM` con authenticated encryption (AEAD) e Initialization Vector (IV) generato crittograficamente per ogni salvataggio via [`save_source_credentials()`](backend/src/gbrain/presentation/sources_api.py:571).
2. **Esclusione da Log e Tracce Applicative:**  
   Le stringhe di connessione, le chiavi API e gli header HTTP `x-api-key` vengono sanificati e mascherati in tutti i logger di sistema e nei messaggi d'errore di [`ImmichGatewayError`](backend/src/gbrain/domain/immich.py:90).
3. **Esclusione dai Payload Procrastinate e Checkpoint:**  
   I task asincroni accodati in Procrastinate ricevono unicamente l'`account_id` e il `job_id` (UUID). Nessun token segreto o credenziale in chiaro transita nella tabella dei task `procrastinate_jobs`. I checkpoint JSONB registrano unicamente cursori di paginazione e metriche numeriche aggregate.

---

## 5. Elenco delle Modifiche Architetturali e d'Integrazione

### 5.1 Schema DB e Modelli di Persistenza
* **Tabelle Esistenti Riutilizzate:**  
  Nessuna alterazione strutturale distruttiva o migrazione SQL bloccante necessaria. Le tabelle del modello dati centrale supportano nativamente l'estensione:
  * [`SourceItem`](backend/src/gbrain/domain/models.py:192): colonne `canonical_hash`, `occurred_at`, `source_state`, `last_observed_at`.
  * [`SourceRevision`](backend/src/gbrain/domain/models.py:226): colonne JSONB `normalized_metadata`, `normalized_content`, `evidence`, oltre a `pipeline_version` (`immich-meta-v2`) e `revision_number`.
  * [`ImportJob`](backend/src/gbrain/domain/models.py:151): colonna JSONB `checkpoint` e `policy_snapshot`.
* **Formato Checkpoint v2:**
  Il checkpoint JSONB traccia puntualmente la scansione:
  ```json
  {
    "schema_version": 2,
    "scan_id": "uuid",
    "mode": "enrichment",
    "phase": "scanning",
    "cursor": "019488a0-...",
    "counts": {
      "seen": 50000,
      "inserted": 1200,
      "updated": 48800,
      "unchanged": 0,
      "invalid": 0,
      "missing": 0
    },
    "requests_completed": 2000,
    "request_budget": null,
    "item_limit": null,
    "coverage_complete": true,
    "missing_evaluation": "not_evaluated"
  }
  ```

### 5.2 Dominio
* **Entità Normalizzata [`ImmichNormalizedAsset`](backend/src/gbrain/domain/immich.py:172):**  
  Arricchita con tutti i campi EXIF e le relazioni descritte nella Matrice Campi, tipizzati con `dataclass(frozen=True, slots=True)`.
* **Versioni di Pipeline e Hash:**  
  Identificate dalle costanti [`IMMICH_PIPELINE_VERSION = "immich-meta-v2"`](backend/src/gbrain/domain/immich.py:19) e [`IMMICH_HASH_VERSION = "immich-canonical-v2"`](backend/src/gbrain/domain/immich.py:18).
* **Gestione della Migrazione v1 $\rightarrow$ v2 senza Revision Churn:**  
  Implementata nella logica di confronto semantico tramite [`compute_immich_v1_canonical_hash()`](backend/src/gbrain/domain/immich.py:407) e [`has_new_v2_semantic_metadata()`](backend/src/gbrain/domain/immich.py:401):  
  Se un asset già salvato con pipeline v1 coincide con la proiezione v1 e non introduce metadati semantici v2 nuovi (o con variazioni), non viene creata alcuna revisione superflua (`unchanged`), aggiornando unicamente `last_observed_at`. Se vengono riscontrati nuovi campi arricchiti (es. camera, ISO, dimensioni, checksum), viene generata esattamente una nuova revisione numerata (`updated`).

### 5.3 Gateway ([`ImmichGateway`](backend/src/gbrain/infrastructure/immich_gateway.py:35))
* **Paginazione Cursore Robusta:**  
  Metodo [`search_period_assets()`](backend/src/gbrain/infrastructure/immich_gateway.py:480) configurato con `withExif: True`, `withPeople: True`, `size: 25`, estrazione del cursore opaco da `response["assets"]["nextCursor"]`.
* **Caching Version Gating:**  
  La chiamata a [`get_server_version()`](backend/src/gbrain/infrastructure/immich_gateway.py:307) viene invocata una sola volta all'inizio della sessione di scansione, evitando il raddoppio delle richieste HTTP per pagina.
* **Isolamento e Tracking Elementi Invalidi:**  
  Qualora un singolo elemento all'interno del batch da 25 risulti privo di ID valido o contenga date corrotte non parsabili, viene marcato come `invalid` ed escluso dalla persistenza, senza arrestare né far fallire gli altri 24 elementi validi del medesimo batch.

### 5.4 Repository ([`AsyncPostgresImmichRepository`](backend/src/gbrain/infrastructure/immich_repository.py:42))
* **Esiti Semantici Distinti:**  
  Restituzione del dataclass [`ImmichBatchOutcome`](backend/src/gbrain/infrastructure/immich_repository.py:29) con 5 contatori atomici:
  1. `inserted`: nuovo asset mai registrato nel database GBrain.
  2. `updated`: asset preesistente con variazione effettiva dei metadati semantici (nuova revisione creata).
  3. `unchanged`: asset preesistente identico (nessuna revisione, solo touch di `last_observed_at`).
  4. `invalid`: elemento remoto scartato per malformazione o corruzione strutturale.
  5. `missing`: asset candidato alla rimozione/tombstone.
* **Transazionalità Atomica per Batch:**  
  La scrittura del blocco di 25 item, delle relative revisioni e del checkpoint aggiornato dell'`ImportJob` avviene all'interno della medesima transazione PostgreSQL (`async with self._session.begin():`). In caso di interruzione anomala di rete, il cursore non avanza e il replay tecnico rimane perfettamente idempotente.

### 5.5 Job Procrastinate ([`execute_immich_job()`](backend/src/gbrain/application/immich_jobs.py:84))
* **Modalità `enrichment`:**  
  Riconoscimento esplicito del parametro `mode="enrichment"`:
  * Avvio sempre dall'inizio della libreria (`cursor = None`), indipendentemente dallo stato di precedenti job parziali v1.
  * Assenza di limite forzato sugli asset (`item_limit = None`), consentendo di superare il vecchio tetto di 50.000 elementi.
  * Gestione del `request_budget`: se numerico, il job si arresta al superamento del limite impostando lo stato `completed_with_warnings` e `phase: "incomplete"`; se `null`, la scansione prosegue fino a `has_more = False`.
* **Ripresa di Tranche Interrotte (`resume_from_job_id`):**  
  Capacità di ereditare il cursore di un job precedente interrotto per budget senza perdere la cronologia degli audit.
* **Controllo Cooperativo di Cancellazione:**  
  Verifica dello stato di cancellazione (`is_cancel_requested`) tra un batch e l'altro mediante [`_cancel_if_requested()`](backend/src/gbrain/application/immich_jobs.py:44), garantendo l'arresto pulito e sicuro dell'elaborazione senza commit a metà pagina.

### 5.6 API REST di Presentazione
* **Schema [`SourceSyncRequest`](backend/src/gbrain/presentation/schemas.py:551):**  
  Estensione retro-compatibile:
  * Ammissione del valore `mode="enrichment"` per la sorgente `immich`.
  * Supporto di valori `null` per `item_limit` e `request_budget` nel ramo Immich.
  * Nessun impatto o modifica per i flussi del connettore `gmail` (che continuano a imporre le validazioni e le modalità esistenti).
* **Endpoint [`trigger_source_sync()`](backend/src/gbrain/presentation/sources_api.py:1072):**  
  Inizializzazione corretta del checkpoint di partenza in base alla modalità richiesta, serializzazione dei task Procrastinate e prevenzione di job concorrenti multipli sullo stesso account.

### 5.7 Frontend UI ([`ImmichConfigPanel`](frontend/src/ImmichConfigPanel.tsx:15))
* **Azione Dedicata «Forza analisi completa»:**  
  Bottone primario dedicato con modale/dialogo di conferma esplicativo:
  > *"Attenzione: «Forza analisi completa» riesaminerà tutti i metadati dell'intera libreria Immich dall'inizio (senza limiti di asset o richieste). L'operazione può richiedere molto tempo e un numero elevato di richieste alle API remote. Può comunque essere annullata in sicurezza tra un batch e l'altro. Confermi l'avvio?"*
* **Visualizzazione dei 4 Contatori Principali:**  
  La scheda di avanzamento e i dettagli di completamento mostrano distintamente:
  * **Prima analisi** (`inserted`): nuovi item inseriti.
  * **Aggiornati** (`updated`): item arricchiti con nuova revisione.
  * **Invariati** (`unchanged`): item confermati identici senza duplicare revisioni.
  * **Errori** (`invalid`): elementi non conformi o malformati.
* **Supporto alla Ripresa:**  
  Pulsante dinamico *"Riprendi tranche interrotta"* attivo quando il job si trova in `completed_with_warnings` con fase `incomplete`.

### 5.8 Test Suite & Congelamento OpenAPI Immich v3.3.3
* **Artefatto Congelato [`immich_openapi_v3.3.3.json`](backend/tests/fixtures/immich_openapi_v3.3.3.json):**  
  Specifica OpenAPI ufficiale scaricata dal repository upstream (SHA-256: `6c665f5d729a6c057385a187e33b8f1a9e86442caec2f5f0fc6ea483ccee3623`), garantendo la conformità dei tipi e dei path JSON.
* **Test di Verifica Contrattuale ([`test_immich_v3_openapi_fixture_contract_compliance()`](backend/tests/test_immich_gateway.py:474)):**  
  Valida la presenza e la nullabilità dei DTO `AssetResponseDto`, `ExifResponseDto`, `MetadataSearchDto`, accertando l'assenza di album inline in `AssetResponseDto` e la conformità di tutti i campi utilizzati.
* **Suite di Test Verticale e di Migrazione ([`backend/tests/test_immich_vertical.py`](backend/tests/test_immich_vertical.py:1)):**  
  Test automatizzati per:
  1. Migrazione v1 $\rightarrow$ v2 senza revision churn su record invariati.
  2. Generazione di nuova revisione per arricchimento effettivo (es. aggiunta EXIF/checksum).
  3. Comportamento della modalità enrichment con cursore ripristinato a `None`.
  4. Gestione del budget richieste e continuazione a tranche.
  5. Isolamento di record invalidi all'interno di batch validi.

---

## 6. Conclusioni e Verifica di Chiusura della Fase 0

Tutti i requisiti ingegneristici della **Fase 0** risultano formalizzati, verificabili e direttamente collegati all'implementazione:
1. Matrice metadati esaustiva con delimitazione chiara di privacy, nullabilità e versioning.
2. Dimostrazione matematica del vantaggio architetturale bulk a cursore (2.000 richieste contro oltre 52.000 per 50.000 asset).
3. Modello di sicurezza read-only rigoroso e crittografia AES-256-GCM a riposo.
4. Definizione completa delle modifiche su DB, Dominio, Gateway, Repository, Job, API REST e Frontend.
5. Contratto API ufficiale Immich 3.3.3 congelato in fixture contrattuale verificata.

# Integrazioni di GBrain

## 1. Scopo del documento

Questo documento descrive le integrazioni presenti in GBrain, con particolare attenzione a:

- stato reale di disponibilità;
- modalità di connessione e autenticazione;
- dati letti dalla sorgente;
- normalizzazione e persistenza;
- sincronizzazione, checkpoint e recupero dagli errori;
- sicurezza e limiti noti.

La distinzione fondamentale è tra:

1. **connettori sorgente**, che acquisiscono informazioni da sistemi esterni come Gmail e Immich;
2. **integrazioni infrastrutturali**, che forniscono persistenza, code di lavoro e servizi operativi;
3. **connettori pianificati**, visibili nel catalogo ma non ancora implementati.

Il catalogo autorevole è definito in [`SOURCE_CATALOG`](../backend/src/gbrain/domain/connectors.py:273).

---

## 2. Stato sintetico

| Integrazione | Stato | Accesso | Dati acquisiti | Persistenza |
|---|---|---|---|---|
| Gmail | Disponibile | OAuth 2.0, scope read-only | Email inviate, intestazioni, corpi MIME normalizzati, allegati come riferimenti, cronologia | PostgreSQL |
| Immich | Disponibile | URL istanza + API key, read-only | Metadati di foto e video, data, descrizione, album e persone | PostgreSQL, senza originali multimediali |
| PostgreSQL | Disponibile | Rete backend + secret | Stato applicativo, credenziali cifrate, job, checkpoint, item e revisioni | Persistenza autorevole |
| Procrastinate | Disponibile | Tabelle PostgreSQL | Code asincrone, retry e scheduling dei job | PostgreSQL |
| Neo4j | Predisposto, non alimentato | Rete backend + secret | Solo verifica di connettività nello stato attuale | Nessuna proiezione applicativa attiva |
| Google Drive | Pianificato | OAuth 2.0 | Non implementato | Nessuna |
| Google Calendar | Pianificato | OAuth 2.0 | Non implementato | Nessuna |
| Synology NAS | Pianificato | Da definire | Non implementato | Nessuna |
| Documenti locali | Pianificato | Directory locale | Non implementato | Nessuna |
| GitHub | Pianificato | Token/OAuth da definire | Non implementato | Nessuna |
| Export ChatGPT | Pianificato | Import file | Non implementato | Nessuna |
| OmniRoute | Pianificato | Da definire | Non implementato | Nessuna |

La disponibilità esposta alla GUI deriva dai descrittori in [`get_source_catalog()`](../backend/src/gbrain/domain/connectors.py:465), non dalla sola presenza di una scheda grafica.

---

## 3. Architettura comune dei connettori

### 3.1 Flusso generale

I connettori disponibili seguono lo stesso flusso logico:

1. l'utente configura le credenziali;
2. GBrain salva i segreti cifrati;
3. viene eseguito un test di connessione read-only;
4. l'utente configura o seleziona il perimetro da importare;
5. l'API crea un job applicativo;
6. Procrastinate accoda il lavoro;
7. il worker recupera e decifra le credenziali a runtime;
8. il gateway interroga esclusivamente endpoint di lettura;
9. i dati vengono normalizzati in strutture interne;
10. repository e transazioni PostgreSQL salvano item, revisioni e checkpoint;
11. la GUI legge stato, contatori e diagnostica dalle API GBrain.

Gli endpoint comuni sono raccolti in [`sources_api.py`](../backend/src/gbrain/presentation/sources_api.py:1) e comprendono catalogo, stato, credenziali, test connessione, anteprima, sincronizzazione, cancellazione, disconnessione e consultazione degli item.

### 3.2 Modello dati condiviso

Le entità principali sono:

- [`ConnectorAccount`](../backend/src/gbrain/domain/models.py:351): rappresenta l'account esterno collegato e il suo stato;
- [`SourceClientCredential`](../backend/src/gbrain/domain/models.py:641): conserva la configurazione sensibile cifrata;
- [`SourcePolicy`](../backend/src/gbrain/domain/models.py:409): conserva la politica di selezione applicata alla sorgente;
- [`ImportJob`](../backend/src/gbrain/domain/models.py:142): conserva stato, snapshot della configurazione, contatori, checkpoint, retry e richiesta di cancellazione;
- [`SourceItem`](../backend/src/gbrain/domain/models.py:188): identifica logicamente un elemento esterno per account, sorgente e identificatore remoto;
- [`SourceRevision`](../backend/src/gbrain/domain/models.py:226): conserva la versione normalizzata di un item e le sue evidenze di provenienza;
- [`GmailCheckpoint`](../backend/src/gbrain/domain/models.py:447): checkpoint specializzato per la cronologia Gmail.

La chiave univoca di [`SourceItem`](../backend/src/gbrain/domain/models.py:188) è composta da account del connettore, tipo di sorgente e ID esterno. Questo rende gli import ripetibili e impedisce la duplicazione logica dello stesso elemento.

### 3.3 Credenziali e cifratura

Le credenziali client vengono gestite da [`SourceCredentialsRepository`](../backend/src/gbrain/infrastructure/source_credentials_repository.py:13).

La cifratura usa:

- AES-256-GCM per confidenzialità e autenticazione;
- nonce casuale a 96 bit per ogni payload;
- HKDF SHA-256 per derivare la chiave dal secret master;
- associated data versionata per impedire riusi fuori contesto.

Il formato è implementato in [`encrypt_payload()`](../backend/src/gbrain/application/crypto.py:42). Le credenziali vengono decifrate solo quando il backend o il worker devono contattare la sorgente. I payload Procrastinate trasportano identificatori di job e account, non API key, access token o refresh token.

---

## 4. Integrazione Gmail

### 4.1 Obiettivo e perimetro

L'integrazione Gmail importa esclusivamente i messaggi nel perimetro **Posta inviata**. Non legge l'intera casella e non modifica messaggi, etichette o impostazioni dell'account.

Il connettore dichiara lo scope Google:

- `https://www.googleapis.com/auth/gmail.readonly`

Lo scope è definito in [`GMAIL_READONLY_SCOPE`](../backend/src/gbrain/domain/gmail.py:19) e verificato dal gateway [`OfficialGmailGateway`](../backend/src/gbrain/infrastructure/gmail_gateway.py:102).

### 4.2 Come viene effettuata la connessione

1. L'utente configura un client OAuth 2.0 Google di tipo Web Application.
2. GBrain avvia Authorization Code Flow con PKCE.
3. Google richiama la callback autenticata di GBrain.
4. GBrain verifica identità, scope e token ricevuti.
5. Access token e refresh token vengono cifrati e associati al [`ConnectorAccount`](../backend/src/gbrain/domain/models.py:351).
6. I token vengono aggiornati a runtime quando necessario.

Il ciclo OAuth è implementato in [`gmail_oauth.py`](../backend/src/gbrain/application/gmail_oauth.py:1). Le istruzioni operative sono disponibili in [`GMAIL_OAUTH_SETUP.md`](GMAIL_OAUTH_SETUP.md).

### 4.3 Endpoint Gmail utilizzati

Il gateway è intenzionalmente read-only e usa le operazioni di profilo, elenco messaggi, lettura messaggio e cronologia. La classe di accesso è [`OfficialGmailGateway`](../backend/src/gbrain/infrastructure/gmail_gateway.py:102).

Il flusso tipico è:

1. verifica del profilo dell'utente;
2. ricerca paginata dei messaggi con etichetta `SENT`;
3. acquisizione del singolo messaggio in formato completo;
4. nelle sincronizzazioni successive, uso della Gmail History API;
5. riconciliazione completa quando la cronologia remota è scaduta o non più utilizzabile.

### 4.4 Dati estratti

Per ciascun messaggio vengono acquisiti e normalizzati:

- ID Gmail del messaggio;
- ID del thread;
- history ID;
- RFC Message-ID, quando disponibile;
- oggetto;
- indirizzi mittente;
- destinatari principali;
- destinatari CC;
- data originale del messaggio;
- precisione e provenienza della data;
- data interna Gmail;
- data di acquisizione;
- snippet;
- corpo nuovo scritto dall'autore;
- firma;
- testo citato;
- testo inoltrato;
- testo originale plain text;
- testo ricavato dall'HTML;
- riferimenti agli allegati, nome file, tipo MIME, dimensione e identificatore remoto;
- intestazioni considerate utili;
- alias del proprietario attribuito al messaggio;
- evidenze e confidenza dell'attribuzione;
- link diretto al messaggio Gmail;
- hash della revisione e versione della pipeline.

Il contratto normalizzato è [`NormalizedGmailMessage`](../backend/src/gbrain/domain/gmail.py:184); la rappresentazione persistibile viene generata da [`NormalizedGmailMessage.provenance_payload()`](../backend/src/gbrain/domain/gmail.py:228).

### 4.5 Trattamento dei corpi MIME

Il parser:

- decodifica parti MIME testuali;
- separa nuovo testo, firma, citazioni e inoltri;
- converte il contenuto HTML in testo utilizzabile;
- applica limiti dimensionali;
- registra troncamenti e anomalie come evidenze diagnostiche;
- considera il contenuto importato non attendibile ai fini di eventuali elaborazioni successive.

La normalizzazione è orchestrata da [`normalize_gmail_message()`](../backend/src/gbrain/application/gmail_mime.py:182).

Gli allegati non vengono descritti come file binari persistiti dal contratto normalizzato: vengono conservati come riferimenti e metadati. Questo riduce occupazione e superficie di rischio.

### 4.6 Come vengono salvati i dati Gmail

La persistenza è gestita da [`AsyncPostgresGmailRepository.persist_batch()`](../backend/src/gbrain/infrastructure/gmail_repository.py:46).

Per ogni messaggio:

1. viene cercato lo [`SourceItem`](../backend/src/gbrain/domain/models.py:188) con lo stesso account, source kind e ID Gmail;
2. se non esiste, viene creato l'item logico;
3. se l'hash della revisione è nuovo, viene creata una [`SourceRevision`](../backend/src/gbrain/domain/models.py:226);
4. contenuto normalizzato ed evidenze vengono salvati in colonne JSONB;
5. checkpoint e contatori del job vengono avanzati nella stessa unità transazionale prevista dal repository;
6. gli elementi rimossi o usciti dal perimetro vengono marcati con uno stato applicativo, non eliminati silenziosamente.

Il payload normalizzato contiene testo e metadati del messaggio, mentre le evidenze descrivono provenienza, parsing, attribuzione e possibili troncamenti.

### 4.7 Modalità di sincronizzazione

Gmail supporta tre modalità:

- **full**: scansione completa della selezione `SENT`;
- **incremental**: aggiornamento mediante Gmail History API a partire dall'history ID salvato;
- **reconciliation**: nuova scansione controllata quando la cronologia è scaduta o serve verificare divergenze.

Le modalità sono definite in [`GmailSyncMode`](../backend/src/gbrain/domain/gmail.py:28) e orchestrate da [`GmailSentConnector`](../backend/src/gbrain/application/gmail_sync.py:107).

Il job applica:

- paginazione;
- budget massimo di richieste;
- rate limiting condiviso;
- retry con backoff per errori transitori;
- cancellazione cooperativa;
- checkpoint persistenti;
- diagnostica per elementi non importati o problematici;
- protezione contro configurazioni cambiate durante un job.

### 4.8 Anteprima, copertura e diagnostica

Prima dell'import è disponibile un'anteprima read-only. Dopo l'import, l'API espone:

- stato dell'account;
- job attivo e ultimi esiti;
- copertura temporale;
- stato del backfill;
- elementi mancanti o non importati;
- motivazioni diagnostiche senza divulgare token o contenuto sensibile nei log.

Il collaudo operativo dedicato è descritto in [`COLLAUDO_FINALE_GMAIL.md`](COLLAUDO_FINALE_GMAIL.md).

### 4.9 Limiti attuali

- Il perimetro è intenzionalmente limitato a `SENT`.
- Gli allegati sono indicizzati come riferimenti; non costituiscono un archivio binario generale.
- Non viene eseguita analisi LLM del contenuto durante l'import.
- Una cronologia Gmail scaduta richiede riconciliazione.
- L'identità del mittente dipende dagli alias confermati dall'utente.

---

## 5. Integrazione Immich

### 5.1 Obiettivo e perimetro

L'integrazione Immich acquisisce **solo metadati** di foto e video. Gli originali rimangono nella libreria Immich e GBrain non scarica né duplica immagini e video.

Il descrittore del connettore è in [`SOURCE_CATALOG`](../backend/src/gbrain/domain/connectors.py:357) e dichiara:

- accesso read-only;
- autenticazione tramite API key;
- paginazione;
- batch API massimo di 25 asset;
- concorrenza pari a 1.

### 5.2 Come viene effettuata la connessione

L'utente configura:

- URL base dell'istanza Immich;
- API key generata da Immich.

I due valori sono salvati insieme come payload cifrato AES-256-GCM. Il worker li recupera e li decifra solo al momento dell'esecuzione.

Il gateway [`ImmichGateway`](../backend/src/gbrain/infrastructure/immich_gateway.py:129):

1. valida l'URL;
2. rileva la versione del server;
3. verifica che la versione sia supportata;
4. legge il profilo associato alla API key;
5. controlla le capability richieste;
6. usa esclusivamente endpoint di lettura.

Gli URL HTTP sono ammessi soltanto per contesti LAN o loopback conformi alle regole di sicurezza; per endpoint pubblici è richiesto HTTPS.

### 5.3 Compatibilità API

Il gateway supporta:

- il contratto verificato Immich 3.3.3;
- un percorso legacy esplicitamente versionato per le versioni supportate dal gateway.

Per Immich 3.3.3 la ricerca usa `POST /api/search/metadata`, payload strutturato e paginazione cursor-based. Il cursore successivo viene letto dal contenitore degli asset della risposta.

Gli album vengono letti con l'endpoint dell'album che restituisce gli asset inline. Poiché tale endpoint non espone una paginazione nativa, GBrain divide localmente la lista in blocchi da 25 durante la persistenza.

### 5.4 Selezioni disponibili

Il pannello operativo consente tre tipi di selezione:

- **tutta la libreria**: nessun filtro temporale;
- **intervallo date**: limite inferiore e/o superiore;
- **album**: import degli asset appartenenti a uno specifico album.

La validazione è definita da [`ImmichSelection`](../backend/src/gbrain/domain/immich.py:77). Album, intervallo e import completo sono mutuamente esclusivi.

### 5.5 Dati estratti

Per ogni asset vengono estratti e normalizzati:

- ID stabile Immich;
- data dell'evento;
- origine della data scelta;
- tipo di asset: immagine, video o altro;
- descrizione EXIF o descrizione dell'asset;
- album associati (con ID e nome nell'import mirato per album; nella scansione globale o di periodo per libreria l'API Immich non include album inline per singolo asset senza N+1, quindi tale limite è esplicito e gli album non vengono inventati);
- persone associate, con ID e nome;
- identità media (checksum, filename e MIME), dimensioni, durata e rating quando presenti;
- timestamp file/remoto/locale ed EXIF, camera, lente ed esposizione;
- stato favorite/archive/trash/visibility e relazioni stack/duplicato/live-photo;
- località testuale e sola indicazione della presenza GPS, mai coordinate esatte;
- hash canonico `immich-canonical-v2` dei metadati;
- payload di provenienza minimo.

Il contratto è [`ImmichNormalizedAsset`](../backend/src/gbrain/domain/immich.py:120), mentre la normalizzazione è eseguita da [`normalize_immich_asset()`](../backend/src/gbrain/domain/immich.py:142).

La data viene scelta, nell'ordine, tra:

1. data/ora locale dell'asset;
2. data originale EXIF;
3. data di creazione file;
4. data di ultimo aggiornamento.

Un asset senza ID stabile o senza alcuna data valida non viene normalizzato come item valido.

### 5.6 Dati non estratti

L'integrazione corrente non salva:

- file originale della foto o del video;
- path assoluti, ID device grezzi, signed URL, miniature e preview video;
- coordinate GPS esatte (policy predefinita `withhold_exact`);
- coordinate, crop o embedding biometrici dei volti;
- payload remoto completo;
- embedding visuali, risultati del servizio Immich Machine Learning o analisi LLM.

La presenza di `immich-machine-learning` nell'installazione Immich non implica che GBrain lo interroghi.

### 5.7 Come vengono salvati i dati Immich

La persistenza è gestita da [`AsyncPostgresImmichRepository.persist_asset_batch()`](../backend/src/gbrain/infrastructure/immich_repository.py:27).

Per ciascun asset:

1. viene cercato lo [`SourceItem`](../backend/src/gbrain/domain/models.py:188) identificato da account, sorgente Immich e ID asset;
2. se non esiste, viene creato;
3. vengono salvati in JSONB i metadati v2 normalizzati con `schema_version=2`;
4. viene creata una [`SourceRevision`](../backend/src/gbrain/domain/models.py:226) con pipeline `immich-meta-v2` quando l'hash canonico cambia;
5. il solo cambio di etichetta pipeline non crea una revisione, mentre nuovi metadati semantici sì;
6. gli esiti sono distinti in inseriti, aggiornati e invariati;
7. evidenze, stato del job e checkpoint vengono aggiornati atomicamente insieme al batch.

La transazione per batch evita il caso in cui il cursore venga avanzato senza che gli asset corrispondenti siano stati salvati.

La modalità API `enrichment`, esposta come **Forza analisi completa**, crea un nuovo job con cursore nullo, intera libreria e limiti asset/richieste `null` (oppure un budget numerico opzionale se eseguita a tranche riprendibili tramite `resume_from_job_id`). Il retry dello stesso job conserva invece il cursore già committato. I checkpoint distinguono `inserted`, `updated`, `unchanged`, `invalid` e `missing`; `missing` resta `not_evaluated`, perché la copertura delle visibility Immich non autorizza ancora tombstone affidabili. Un budget numerico interrompe la tranche come `completed_with_warnings`, lasciandola riprendibile. Il massimo `remote_updated_at` osservato e `overlap_ready` preparano un futuro incremental, senza dichiararlo implementato.

### 5.8 Import massivo, paginazione e checkpoint

Il job [`execute_immich_job()`](../backend/src/gbrain/application/immich_jobs.py:60) importa gli asset in blocchi da 25.

Per una ricerca su tutta la libreria o per intervallo:

1. il gateway invia una richiesta con massimo 25 elementi;
2. normalizza gli asset ricevuti;
3. il repository salva il blocco;
4. il checkpoint conserva il cursore successivo e i contatori;
5. il job continua finché non termina la libreria o viene raggiunto un limite.

Per un album:

1. viene eseguita una singola lettura remota dell'album;
2. gli asset inline vengono suddivisi localmente in blocchi da 25;
3. il checkpoint conserva l'offset dell'album;
4. un retry riprende dal primo blocco non confermato.

Il checkpoint registra almeno:

- fase del job;
- cursore remoto oppure offset album;
- blocchi completati;
- richieste remote completate;
- asset enumerati;
- asset inseriti;
- duplicati;
- limite totale;
- budget di richieste;
- momento dell'ultimo batch;
- eventuale motivo di arresto incompleto.

### 5.9 Limiti e budget

Sono distinti tre concetti:

- **dimensione batch API**: sempre al massimo 25;
- **item limit**: massimo totale di asset elaborabili dal job;
- **request budget**: massimo numero di richieste remote effettuabili dal job.

Se il lavoro termina perché raggiunge un limite mentre esistono ancora asset remoti, non viene dichiarato completamento pieno: il job termina con avvisi e un motivo osservabile nel checkpoint.

### 5.10 Resume, retry e cancellazione

Il resume è sicuro perché persistenza del batch e avanzamento del checkpoint sono atomici:

- errore prima del commit: il cursore non avanza;
- errore dopo il commit: il cursore salvato permette di riprendere dal blocco successivo;
- replay dello stesso asset: la chiave composta e l'hash canonico mantengono l'idempotenza.

Gli errori transitori del gateway vengono rilanciati al sistema di code per il retry. Gli errori permanenti, come credenziali non valide o versione non supportata, chiudono il job con codice diagnostico.

La cancellazione è cooperativa: la richiesta viene registrata nel job e controllata tra due batch, così il batch transazionale in corso non viene lasciato a metà.

### 5.11 Interfaccia utente

La connessione iniziale usa la wizard, limitata a credenziali e verifica. L'operatività è separata nel pannello [`ImmichConfigPanel`](../frontend/src/ImmichConfigPanel.tsx), che consente di:

- scegliere intera libreria, periodo o album;
- impostare limite totale e budget;
- avviare l'import;
- osservare il progresso tramite polling;
- vedere contatori di elementi elaborati, importati e duplicati;
- annullare il job attivo;
- consultare i metadati già importati.

Il cursore tecnico usato per la paginazione non viene esposto all'utente.

### 5.12 Limiti attuali

- Non esiste sincronizzazione incrementale Immich basata su `updatedAt`.
- Il connettore importa metadati, non contenuti binari.
- L'endpoint album non è paginato da Immich e richiede il caricamento inline della lista remota.
- La concorrenza è intenzionalmente limitata a 1.
- Non viene usato Immich Machine Learning.

---

## 6. Integrazione PostgreSQL

PostgreSQL è il sistema autorevole di GBrain. Conserva:

- utenti e sessioni applicative;
- credenziali cifrate;
- account connettori;
- politiche e configurazioni di sincronizzazione;
- job e checkpoint;
- item sorgente e revisioni;
- diagnostica Gmail;
- eventi outbox;
- tabelle della coda Procrastinate.

Le scritture dei connettori vengono eseguite in transazione. PostgreSQL non è una semplice cache: è la fonte primaria per stato operativo, provenienza e idempotenza.

La configurazione runtime è dichiarata in [`compose.yaml`](../compose.yaml), con password letta da Docker secret.

---

## 7. Integrazione Procrastinate

Procrastinate fornisce la coda asincrona usando PostgreSQL, senza Redis o Kafka.

Viene usato per:

- eseguire import senza bloccare le richieste HTTP;
- assegnare code distinte ai connettori;
- applicare lock per account;
- gestire retry dei guasti transitori;
- mantenere il payload del task privo di segreti;
- rendere osservabile l'associazione tra job applicativo e job di coda.

Le identità condivise delle code sono definite in [`job_queue.py`](../backend/src/gbrain/domain/job_queue.py:1), mentre i task worker sono registrati in [`worker.py`](../backend/src/gbrain/worker.py:1).

---

## 8. Integrazione Neo4j

Neo4j è presente nello stack ed è raggiungibile dal backend, ma **non riceve ancora la proiezione dei dati Gmail o Immich**.

L'implementazione corrente [`GraphProjection`](../backend/src/gbrain/infrastructure/graph.py:8) esegue soltanto verifica di connettività e chiusura del driver.

Di conseguenza:

- PostgreSQL rimane l'unica base dati applicativa autorevole;
- non esiste ancora un worker che trasformi gli outbox event in nodi e relazioni Neo4j;
- la presenza del container Neo4j non deve essere interpretata come grafo semantico già popolato;
- backup e monitoraggio includono comunque il servizio predisposto.

---

## 9. Sicurezza trasversale

### 9.1 Principio read-only

Gmail e Immich sono connettori di sola lettura. Il backend non espone operazioni remote di creazione, modifica o cancellazione dei contenuti originali.

### 9.2 Segreti

- Credenziali client, token OAuth e API key sono cifrati a riposo.
- Il secret master è fornito esternamente, non inserito nel database insieme ai ciphertext.
- I task asincroni contengono solo identificatori.
- I log usano codici e dettagli sanitizzati.
- Le risposte API non restituiscono API key o token.

### 9.3 Isolamento per proprietario

Account, credenziali, job e item sono associati al proprietario autenticato. Gli endpoint verificano la sessione e, per le operazioni mutative, la protezione CSRF.

### 9.4 Provenienza e contenuto non attendibile

I dati provenienti da servizi esterni conservano evidenze della loro origine. Il contenuto Gmail viene marcato come non attendibile, così eventuali pipeline future non possono confonderlo con istruzioni di sistema.

---

## 10. Connettori pianificati

Il catalogo contiene anche descrittori non disponibili:

- Google Drive;
- Google Calendar;
- Synology NAS;
- documenti locali;
- GitHub;
- export ChatGPT;
- OmniRoute.

Queste voci hanno stato `coming soon` in [`SOURCE_CATALOG`](../backend/src/gbrain/domain/connectors.py:273). Attualmente non dispongono di gateway, job di import e persistenza verticale completi. La loro presenza serve a mantenere il catalogo e l'interfaccia estensibili, non rappresenta un'integrazione funzionante.

---

## 11. Tabella comparativa dei dati persistiti

| Aspetto | Gmail | Immich |
|---|---|---|
| Identificatore remoto | ID messaggio Gmail | ID asset Immich |
| Contenuto principale | Corpi testuali MIME normalizzati | Descrizione e metadati |
| File binari | Non archiviati come allegati completi | Mai scaricati |
| Partecipanti | Mittente, destinatari, CC, alias | Persone riconosciute da Immich |
| Raggruppamenti | Thread e history ID | Album |
| Data evento | Header e data interna Gmail | Timeline/EXIF/file/updated |
| Revisioni | Hash del contenuto normalizzato | Hash canonico dei metadati |
| Checkpoint | Page token e history ID | Cursor di ricerca o offset album |
| Incrementale | Sì, History API | No |
| Riconciliazione | Sì | Replay idempotente e scansione selezionata |

---

## 12. Verifiche e riferimenti

Le aree principali sono coperte da test automatici:

- gateway Gmail: [`test_gmail_gateway.py`](../backend/tests/test_gmail_gateway.py);
- flussi operativi Gmail: [`test_gmail_operational.py`](../backend/tests/test_gmail_operational.py);
- checkpoint e modalità Gmail: [`test_gmail_sync_mode_and_checkpoints.py`](../backend/tests/test_gmail_sync_mode_and_checkpoints.py);
- gateway Immich: [`test_immich_gateway.py`](../backend/tests/test_immich_gateway.py);
- persistenza, paginazione, resume, budget, cancellazione e album Immich: [`test_immich_vertical.py`](../backend/tests/test_immich_vertical.py);
- API e ciclo di vita delle fonti: [`test_sources_wizard_backend.py`](../backend/tests/test_sources_wizard_backend.py);
- contratto code multi-connettore: [`test_multi_connector_job_contract.py`](../backend/tests/test_multi_connector_job_contract.py).

Riferimenti di approfondimento:

- [`CONNETTORI.md`](CONNETTORI.md): contratti e catalogo dei connettori;
- [`ARCHITETTURA.md`](ARCHITETTURA.md): confini di sistema e infrastruttura;
- [`MODELLO_DATI.md`](MODELLO_DATI.md): modello concettuale e tabelle;
- [`SICUREZZA.md`](SICUREZZA.md): misure di sicurezza;
- [`adr/003-sincronizzatore-gmail-fase2.md`](adr/003-sincronizzatore-gmail-fase2.md): decisioni Gmail;
- [`adr/004-valutazione-immich-integration.md`](adr/004-valutazione-immich-integration.md): decisioni Immich.

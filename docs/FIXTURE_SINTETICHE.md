# Fixture sintetiche e modalità demo

La modalità demo serve a mostrare e collaudare la fondazione GBrain esclusivamente con dati inventati. È **disabilitata per default**, viene rifiutata in `production` e non modifica autenticazione, autorizzazione, sessioni o protezione CSRF.

Il comando demo non apre connessioni verso Gmail, Immich, OmniRoute o altri servizi esterni e scrive direttamente nel modello di provenienza PostgreSQL. Separatamente, il nucleo Gmail dispone di fixture e fake gateway di test che non sono esposti dalla UI e non effettuano rete.

## Attivazione e comandi

1. Impostare `GBRAIN_ENVIRONMENT=development` oppure `test`.
2. Impostare esplicitamente `GBRAIN_DEMO_MODE=true`.
3. Applicare normalmente la migrazione esistente.
4. Eseguire nel container applicativo:

```console
python -m gbrain.cli demo-fixtures
```

Il comando è idempotente: identificatori, timestamp e contenuti sono deterministici, quindi una riesecuzione aggiorna lo stesso import job, gli stessi source item e le stesse revisioni senza duplicarli.

Per ripristinare i record demo noti prima di ricrearli occorre il flag esplicito:

```console
python -m gbrain.cli demo-fixtures --reset
```

Senza `GBRAIN_DEMO_MODE=true` entrambi i comandi terminano con errore prima di aprire una transazione di scrittura. Il reset verifica identità e marcatori prima della cancellazione e opera soltanto sugli UUID deterministici del dataset, con `source_kind=demo_fixture`, identificativi esterni prefissati `demo:v1:` e marcatore `GBRAIN_SYNTHETIC_DEMO_FIXTURE`. Record non-demo non vengono selezionati né cancellati. Una collisione con un record privo di marcatura interrompe l'operazione.

## Dati visibili

Il seed crea realmente, nello schema corrente:

- 1 `ImportJob` completato con checkpoint/progresso sintetico, elenco casi e indicazione `network_access=false`;
- 12 `SourceItem` nel namespace riservato;
- 14 `SourceRevision` con payload JSONB marcati e senza riferimenti a file personali.

L'import job appare nell'endpoint privato `/api/imports` e quindi nella vista Fonti. L'endpoint privato `/api/demo/status` restituisce conteggi effettivi, conteggi attesi e stato di completezza. Banner e pannello persistente della UI dichiarano che si tratta di ambiente sintetico e che nessun servizio reale è collegato. Entrambi gli endpoint privati continuano a richiedere una sessione valida; le operazioni mutanti esistenti continuano a richiedere CSRF.

## Casi coperti

Tutti i soggetti sono dichiarati fittizi, gli indirizzi usano `example.test` e i payload hanno `representation=fixture_observation`: non sono claim autobiografici confermati.

| Caso | Rappresentazione corrente |
|---|---|
| Due omonimi | Due persone fittizie con stesso nome visualizzato ma ID e indirizzi demo distinti nei metadati del thread. |
| Alias del proprietario | Elenco di indirizzo principale e alias demo nel thread. |
| Mail ricevuta in thread con posta inviata | Un payload thread con due messaggi e direzioni `sent`/`received`. |
| Inoltro | Mail sintetica con flag e catena di inoltro strutturata. |
| Data ambigua | Valore grezzo `03/04/2024`, due candidati e stato `unresolved`; `occurred_at` resta nullo. |
| Preferenza cambiata | Due revisioni dello stesso item, entrambe osservazioni non confermate, con relazione di supersessione nei metadati. |
| Foto senza data | Metadato foto senza binario e senza data; `occurred_at` resta nullo. |
| Persona etichettata erroneamente | Etichetta `known_incorrect`, correzione fittizia e richiesta di revisione. |
| Duplicato e revisione | Item con due revisioni più item duplicato separato che dichiara il riferimento all'originale. |
| Messaggi identici ma distinti | Due source item con contenuto uguale e chiavi messaggio differenti. |
| Video visto senza opinione | Stato `seen`, opinione nulla e policy esplicita di non inferenza. |
| Prompt injection malevola | Stringa memorizzata come `untrusted_text`, `literal_content_only`, non eseguibile e non istruzione di sistema. |
| Fonte eliminata/esclusione | Item con `deleted_at`, tombstone e `excluded_from_processing=true`. |

## Limiti intenzionali della Fase 1

Lo schema corrente normalizza provenienza, revisioni, cancellazione e import job, ma non dispone ancora di entità normalizzate per persone/alias, thread e singoli messaggi, media/EXIF, preferenze temporali, risoluzione delle date, deduplicazione semantica, opinioni o claim epistemici. Questi aspetti sono quindi payload strutturati di fixture in `SourceRevision.normalized_metadata`, marcati `normalization_status=future_case_payload`.

Il dataset demo non simula estrazione LLM, Graphiti, grafi, sincronizzazioni o connettori. Non viene creato alcun claim autobiografico e il testo di prompt injection non viene mai promosso a istruzione. Questi payload demo sono contratti di test prospettici.

## Corpus Gmail fixture-first

[`backend/tests/fixtures/gmail/messages.json`](../backend/tests/fixtures/gmail/messages.json) e il fake contrattuale [`backend/tests/fakes/gmail_gateway.py`](../backend/tests/fakes/gmail_gateway.py) coprono dati inventati per alias confermati, thread misto SENT/ricevuto, fetch diretto ricevuto, inoltro, quote/firma, HTML con tracker e script, prompt injection, charset, multipart/allegato, ID distinti con testo identico e alias non confermato. I test aggiungono scenari a più pagine, history mista/scaduta, 429/5xx/revoca, crash dopo commit, revisione, preview troncata, rimozione ed exclusion preservata.

Le fixture non contengono token, dati personali reali né claim autobiografici; le email restano materiale di fonte non elevato a fatto autobiografico.

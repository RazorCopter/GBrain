# Piano operativo Fase 2 — Gmail `SENT`

## Stato e confini

- **Stato:** backlog approvabile; nessuna voce di questo documento è implementata.
- **Prerequisito:** la fondazione Fase 1 resta utilizzabile per sviluppo deterministico, ma il collaudo Docker/Linux reale di migrazione, persistenza, worker, limiti, mount e backup/restore è ancora un gate separato.
- **Incremento ristretto:** importazione read-only dei soli messaggi Gmail inviati dal proprietario, prima con fixture e contract test, poi con collaudo reale esplicitamente autorizzato.
- **Fuori ambito:** inbox generica, invio/modifica/cancellazione Gmail, Immich, NAS, OmniRoute, Graphiti, sink Neo4j, estrazione LLM e collegamento di servizi personali durante la preparazione del backlog.
- **Regola di evidenza:** distinguere sempre codice predisposto, test deterministico superato e integrazione Gmail reale eseguita. L'ultima non può essere dichiarata senza credenziali, account e autorizzazione esplicita.

## Decisioni da chiudere prima del codice di rete

1. **Identificare i candidati:** registrare repository, versione/commit, maintainer e licenza dei due MCP candidati citati nella specifica. Nel repository corrente non sono nominati; sono quindi indicati come MCP A e MCP B, senza assumerne identità.
2. **Audit MCP A e MCP B:** per ciascuno verificare provenienza, manutenzione, licenza, dipendenze transitive, modalità di custodia token, scope richiesti, supporto a paginazione/history/MIME, telemetria, esecuzione di comandi, superficie prompt/tool e comportamento su retry/rate limit.
3. **Audit client Gmail ufficiale:** valutare libreria Google supportata, versioni compatibili, licenza, dipendenze, flusso OAuth installato/web appropriato al deployment, gestione refresh token, timeout/retry e copertura delle API necessarie.
4. **Decisione unica del sincronizzatore:** scegliere **un solo** percorso di produzione tra MCP A, MCP B e client ufficiale. Vietare sincronizzatori paralleli sullo stesso account/checkpoint; gli altri possono restare esclusivamente adattatori sperimentali isolati o essere scartati.
5. **ADR obbligatoria:** documentare confronto, decisione, versione pin, modello di minaccia, modalità di rollback e motivazione. Nessuna nuova dipendenza entra nei lockfile prima di questa decisione.

## Backlog prioritizzato

### P0 — Contratto deterministico e invarianti di sicurezza

**F2-001 — Contratto del connettore Gmail `SENT`**

- Definire tipi separati per locator, pagina, messaggio grezzo, messaggio normalizzato e checkpoint.
- Rendere espliciti timeout, retry classificati, errori permanenti/transitori, limite pagina e budget di richieste.
- Imporre che il connettore non scriva direttamente su Neo4j e che PostgreSQL resti autorevole.
- Criterio di accettazione: contract test eseguibili interamente con fixture, senza rete né credenziali.

**F2-002 — Dataset fixture Gmail e falsi deterministici**

- Coprire thread con ricevute e inviate, alias del proprietario, destinatari multipli, inoltro, date ambigue, duplicati/revisioni, messaggi distinti con contenuto identico, cancellazione/non reperibilità e prompt injection nel contenuto.
- Includere pagine multiple, token pagina scaduto/non valido, history gap, retry, `429`, `5xx` e MIME malformato.
- Non usare messaggi o identificativi personali.
- Criterio di accettazione: risultati, hash, ordinamento e checkpoint identici a ogni esecuzione.

**F2-003 — Modello di identità e provenienza**

- Definire chiavi stabili basate sugli identificativi Gmail, senza deduplicare messaggi distinti in base al solo corpo.
- Conservare account/source, message ID Gmail, thread ID, history ID, internal date, header Message-ID e hash della revisione.
- Mappare il contratto sulle tabelle autorevoli e introdurre migrazioni additive soltanto se necessarie.
- Criterio di accettazione: replay della stessa fixture non duplica item/revisioni e una modifica osservabile produce una revisione tracciabile.

**F2-004 — Confine trust e prompt injection**

- Trattare subject, header, body, allegati e snippet come dati non fidati.
- Vietare che contenuto email modifichi filtri, checkpoint, strumenti, query o istruzioni di sistema.
- Preservare il testo originale necessario alla citazione e produrre rappresentazioni derivate separate.
- Criterio di accettazione: fixture ostili restano contenuto citabile e non attivano comandi, tool o cambi di policy.

### P1 — Selezione `SENT`, OAuth e normalizzazione

**F2-005 — OAuth read-only e custodia credenziali**

- Richiedere il minimo scope Gmail read-only compatibile con list/fetch/history; vietare scope di modifica o invio.
- Definire bootstrap consenso, refresh, revoca, rotazione, file/secret store, permessi e redazione log.
- Non inserire client secret, authorization code o refresh token in database grezzo, fixture, log, backup applicativo o repository.
- Criterio di accettazione deterministico: configurazione incompleta rifiutata con errore sicuro; nessun test richiede un token reale.
- Collaudo reale: pendente fino a credenziali e account di prova esplicitamente autorizzati.

**F2-006 — Identità proprietario e alias**

- Ottenere l'indirizzo primario e l'insieme degli alias `sendAs` autorizzati usando endpoint/scopo compatibili con la decisione OAuth.
- Normalizzare gli indirizzi senza inferire alias non restituiti dall'API.
- Versionare il set alias usato da ogni import per rendere verificabile la selezione.
- Criterio di accettazione: fixture con alias primario/secondario include solo messaggi realmente inviati dal proprietario.

**F2-007 — Enforcement `SENT` a difesa multipla**

- Applicare un filtro server-side Gmail limitato alla posta inviata durante l'enumerazione.
- Dopo ogni fetch verificare label/metadata disponibili e almeno un mittente appartenente al set owner/alias previsto.
- Applicare lo stesso vincolo agli elementi scoperti via history; non fidarsi del solo evento history o del solo risultato list.
- Rifiutare/quarantenare elementi ambigui senza avanzare oltre un checkpoint non sicuro.
- Criterio di accettazione: fixture INBOX-only, draft, spam, messaggio ricevuto nello stesso thread e mittente estraneo non vengono persistiti come `SENT`.

**F2-008 — Parser MIME limitato e robusto**

- Gestire multipart annidati, `text/plain`, `text/html`, charset, transfer encoding, header encoded-word e parti mancanti/malformate.
- Stabilire una preferenza deterministica del testo e una sanitizzazione/estrazione che non esegua HTML o URL.
- Registrare metadata degli allegati senza scaricare blob in questa prima tranche, salvo decisione esplicita successiva.
- Applicare limiti a dimensione, profondità MIME, numero parti e tempo di parsing.
- Criterio di accettazione: corpus fixture produce testo/citazioni stabili e fallimenti classificati, senza crash o consumo illimitato.

### P1 — Sincronizzazione riprendibile

**F2-009 — Paginazione e checkpoint transazionale**

- Persistire stato pagina/cursore solo insieme ai record della pagina già validati.
- Non avanzare il checkpoint su errore parziale; rendere sicuro il replay dell'ultima pagina.
- Separare bootstrap iniziale, sincronizzazione incrementale e stato di errore.
- Criterio di accettazione: arresto simulato in ogni punto riprende senza perdita né duplicazione.

**F2-010 — Gmail history e reconcile**

- Usare history solo dopo un bootstrap completo con history ID valido.
- Gestire history ID scaduto/gap con reconcile limitato e osservabile, non con reset silenzioso.
- Definire finestra/strategia di riconciliazione per modifiche label, messaggi non reperibili e ritardi.
- Criterio di accettazione: gap simulato porta a reconcile deterministico e conserva l'ultimo checkpoint certamente completato.

**F2-011 — Idempotenza, revisioni, esclusioni e cancellazioni**

- Applicare upsert per identità sorgente e hash della revisione.
- Integrare exclusion/deletion state in modo che replay e reconcile non facciano ricomparire record esclusi o cancellati.
- Produrre outbox nella stessa transazione dei cambi autorevoli, senza implementare il sink Neo4j.
- Criterio di accettazione: doppia importazione e retry producono lo stesso stato PostgreSQL e gli stessi eventi stabili.

**F2-012 — Scheduling e backpressure**

- Eseguire un solo sincronizzatore per account/checkpoint; usare lock transazionale/advisory documentato.
- Definire batch limitati, jitter/backoff, rispetto `Retry-After`, massimo retry e dead-letter/error code osservabile.
- Non aumentare la concorrenza worker oltre il vincolo corrente senza un benchmark e una decisione separata.
- Criterio di accettazione: due avvii concorrenti non elaborano indipendentemente lo stesso checkpoint.

### P2 — Ricerca testuale e citazioni

**F2-013 — Indice testuale PostgreSQL**

- Definire campi indicizzati derivati da subject, mittente/destinatari, data e corpo normalizzato.
- Conservare separazione tra payload/provenienza e rappresentazione ricercabile.
- Non introdurre motori di ricerca aggiuntivi in questo incremento.
- Criterio di accettazione: query fixture note restituiscono risultati deterministici entro limiti documentati.

**F2-014 — Citazioni verificabili**

- Ogni risultato deve includere source kind, account logico, message/thread ID, timestamp, revisione e frammento derivato.
- Il frammento deve essere riconducibile alla revisione persistita; nessuna risposta deve presentare inferenze come testo della fonte.
- Criterio di accettazione: contract test verificano round-trip tra risultato, revisione e testo citato.

### P2 — Osservabilità e collaudo reale

**F2-015 — Stato import e diagnostica sicura**

- Esporre contatori pagina/item/retry, checkpoint non sensibile, ultimo successo ed error code senza body, token o indirizzi completi nei log.
- Definire metriche/soglie per quota, backlog, errori permanenti e checkpoint fermo.
- Criterio di accettazione: test di redazione impediscono la comparsa di token e contenuto email nei log.

**F2-016 — Gate di collaudo reale autorizzato**

- Usare un account di prova o un sottoinsieme controllato, consenso esplicito e credenziali revocabili.
- Verificare scope effettivi, alias, enforcement `SENT`, paginazione, history/reconcile, quote, retry e revoca.
- Confrontare campione API con stato PostgreSQL e citazioni; registrare data, account di prova non sensibile, versione client e risultati.
- Revocare/eliminare credenziali di prova secondo runbook.
- Stato iniziale: **NON ESEGUITO — accessi e autorizzazione mancanti**.

## Sequenza di delivery e gate

1. Chiudere audit MCP A/MCP B/client ufficiale e ADR del singolo sincronizzatore.
2. Implementare contratto, fixture e test P0 senza rete.
3. Implementare storage/migrazione e pipeline fixture con test PostgreSQL deterministici disponibili; non dichiarare integrazione reale se usa fake.
4. Implementare adattatore scelto dietro il contratto, mantenendo OAuth disabilitato senza configurazione esplicita.
5. Superare enforcement `SENT`, MIME, pagination/checkpoint/reconcile/idempotenza e prompt-injection test.
6. Aggiungere ricerca testuale e citazioni verificabili senza introdurre un nuovo datastore.
7. Solo dopo autorizzazione, eseguire il collaudo Gmail reale e registrare limiti/rate effettivi.

## Criterio di uscita della prima tranche Fase 2

La tranche è pronta per revisione quando tutti i contract test fixture sono verdi, il sincronizzatore scelto è documentato/pinnato, il filtro `SENT` è verificato a più livelli, replay/interruzione/history gap sono deterministici, MIME e prompt injection hanno limiti espliciti, e ricerca/citazioni rinviano alla revisione sorgente. L'accesso reale resta una voce separata e non può essere dedotto dai test fixture.

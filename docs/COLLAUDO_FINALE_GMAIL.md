# Collaudo finale Gmail

> Stato: il collaudo su server, Gmail reale, LXC e servizi esterni **non è stato eseguito localmente**. Questa runbook descrive prove manuali non distruttive da svolgere nell'ambiente autorizzato.

## Prerequisiti

- account Gmail del proprietario collegato e alias mittente confermato;
- worker e database operativi, senza job Gmail attivo;
- configurazione desiderata salvata (revisione, filtro e periodo annotati);
- una mail già importata e identificabile senza includere dati personali negli screenshot;
- possibilità di inviare una nuova mail dall'account proprietario verso un destinatario di test;
- accesso alla pagina Fonti e ai dettagli del job, senza accesso diretto o modifiche manuali al database.

Prima delle prove acquisire: totale autorevole “nel database”, revisione configurazione, periodo desiderato, intervallo effettivamente importato, stato backfill/copertura e identificativo dell'ultimo job. Oscurare indirizzi, oggetti, contenuti e token.

## 1. Ricerca e apertura di una mail importata

1. Cercare una mail già importata con termini non sensibili e sufficientemente selettivi.
2. Verificare che la UI distingua elementi mostrati, risultati trovati e totale nel database.
3. Aprire il risultato e verificare metadati, citazione/provenance e contenuto atteso.

**Evidenze:** schermata dei conteggi, identificativo sorgente anonimizzato, apertura riuscita e prova della provenance. Il totale nel database non deve essere interpretato come la dimensione limitata della lista.

## 2. Ripetizione idempotente della stessa importazione

1. Annotare totale nel database, revisione, periodo desiderato e intervallo importato.
2. Avviare nuovamente la stessa importazione dello storico senza cambiare configurazione.
3. Attendere lo stato terminale del job e ricaricare la pagina.
4. Verificare `0` nuovi elementi, duplicati riconosciuti secondo i contatori del job e nessun incremento del totale autorevole.
5. Verificare che completamento del job e copertura rimangano indicatori separati: la copertura è completa solo se selezione/revisione e intervallo coincidono e non resta un checkpoint parziale.

**Evidenze:** job ID, configurazione/revisione, contatori prima/dopo, totale prima/dopo, stato backfill e copertura. Un backfill storico di una revisione diversa deve essere indicato come precedente, non come corrente.

## 3. Nuova email e “Aggiorna ora”

1. Annotare il totale autorevole iniziale.
2. Inviare una sola nuova email dall'account proprietario verso il destinatario di test, con oggetto univoco.
3. Se necessario attendere la disponibilità nella cronologia Gmail, poi selezionare **Aggiorna ora** una sola volta.
4. Attendere lo stato terminale, cercare l'oggetto univoco e aprire la mail.
5. Verificare provenance e incremento esatto del totale da `N` a `N + 1`.

**Evidenze:** timestamp di invio e aggiornamento, job ID, contatori del job, ricerca/apertura riuscita, totale prima/dopo. Non acquisire corpo o destinatari reali non necessari.

## Non distruttività e rollback

Le prove non eliminano sorgenti, credenziali, messaggi, checkpoint o configurazioni e non richiedono query correttive sul database. La ripetizione dell'import è intenzionalmente idempotente. Usare esclusivamente una mail di test nuova e non modificare il periodo durante il collaudo.

In caso di anomalia interrompere ulteriori avvii, conservare job ID, revisione, periodo, range effettivo, contatori e timestamp. Il rollback consiste nel non proseguire e, solo se un job è ancora attivo, richiederne l'annullamento dalla UI; non cancellare dati, non resettare checkpoint e non alterare manualmente lo stato persistito.

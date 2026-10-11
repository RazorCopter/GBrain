# Setup OAuth Gmail read-only

## Stato e confine

L'integrazione usa unicamente il client ufficiale Google e lo scope esatto:

`https://www.googleapis.com/auth/gmail.readonly`

Lo scope autorizza tecnicamente la lettura dell'intera casella. Il wrapper applicativo non accetta query inserite dall'utente ed esegue soltanto operazioni di lettura su messaggi con etichetta `SENT`; ogni fetch diretto riverifica `SENT`. Non esistono operazioni send, modify o drafts. Nessuna password Google viene richiesta o conservata.

Il repository e i test non collegano un account reale. In assenza dei secret la pagina Fonti mostra correttamente **non configurato**.

## Azione singola richiesta in Google Cloud

Creare/scaricare un client OAuth 2.0 di tipo **Applicazione web**, registrando esattamente la callback:

- sviluppo: `http://localhost:8000/api/gmail/oauth/callback`;
- produzione: `${GBRAIN_PUBLIC_URL}/api/gmail/oauth/callback`, obbligatoriamente HTTPS.

Salvare il JSON scaricato come `secrets/gmail-oauth-client.json`. Il file è ignorato da Git e viene montato read-only come `/run/secrets/gmail_oauth_client`.

Non ampliare gli scope nel client o nella consent screen. Il server rifiuta token con scope diverso dall'unico scope readonly.

## Chiave dei token

Generare 32 byte casuali e salvarli in forma esadecimale (64 caratteri) in un secret file, ad esempio `secrets/gmail-token-key.txt`. Configurare `GBRAIN_GMAIL_TOKEN_ENCRYPTION_KEY_FILE` con lo stesso percorso/mount sia per app sia per worker. È una chiave distinta dal secret di sessione.

I token sono scambiati server-side, cifrati con AES-256-GCM autenticato e formato versionato prima del database. Token, client secret, verifier PKCE e state non vengono inviati al frontend, salvati in localStorage o inseriti nei payload Procrastinate. Il browser riceve soltanto l'URL Google di autorizzazione.

## Variabili

- `GBRAIN_GMAIL_OAUTH_CLIENT_SECRET_FILE=/run/secrets/gmail_oauth_client`
- `GBRAIN_GMAIL_TOKEN_ENCRYPTION_KEY_FILE=/run/secrets/gmail_token_key`
- `GBRAIN_GMAIL_OAUTH_REDIRECT_URI=<PUBLIC_URL>/api/gmail/oauth/callback`

Il profilo Compose di base non monta questi secret opzionali, così il setup può restare pendente senza impedire l'avvio. Per attivarlo, aggiungere ai servizi `app` e `worker` gli stessi due mount/secrets e le tre variabili sopra (oppure un file override Compose locale non versionato). `GBRAIN_PUBLIC_URL` e redirect devono coincidere. In produzione entrambi usano HTTPS e i cookie di sessione devono essere secure.

## Flusso e protezioni

1. l'owner autenticato avvia OAuth con POST protetta CSRF;
2. il server genera state ad alta entropia monouso, lega owner e sessione, imposta scadenza 5–15 minuti e cifra il verifier PKCE;
3. la callback richiede la stessa sessione, valida binding/scadenza/replay e consuma lo state prima dello scambio;
4. lo scambio, la verifica dello scope e `users.getProfile` avvengono server-side;
5. l'indirizzo del profilo è proposto come alias, ma non vale finché l'owner non lo conferma esplicitamente;
6. almeno un alias confermato è richiesto per preview/import;
7. refresh e gateway ricontrollano lo scope esatto.

## Revoca e rinnovo

**Revoca e disconnetti** tenta la revoca remota Google quando la rete è disponibile e rimuove sempre localmente ciphertext, scope e alias. L'esito distingue revoca remota confermata, fallita o non tentata; non viene mai mostrato un falso successo remoto.

Un errore auth/refresh marca il lavoro come fallito e richiede una nuova autorizzazione. Errori transitori/quota sono gli unici ritentati nel budget; history scaduta richiede esplicitamente riconciliazione.

## Coda e policy

Preview, full import, incrementale e riconciliazione usano il perimetro server-side SENT. La preview non persiste messaggi completi e non avanza checkpoint. L'import richiede una seconda conferma e il server confronta la snapshot esatta della policy mostrata. I task Procrastinate sono persistenti e serializzati con lock globale iniziale; esiste inoltre un solo job pending/running per account.

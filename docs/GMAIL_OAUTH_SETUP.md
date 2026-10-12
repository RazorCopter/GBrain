# Setup OAuth Gmail read-only

## Stato, confini e requisiti di sicurezza

L'integrazione utilizza unicamente il client ufficiale Google API e richiede lo scope esatto e minimale:

`https://www.googleapis.com/auth/gmail.readonly`

Lo scope autorizza tecnicamente la lettura della casella postale dell'utente in modalità sola lettura. L'applicazione impone le seguenti invarianti a garanzia della massima privacy:
- Il wrapper applicativo non accetta query arbitrarie inserite dall'utente ed esegue **esclusivamente** operazioni di lettura su messaggi recanti l'etichetta di sistema `SENT` (messaggi inviati dall'utente);
- Ogni fetch diretto ricontrolla la presenza del label `SENT`;
- Non esistono operazioni di invio (`send`), modifica (`modify`), bozze (`drafts`) o cancellazione;
- Nessuna password Google dell'utente viene mai richiesta o memorizzata;
- In assenza di configurazione, la pagina Fonti riporta fedelmente lo stato **non configurato**.

---

## 1. Procedura Principale (Consigliata) — Source Connection Wizard via Web UI

A partire da questa versione, l'attivazione della fonte Gmail **non richiede la modifica manuale di file Compose o il montaggio di file sul filesystem dell'host**. L'intera configurazione si effettua direttamente dall'interfaccia utente di GBrain.

### Passo 1.1: Creazione delle credenziali in Google Cloud Console
1. Accedere alla [Google Cloud Console](https://console.cloud.google.com/);
2. Selezionare o creare un progetto (es. `GBrain Private`);
3. In **API e servizi > Libreria**, cercare e abilitare la **Gmail API**;
4. In **Schermata consenso OAuth**:
   - Tipo utente: **Esterno**;
   - Stato pubblicazione: **In prova (Testing)**;
   - Aggiungere il proprio indirizzo email personale alla lista **Utenti di test**;
   - Aggiungere lo scope `https://www.googleapis.com/auth/gmail.readonly`;
5. In **API e servizi > Credenziali**:
   - Cliccare su **+ Crea credenziali > ID client OAuth**;
   - Tipo di applicazione: **Applicazione web**;
   - Nome: `GBrain Client`;
   - In **URI di reindirizzamento autorizzati**, inserire l'URI esatto di callback:
     - Per installazioni con dominio HTTPS: `https://<IL_TUO_DOMINIO>/api/gmail/oauth/callback` (es. `https://gbrain.lan.home/api/gmail/oauth/callback`);
     - Per sviluppo locale o tunnel loopback: `http://localhost:8000/api/gmail/oauth/callback`;
6. Scaricare il file JSON delle credenziali appena create (pulsante **Scarica JSON**).

### Passo 1.2: Configurazione guidata tramite il Wizard
1. Aprire la dashboard di GBrain e recarsi nella pagina [`Fonti`](frontend/src/SourcesPage.tsx:1) (`/sources`);
2. Nel catalogo connettori, individuare la scheda **Gmail SENT** e cliccare su **Configura**;
3. Nel **Source Connection Wizard** ([`frontend/src/SourceConnectionWizard.tsx`](frontend/src/SourceConnectionWizard.tsx:1)) sono disponibili due modalità:
   - **Upload File JSON (Scelta consigliata):** trascinare o selezionare il file JSON scaricato da Google Cloud. Il sistema parserizza istantaneamente il documento ed estrae `client_id` e `client_secret`;
   - **Inserimento Manuale:** incollare o digitare `Client ID` e `Client Secret` nei rispettivi campi form;
4. Cliccare su **Salva e attiva fonte**:
   - Il backend valida i formati dei parametri;
   - I secret vengono crittografati con cifratura autenticata **AES-256-GCM** e persistiti nella tabella `source_credentials` su PostgreSQL tramite [`backend/src/gbrain/infrastructure/source_credentials_repository.py`](backend/src/gbrain/infrastructure/source_credentials_repository.py:1);
   - Lo stato del connettore passa immediatamente a **Disconnesso (Pronto per autorizzazione)**.

---

## 2. Vincolo Google OAuth sulle LAN HTTP e Modalità Operative Consigliate

### 2.1 Il vincolo tecnico di Google Cloud
Per stringenti policy di sicurezza, **Google OAuth 2.0 rifiuta categoricamente gli indirizzi IP privati su protocollo HTTP non cifrato** (come ad esempio `http://192.168.1.150:8000/api/gmail/oauth/callback`). Inserendo un IP LAN HTTP, la console Google Cloud restituirà un errore di tipo:
> *Invalid Redirect: redirect_uri must use HTTPS or be a localhost address.*

### 2.2 Modalità Operative Supportate

| Scenario Operativo | Configurazione URI di Callback | Note e Requisiti |
|---|---|---|
| **Home Lab / NAS con Reverse Proxy (Consigliato)** | `https://gbrain.tuodominio.it/api/gmail/oauth/callback` | Richiede un certificato SSL (Let's Encrypt o locale con CA fidata). Impostare `GBRAIN_PUBLIC_URL=https://gbrain.tuodominio.it` e `GBRAIN_SESSION_SECURE_COOKIE=true`. |
| **Sviluppo Locale su PC** | `http://localhost:8000/api/gmail/oauth/callback` | Consentito nativamente da Google su HTTP standard senza HTTPS. |
| **Server Headless / NAS senza Dominio Pubblico (Tunnel SSH)** | `http://localhost:8000/api/gmail/oauth/callback` | Creare un port forwarding SSH dal PC client verso il server: `ssh -L 8000:localhost:8000 utente@ip-del-nas`. Aprire il browser su `http://localhost:8000` per completare il flusso di autenticazione. |
| **Rete VPN con MagicDNS (es. Tailscale)** | `https://gbrain.tailnet-name.ts.net/api/gmail/oauth/callback` | Abilitare i certificati HTTPS automatici Tailscale per ottenere un FQDN HTTPS valido e privato. |

---

## 3. Procedura Avanzata / Legacy — Montaggio File Secret su Filesystem

Per ambienti completamente headless, container orchestrati via CI/CD o scenari GitOps in cui si preferisce gestire i segreti tramite file read-only montati dall'infrastruttura, il supporto filesystem è pienamente attivo come fallback non distruttivo.

1. Salvare il file JSON scaricato da Google nella cartella dei segreti: `secrets/gmail-oauth-client.json` (il file è escluso da Git tramite `.gitignore`);
2. Montare il file nei servizi `app` e `worker` del file Compose:
   ```yaml
   secrets:
     gmail_oauth_client:
       file: ./secrets/gmail-oauth-client.json
   ```
3. Configurare la variabile d'ambiente:
   `GBRAIN_GMAIL_OAUTH_CLIENT_SECRET_FILE=/run/secrets/gmail_oauth_client`
4. **Gerarchia di Risoluzione:** All'avvio dell'autorizzazione, [`backend/src/gbrain/application/gmail_oauth.py`](backend/src/gbrain/application/gmail_oauth.py:1) verifica prima la presenza di credenziali configurate nel database; se non presenti, effettua il caricamento trasparente dal secret file del filesystem.

---

## 4. Chiavi di Cifratura e Gestione dei Token

### 4.1 Chiave Master di Cifratura
Tutti i token Google scambiati e i secret memorizzati nel database relazionale vengono cifrati tramite crittografia simmetrica **AES-256-GCM** autenticata.
- La chiave crittografica è a 256 bit (32 byte esadecimali / 64 caratteri HEX);
- Può essere definita tramite la variabile d'ambiente unificata `GBRAIN_ENCRYPTION_MASTER_KEY` oppure tramite il file di chiave storico puntato da `GBRAIN_GMAIL_TOKEN_ENCRYPTION_KEY_FILE=/run/secrets/gmail_token_key`;
- Per generare una chiave sicura:
  ```bash
  openssl rand -hex 32
  ```

### 4.2 Riservatezza dei Token e Formato Crittografico
I token OAuth (access token e refresh token) sono scambiati unicamente server-side, cifrati prima della memorizzazione in PostgreSQL e non vengono mai esposti al browser, memorizzati in `localStorage` o trasmessi nelle code dei task Procrastinate. Il frontend riceve unicamente l'URL di consenso Google e lo stato operativo.

---

## 5. Flusso di Autorizzazione e Invarianti di Sicurezza

1. **Inizializzazione:** L'owner autenticato avvia la procedura OAuth tramite una chiamata POST protetta da cookie di sessione e token anti-CSRF;
2. **State e PKCE:** Il server genera uno `state` monouso ad alta entropia calcolato con HMAC-SHA256 legato all'owner e alla sessione attiva, con scadenza tassativa di 10 minuti, e cifra il code verifier PKCE con AES-256-GCM;
3. **Validazione Callback:** La callback Google esige la medesima sessione HTTP, convalida lo stato, previene qualsiasi tentativo di replay e invalida lo state prima dello scambio dei token;
4. **Scambio Server-Side:** Lo scambio del code per i token di accesso e la chiamata a `users.getProfile` avvengono esclusivamente backend-to-backend;
5. **Verifica Alias:** L'indirizzo email estratto dal profilo viene proposto all'utente come alias; nessun import o preview può essere avviato finché l'owner non conferma esplicitamente almeno un alias (`alias_required`);
6. **Revoca e Disconnessione:** Il comando "Revoca e disconnetti" cancella permanentemente le credenziali e i token locali dal database PostgreSQL e tenta contestualmente la revoca remota presso gli endpoint Google.

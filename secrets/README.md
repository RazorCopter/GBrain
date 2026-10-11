# Secret locali

I secret non devono essere aggiunti al repository. `compose.yaml` converte i secret obbligatori della fondazione in file sotto `/run/secrets`; i due secret Gmail sono opt-in e vanno montati tramite override Compose locale come descritto nel setup.

Per esecuzioni esterne a Compose, il backend accetta anche percorsi espliciti tramite:

- `GBRAIN_POSTGRES_PASSWORD_FILE`
- `GBRAIN_AUTH_SECRET_FILE`
- `GBRAIN_NEO4J_PASSWORD_FILE`
- `GBRAIN_GMAIL_OAUTH_CLIENT_SECRET_FILE` (JSON OAuth Web ufficiale, montato read-only)
- `GBRAIN_GMAIL_TOKEN_ENCRYPTION_KEY_FILE` (chiave esadecimale di 32 byte, file separato e condiviso da app/worker)

I file di valore contengono soltanto il secret, con eventuale newline finale. I file `*.txt` e `*.json` in questa directory sono ignorati da Git. I token Gmail vengono cifrati con AEAD versionata prima della persistenza; non devono comparire in browser, localStorage, log o payload della coda. Setup completo in [`docs/GMAIL_OAUTH_SETUP.md`](../docs/GMAIL_OAUTH_SETUP.md).

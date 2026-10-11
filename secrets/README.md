# Secret locali

I secret non devono essere aggiunti al repository. `compose.yaml` converte le variabili host indicate in `example.env` in file sotto `/run/secrets`.

Per esecuzioni esterne a Compose, il backend accetta anche percorsi espliciti tramite:

- `GBRAIN_POSTGRES_PASSWORD_FILE`
- `GBRAIN_AUTH_SECRET_FILE`
- `GBRAIN_NEO4J_PASSWORD_FILE`

Ogni file deve contenere soltanto il valore del secret, con eventuale newline finale. I file `*.txt` in questa directory sono ignorati da Git.

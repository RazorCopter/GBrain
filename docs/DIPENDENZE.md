# Dipendenze e pin — fondazione Fase 1

L'inventario verificabile di immagini, dipendenze dirette, versioni e stato licenze è mantenuto in [`COMPONENTI_LICENZE.md`](COMPONENTI_LICENZE.md). Questa pagina riassume la politica tecnica.

## Sorgenti autorevoli

- Python: [`pyproject.toml`](../pyproject.toml) dichiara i pin diretti; [`uv.lock`](../uv.lock) blocca risoluzione, sorgenti e hash.
- Frontend: [`frontend/package.json`](../frontend/package.json) dichiara i pin diretti; [`frontend/package-lock.json`](../frontend/package-lock.json) blocca l'albero npm.
- Container: [`Dockerfile`](../Dockerfile) e [`compose.yaml`](../compose.yaml) fissano versione e digest delle immagini base/infrastrutturali.

Non usare `latest` e non aggiornare un pin senza rigenerare il relativo lockfile, eseguire verifiche, aggiornare inventario/licenze e provare migrazioni/restore quando coinvolge datastore.

## Runtime corrente

- Python `>=3.12,<3.15`; immagine runtime 3.12.12 slim Bookworm.
- FastAPI/Starlette/Pydantic, SQLAlchemy/Alembic, psycopg, Procrastinate e driver Neo4j con versioni esatte nel lockfile.
- Node 24.12.0 solo nello stage builder.
- React 18.3.1 compilato come asset statico; nessun server Node in produzione.
- PostgreSQL 16.10 Bookworm e Neo4j 5.26.12 Community fissati per digest.

Il build Python usa `uv sync --frozen --no-dev --no-editable`; il frontend usa `npm ci --ignore-scripts`. I tool dev non sono intenzionalmente inclusi nel runtime Python.

## Componenti non presenti

Graphiti, SDK/connettore Gmail, Immich, OmniRoute, MCP e connector NAS non sono dipendenze del codice corrente. Versione, compatibilità e licenze sono input futuri e non devono essere descritti come approvati o testati. Il prossimo passo previsto è il contratto/connettore Fase 2 per Gmail `SENT`, inizialmente senza accesso a un servizio reale.

## Decisione sincronizzatore Gmail — 11 ottobre 2026

Il sottocompito B ha stabilito che il sincronizzatore Fase 2 utilizzerà il **client ufficiale Gmail** (`google-api-python-client` + `google-auth` + `google-auth-oauthlib`), non un MCP di terze parti. Vedere [`docs/adr/003-sincronizzatore-gmail-fase2.md`](adr/003-sincronizzatore-gmail-fase2.md).

Le dipendenze future da aggiungere a `pyproject.toml` al momento dell'implementazione:

| Pacchetto | Versione | Stato |
|---|---|---|
| `google-api-python-client` | da pinnare | non ancora nel lockfile; licenza Apache-2.0 verificata dalla documentazione |
| `google-auth` | da pinnare | non ancora nel lockfile; licenza Apache-2.0 verificata dalla documentazione |
| `google-auth-oauthlib` | da pinnare | non ancora nel lockfile; licenza Apache-2.0 verificata dalla documentazione |

I due MCP candidati (`jimsimoy/gmail-mcp`, `klodr/gmail-mcp`) non sono dipendenze e non vanno aggiunti. La loro ispezione del codice sorgente non è stata possibile in questo sottocompito.

## Verifica e aggiornamento

Per ogni aggiornamento:

1. revisionare changelog, advisory e requisiti runtime;
2. verificare licenze/notice da fonti primarie, non soltanto metadata di registry;
3. rigenerare lockfile con tool/versione controllati;
4. eseguire lint, type-check, test e build deterministici;
5. verificare Compose e costruire l'immagine su daemon disponibile;
6. per PostgreSQL/Neo4j/Alembic, creare backup e completare prova restore isolata;
7. produrre SBOM/scansione dell'artefatto reale e registrare digest.

Le licenze indicate nell'inventario distinguono espressamente metadata locale e verifica primaria pendente; non sono un parere legale.

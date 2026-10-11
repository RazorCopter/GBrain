# Inventario componenti, versioni e licenze — Fase 1

## Metodo e significato dello stato

Versioni ricavate dai file presenti nel repository: [`Dockerfile`](../Dockerfile), [`compose.yaml`](../compose.yaml), [`pyproject.toml`](../pyproject.toml), [`uv.lock`](../uv.lock), [`frontend/package.json`](../frontend/package.json) e [`frontend/package-lock.json`](../frontend/package-lock.json). “Confermata nel lockfile” significa che il metadata locale dichiara una licenza; non equivale a consulenza legale né a verifica del testo/licenza primaria upstream. “Verifica primaria pendente” richiede confronto con repository/distribuzione ufficiale, notice e obblighi della versione effettivamente distribuita.

L'inventario transitive completo resta nei lockfile; la tabella evidenzia componenti diretti e immagini. Prima di una release/deployment va prodotto un SBOM dell'artefatto reale e svolta una revisione licenze/vulnerabilità, perché lockfile e immagini includono dipendenze transitive e pacchetti OS.

## Immagini e toolchain

| Componente | Versione/pin repository | Ruolo | Licenza/stato |
|---|---|---|---|
| GBrain | `0.1.0` | applicazione locale | nessun file `LICENSE` nel repository: titolarità/termini **da definire** prima della distribuzione |
| Node base | `24.12.0-bookworm-slim` + digest SHA-256 | solo build frontend | licenze Node + Debian/pacchetti immagine: verifica primaria/SBOM immagine pendente |
| Python base/runtime | `3.12.12-slim-bookworm` + digest | build/runtime app | PSF per Python; Debian/pacchetti immagine: verifica primaria/SBOM pendente |
| `uv` | `0.12.19` | resolver/install nel builder | licenza primaria pendente; versione fissata nel Dockerfile |
| PostgreSQL | `16.10-bookworm` + digest | database autorevole | PostgreSQL License comunemente dichiarata; verifica primaria di immagine e pacchetti pendente |
| Neo4j Community | `5.26.12-community` + digest | proiezione grafo | licenza Community/versione e termini container da verificare su fonte primaria; non indicata come “approvata” senza tale verifica |
| Docker/Compose | versione host non fissata | runtime/orchestrazione | input operativo esterno; versione e licenza della distribuzione effettiva da registrare |

Non vengono usati tag `latest`. Il tag applicativo Compose è parametrico, default `0.1.0`; il suo digest non è fissato nel repository e va registrato nel deployment/manifest backup.

## Dipendenze Python dirette

Versioni bloccate da [`pyproject.toml`](../pyproject.toml) e [`uv.lock`](../uv.lock):

| Pacchetto | Versione | Uso | Licenza |
|---|---:|---|---|
| Alembic | 1.20.0 | migrazioni | verifica primaria pendente |
| argon2-cffi | 25.1.0 | hashing password | verifica primaria pendente |
| FastAPI | 0.143.0 | API | MIT nota dal progetto; verifica primaria versione/notice pendente |
| google-api-python-client | 2.201.0 | client ufficiale Gmail API | metadata installato: Apache 2.0; verifica primaria testo/notice pendente |
| google-auth | 2.61.0 | credenziali e autenticazione Google | metadata installato: Apache 2.0; verifica primaria testo/notice pendente |
| google-auth-oauthlib | 1.5.0 | OAuth Web Google server-side | metadata installato: Apache 2.0; verifica primaria testo/notice pendente |
| cryptography | 50.0.2 (transitiva, lockfile) | AES-GCM per token OAuth cifrati/versionati | metadata/licenza primaria e notice da verificare nel gate release |
| driver Neo4j Python | 6.4.0 | health/proiezione futura | verifica primaria pendente |
| Procrastinate | 3.10.0 | coda PostgreSQL | MIT nota dal progetto; verifica primaria versione/notice pendente |
| psycopg `[binary,pool]` | 3.3.6 | PostgreSQL sync/async/pool | verifica primaria e componenti binary pendente |
| Pydantic | 2.14.0 | modelli/config | MIT nota dal progetto; verifica primaria pendente |
| pydantic-settings | 2.15.0 | configurazione | MIT nota dal progetto; verifica primaria pendente |
| python-multipart | 0.0.32 | parsing form | verifica primaria pendente |
| SQLAlchemy `[asyncio]` | 2.1.4 | ORM | MIT nota dal progetto; verifica primaria pendente |
| Uvicorn `[standard]` | 0.54.0 | ASGI server | BSD-3-Clause nota dal progetto; extra/transitive e verifica primaria pendenti |

Dipendenza transitiva rilevante: Starlette `1.7.0` nel lockfile. Dev: httpx `0.28.1`, mypy `2.4.0`, pytest `9.1.1`, pytest-asyncio `1.4.0`, Ruff `0.17.0`; licenze primarie e transitivi da verificare prima di redistribuire tool/artefatti che li includano. Il runtime viene costruito con `uv sync --frozen --no-dev`, quindi il gruppo dev non è intenzionalmente incluso nell'immagine runtime. Le tre licenze Google sopra sono state lette dai metadata della distribuzione installata l'11 ottobre 2026, non da una revisione legale o dalla fonte primaria upstream.

Il lockfile registra hash/sorgenti PyPI, ma non campi licenza affidabili per ogni pacchetto. Non si dichiara quindi una conferma primaria sulla sola base di [`uv.lock`](../uv.lock).

## Frontend diretto

Il lockfile npm locale contiene i seguenti metadata:

| Pacchetto | Versione | Scope | Licenza dichiarata nel lockfile | Stato |
|---|---:|---|---|---|
| React | 18.3.1 | runtime | MIT | confermata nel lockfile; primaria pendente |
| React DOM | 18.3.1 | runtime | MIT | confermata nel lockfile; primaria pendente |
| `@types/react` | 18.3.27 | dev/build | MIT | confermata nel lockfile; primaria pendente |
| `@types/react-dom` | 18.3.7 | dev/build | MIT | confermata nel lockfile; primaria pendente |
| `@vitejs/plugin-react` | 6.1.2 | dev/build | MIT | confermata nel lockfile; primaria pendente |
| TypeScript | 7.0.2 | dev/build | Apache-2.0 | confermata nel lockfile; primaria pendente |
| Vite | 8.3.4 | dev/build | MIT | confermata nel lockfile; primaria pendente |

React e React DOM sono compilati nel bundle distribuito; Vite/TypeScript/plugin e tipi sono tool di build. Le dipendenze transitive, inclusi binding opzionali per piattaforma, sono elencate in [`frontend/package-lock.json`](../frontend/package-lock.json) con metadata licenza ove fornito. Prima della distribuzione generare notice/SBOM dal lockfile e dall'immagine effettiva; non assumere che ogni metadata npm sia completo o una verifica legale.

## Servizi futuri/non presenti

| Componente | Stato codice | Versione/licenza |
|---|---|---|
| Gmail API / connettore `SENT` | implementato senza collaudo account reale; client ufficiale `google-api-python-client` + `google-auth` — ADR [`003`](../docs/adr/003-sincronizzatore-gmail-fase2.md) | librerie pinnate; metadata installato Apache 2.0, verifica primaria/termini servizio pendente |
| MCP `jimsimoy/gmail-mcp` | **NON adottato**; ispezione codice sorgente non eseguita | licenza, versione e dipendenze SCONOSCIUTE — non verificate da fonte primaria |
| MCP `klodr/gmail-mcp` | **NON adottato**; ispezione codice sorgente non eseguita | licenza, versione e dipendenze SCONOSCIUTE — non verificate da fonte primaria |
| Immich | non integrato | URL/versione/API/licenza dell'istanza sono input futuri, non noti |
| OmniRoute | non integrato | versione/protocollo/licenza/termini non noti |
| Graphiti | non dipendenza del lockfile, non implementato | compatibilità e licenza primaria pendenti |
| NAS/document connector | non implementato | tecnologia mount/protocollo/versione non noti |

Non presentare tali componenti come installati, testati o approvati.

## Checklist di rilascio

1. confrontare lockfile e manifest dell'immagine realmente costruita;
2. generare SBOM Python/npm/OS/container e conservarlo con la release;
3. eseguire scansione vulnerabilità con database aggiornato, registrando falsi positivi/eccezioni;
4. verificare su fonti primarie licenze, notice, redistribuzione e compatibilità per tutte le dipendenze runtime/transitive;
5. chiarire la licenza del repository GBrain;
6. verificare licenza/termini della precisa edizione Neo4j Community e delle immagini ufficiali;
7. ripetere inventario a ogni aggiornamento lockfile/digest;
8. aggiungere separatamente versioni/licenze/termini di Gmail, Immich, OmniRoute o Graphiti soltanto quando diventano dipendenze effettive.

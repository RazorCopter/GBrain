# Piano 005 — Paginazione Immich a blocchi + Pannello Configurazione

> **Stato**: da implementare  
> **Prerequisito completato**: connettore Immich v3.3.3 funzionante con test-connection, preview, sync singolo blocco ≤25, cancel, consultazione items.  
> **Obiettivo**: import massivo dell'intera libreria Immich (o di un periodo) a blocchi di 25 con checkpoint, e pannello di configurazione separato dalla wizard di onboarding.

---

## 1  Decisioni architetturali

### 1.1  Paginazione cursor-based (v3 search)

L'API Immich v3.3.3 `POST /api/search/metadata` restituisce nella risposta:

```json
{
  "assets": {
    "items": [...],
    "nextCursor": "uuid-del-prossimo-asset"
  }
}
```

Il campo `nextCursor` è `null | undefined` quando non ci sono ulteriori risultati. Per iterare si aggiunge `"cursor": "<nextCursor>"` nel payload della request successiva. **Il campo `page` non esiste nel contratto v3**; la paginazione è esclusivamente cursor-based.

Per gli album (`GET /albums/{id}`), **non esiste paginazione nativa**: la risposta include tutti gli asset inline. La suddivisione è solo client-side (già implementata con slicing `[:limit]`).

**Decisione**: il loop di paginazione avviene **nel job** (`execute_immich_job`), non nel gateway. Il gateway espone un metodo `search_period_assets` che accetta un `cursor` opzionale e restituisce `(assets, next_cursor)`. Il job itera fino a esaurimento cursor o raggiungimento del budget di request.

### 1.2  Checkpoint incrementale

Ogni iterazione di blocco da 25 produce un checkpoint atomico nel campo `ImportJob.checkpoint` (JSONB):

```python
{
    "phase": "paginating",          # "queued" | "paginating" | "completed" | "failed"
    "cursor": "<next_cursor>",      # null se terminato
    "pages_completed": 3,
    "total_enumerated": 75,
    "total_inserted": 60,
    "total_duplicate": 15,
    "last_batch_at": "2026-10-15T14:00:00Z"
}
```

In caso di crash/retry, il job **riprende dal cursor salvato** senza ri-processare i blocchi precedenti. Questo sfrutta il meccanismo di retry di Procrastinate già in uso.

### 1.3  Budget di request e limiti

Lo schema `SourceSyncRequest` ha già i campi `item_limit` (max asset totali) e `request_budget` (max request API). La semantica cambia:

| Campo | Significato attuale | Significato nuovo |
|---|---|---|
| `item_limit` | Tetto hard a 25 | Tetto totale asset da importare (default 5000, max 50000) |
| `request_budget` | Non usato | Max pagine API (default 200 = 5000 asset, safety valve) |
| `batch_size` | Non usato | Fissato a 25 (costante Immich, non configurabile dall'utente) |

Il job si ferma quando: `next_cursor` è null, oppure `total_enumerated >= item_limit`, oppure `pages_completed >= request_budget`, oppure viene ricevuta cancellazione.

### 1.4  Selezione "tutti" (nessun filtro periodo)

Attualmente `ImmichSelection.__post_init__` richiede `album_id` XOR `(after, before)` con almeno uno dei due. Per supportare "importa tutto" senza filtro:

**Decisione**: aggiungere un campo `import_all: bool = False` a `ImmichSelection`. Se `import_all=True`, `after` e `before` possono essere entrambi `None` e `album_id` deve essere `None`. Il gateway invia il payload di search senza filtro `takenAt`.

### 1.5  Separazione onboarding vs configurazione

La wizard (`SourceConnectionWizard`) resta il flusso di prima connessione (step 1→6: selezione→credenziali→auth→test→preview→sync). Il **pannello di configurazione** è una vista separata accessibile **dopo** che l'account è `ACTIVE`, e permette di:

- Selezionare un range date o "tutti"
- Avviare un import massivo
- Visualizzare il progresso in tempo reale (polling checkpoint)
- Cancellare un import in corso
- Consultare gli asset già importati

**Decisione**: il pannello è un nuovo componente React `ImmichConfigPanel` renderizzato dentro `SourcesPage` quando l'account Immich è attivo, al posto del bottone "Consulta metadati" attuale.

---

## 2  Contratti modificati

### 2.1  Gateway — nuovo signature `search_period_assets`

In [`immich_gateway.py`](backend/src/gbrain/infrastructure/immich_gateway.py:468):

```python
async def search_period_assets(
    self,
    after: datetime | None,
    before: datetime | None,
    limit: int = IMMICH_MAX_ITEMS_LIMIT,
    cursor: str | None = None,          # ← NUOVO
) -> tuple[list[ImmichNormalizedAsset], str | None]:  # ← ritorna anche next_cursor
```

Il payload v3 diventa:

```python
payload = {
    "size": bounded_limit,
    "withExif": True,
    "withPeople": True,
    "orderBy": {"field": "localDateTime", "direction": "asc"},  # asc per scan cronologico
}
if taken_at:
    payload["filter"] = {"takenAt": taken_at}
if cursor:
    payload["cursor"] = cursor
```

La risposta viene parsata per estrarre `next_cursor = assets_container.get("nextCursor")`.

**Breaking change**: il tipo di ritorno cambia da `list[...]` a `tuple[list[...], str | None]`. Aggiornare tutti i call-site (job, preview, test).

### 2.2  Dominio — `ImmichSelection` aggiornato

In [`immich.py`](backend/src/gbrain/domain/immich.py:73):

```python
@dataclass(frozen=True, slots=True)
class ImmichSelection:
    album_id: str | None = None
    after: datetime | None = None
    before: datetime | None = None
    item_limit: int = 5000                    # ← era IMMICH_MAX_ITEMS_LIMIT (25)
    import_all: bool = False                  # ← NUOVO

    def __post_init__(self) -> None:
        if self.item_limit < 1 or self.item_limit > 50000:
            raise ValueError(...)
        if self.import_all:
            if self.album_id or self.after or self.before:
                raise ValueError("import_all è incompatibile con filtri")
            return
        # ... validazione esistente XOR album/periodo ...
```

### 2.3  Schema API — `SourceSyncRequest` aggiornato

In [`schemas.py`](backend/src/gbrain/presentation/schemas.py:551):

```python
class SourceSyncRequest(BaseModel):
    album_id: str | None = None
    period_after: datetime | None = None
    period_before: datetime | None = None
    import_all: bool = False                           # ← NUOVO
    item_limit: int = Field(default=5000, ge=1, le=50000)  # ← era 500/5000
    request_budget: int = Field(default=200, ge=1, le=2000)
    batch_size: int = Field(default=25, ge=25, le=25)  # ← fisso a 25
    concurrency: int = Field(default=1, ge=1, le=1)
    mode: Literal["full", "incremental", "reconciliation"] = "full"
```

### 2.4  Nuovo endpoint progress (o estensione status)

Opzione scelta: **estendere `GET /{source_id}/status`** per includere `active_job_checkpoint` quando c'è un job running/pending per Immich. Nessun endpoint nuovo.

Il campo `checkpoint` del job contiene già i dati di progresso. Lo schema `SourceStatusResponse` in [`schemas.py`](backend/src/gbrain/presentation/schemas.py:462) viene esteso:

```python
class SourceStatusResponse(BaseModel):
    # ... campi esistenti ...
    active_job: ImportJobResponse | None = None        # ← NUOVO: job attivo con checkpoint
```

### 2.5  Contratti TypeScript

In [`api.ts`](frontend/src/api.ts):

```typescript
export interface SourceSyncPayload {
    album_id?: string
    period_after?: string
    period_before?: string
    import_all?: boolean          // ← NUOVO
    item_limit?: number           // default 5000
    request_budget?: number
    mode?: 'full' | 'incremental' | 'reconciliation'
}

export interface ImportJobCheckpoint {
    phase: string
    cursor?: string | null
    pages_completed?: number
    total_enumerated?: number
    total_inserted?: number
    total_duplicate?: number
    last_batch_at?: string
}
```

---

## 3  Componenti coinvolti e ordine delle modifiche

### Fase A — Backend paginazione (stimato: 1 sessione Code)

1. **`immich.py`** — Aggiornare `ImmichSelection` con `import_all` e nuovi limiti `item_limit`
2. **`immich_gateway.py`** — Aggiungere parametro `cursor` a `search_period_assets`, ritornare `(assets, next_cursor)`, cambiare `orderBy` a `asc`
3. **`immich_jobs.py`** — Riscrivere `execute_immich_job` con loop paginato:
   - Caricare checkpoint → riprendere da `cursor` se presente
   - Loop: fetch blocco → persist batch → aggiornare checkpoint atomicamente
   - Condizioni di stop: cursor null / item_limit / request_budget / cancellazione
   - Gestione errori transitori: salvare cursor nel checkpoint e lasciare retry a Procrastinate
4. **`schemas.py`** — Aggiornare `SourceSyncRequest` e `SourceStatusResponse`
5. **`sources_api.py`** — Aggiornare `trigger_source_sync` per passare `import_all` e nuovi limiti nel `policy_snapshot`; aggiornare `get_source_status` per includere `active_job`

### Fase B — Test paginazione (stimato: stessa sessione Code)

6. **`test_immich_gateway.py`** — Test cursor pagination: mock che restituisce `nextCursor` per 3 pagine poi null; test boundary nessun cursor; test import_all senza filtri
7. **`test_immich_vertical.py`** — Test job paginato end-to-end: mock gateway che restituisce 3 pagine, verificare checkpoint intermedi e conteggi finali; test resume da checkpoint; test cancellazione mid-pagination

### Fase C — Frontend pannello configurazione (stimato: 1 sessione Code)

8. **`api.ts`** — Aggiornare `SourceSyncPayload`, aggiungere `ImportJobCheckpoint`
9. **Nuovo file `ImmichConfigPanel.tsx`** — Componente con:
   - Radio: "Tutto" / "Periodo" (date picker after/before)
   - Bottone "Avvia importazione massiva"
   - Barra progresso: polling `GET /sources/immich/status` ogni 3s durante job attivo
   - Display: `total_enumerated` / `total_inserted` / `total_duplicate` / `pages_completed`
   - Bottone "Annulla importazione" durante job attivo
   - Tabella asset importati (riuso consultazione esistente)
10. **`SourcesPage.tsx`** — Sostituire il blocco Immich (bottone "Consulta metadati") con `<ImmichConfigPanel>` quando account attivo
11. **`SourceConnectionWizard.tsx`** — Rimuovere/semplificare step 5-6 per Immich: la wizard fa solo connessione (step 1→4), poi redirect al pannello config

### Fase D — Gate e collaudo (stimato: verifica rapida)

12. pytest mirati, ruff, mypy, frontend build
13. Collaudo manuale su istanza Immich reale (library >25 asset)

---

## 4  Criteri di accettazione

| # | Criterio | Verifica |
|---|----------|----------|
| AC-1 | Import di una libreria con >25 asset produce correttamente tutti i metadati nel DB | Query `SELECT count(*) FROM source_items WHERE source_kind='immich'` ≥ totale libreria |
| AC-2 | Il checkpoint JSONB si aggiorna dopo ogni blocco di 25 | Ispezionare `import_jobs.checkpoint` durante esecuzione: `pages_completed` incrementa |
| AC-3 | Un crash/restart del worker riprende dal cursor salvato senza duplicati | Kill worker mid-import → restart → conteggio finale = atteso, duplicates = 0 per blocchi già salvati |
| AC-4 | `item_limit` limita effettivamente il numero totale di asset importati | Impostare `item_limit=50` su libreria >50 → esattamente ≤50 asset importati |
| AC-5 | `request_budget` limita il numero di request API | Impostare `request_budget=2` → max 50 asset (2×25) |
| AC-6 | Cancellazione durante paginazione ferma il job al blocco corrente | Inviare cancel → job si ferma, checkpoint scrive `phase=cancelled`, asset già importati restano |
| AC-7 | Il pannello configurazione Immich è accessibile solo con account attivo, separato dalla wizard | Navigazione: Sources → card Immich → "Configura" apre pannello; wizard accessibile da "Connetti" |
| AC-8 | Il pannello mostra progresso in tempo reale durante import | Barra progresso si aggiorna ogni ~3s con contatori corretti |
| AC-9 | L'opzione "Importa tutto" funziona senza specificare date | `import_all=True` → nessun filtro `takenAt` → scan completo della libreria |
| AC-10 | Tutti i gate passano: pytest ≥ 45 pass, ruff, mypy 0 errori, frontend build ok | Exit code 0 per ogni gate |

---

## 5  Rischi e mitigazioni

| Rischio | Mitigazione |
|---------|-------------|
| Librerie enormi (>100k asset): job molto lungo | `request_budget` come safety valve; il checkpoint consente di completare in esecuzioni successive |
| Rate limiting Immich: l'API non documenta rate limits espliciti | Intervallo `asyncio.sleep(0.2)` tra blocchi; backoff su 429 (se mai restituito) |
| `nextCursor` cambia semantica tra versioni Immich | Il gateway è già version-gated a `3.3.3` esatto; per versioni legacy (1.106–1.140) mantenere il percorso `page`-based esistente |
| Album con migliaia di asset (nessuna paginazione nativa) | Per album, il gateway scarica tutti gli asset e li suddivide client-side in batch da 25 per la persistenza; non è un problema di memory per album ragionevoli (<10k asset) |

---

## 6  Decisioni escluse da questo incremento

- Analisi visiva LLM/machine-learning sugli asset
- Sync incrementale basato su `updatedAt` (richiede indice lato Immich)
- Multi-account Immich (un solo account per owner)
- Filtro per tipo asset (IMAGE/VIDEO) nel pannello

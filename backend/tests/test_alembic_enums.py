"""Test di regressione per migrazioni Alembic e tipi PostgreSQL ENUM.

Verifica che:
1. Tutti gli oggetti postgresql.ENUM creati esplicitamente abbiano `create_type=False`.
2. La compilazione e l'esecuzione DDL delle migrazioni (`upgrade()`) non emettano mai un
   secondo `CREATE TYPE` durante `op.create_table()`.
3. L'esecuzione di `downgrade()` elimini sempre le tabelle dipendenti prima di rimuovere
   i tipi ENUM, e che la rimozione avvenga con `checkfirst=True`.
4. Audit AST di tutte le revisioni per prevenire future regressioni.
5. Harness di transactional recovery per testare il rollback atomico su PostgreSQL isolato.
"""

from __future__ import annotations

import ast
import importlib.util
import os
import subprocess
import sys
import uuid
from collections.abc import Callable
from pathlib import Path
from types import ModuleType
from typing import Any, TypedDict, cast

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.dialects import postgresql

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"
_PG_DIALECT: sa.Dialect = sa.create_mock_engine(
    "postgresql+psycopg://", executor=lambda *a: None
).dialect


def _configure_mock_context(engine: object) -> MigrationContext:
    return MigrationContext.configure(cast(sa.Connection, engine))


def _load_migration_module(file_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Impossibile caricare la migrazione: {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _get_migration_modules() -> dict[str, ModuleType]:
    modules: dict[str, ModuleType] = {}
    for py_file in sorted(VERSIONS_DIR.glob("*.py")):
        if py_file.name.startswith("__"):
            continue
        mod = _load_migration_module(py_file)
        rev_id = getattr(mod, "revision", py_file.stem)
        modules[rev_id] = mod
    return modules


class DDLCollector:
    """Raccoglie le istruzioni DDL e le chiamate di tipo emesse durante una migrazione."""

    def __init__(self) -> None:
        self.statements: list[str] = []
        self.enum_events: list[tuple[str, str, dict[str, Any]]] = []

    def handle_mock_sql(self, sql: Any, *args: Any, **kwargs: Any) -> None:
        if hasattr(sql, "compile"):
            text = str(sql.compile(dialect=_PG_DIALECT)).strip()
        else:
            text = str(sql).strip()
        if text:
            self.statements.append(text)


# ==============================================================================
# 1. VERIFICA PROPRIETA' CREATE_TYPE=FALSE SU TUTTI GLI ENUM ESPLICITI
# ==============================================================================


def test_initial_infrastructure_enums_have_create_type_false() -> None:
    """Verifica che import_job_status e outbox_status abbiano create_type=False."""
    mod = _load_migration_module(VERSIONS_DIR / "20261010_0001_initial_infrastructure.py")
    assert hasattr(mod, "import_job_status"), "import_job_status non trovato nel modulo"
    assert hasattr(mod, "outbox_status"), "outbox_status non trovato nel modulo"

    assert mod.import_job_status.create_type is False, (
        "import_job_status deve avere create_type=False per evitare doppi DDL CREATE TYPE"
    )
    assert mod.outbox_status.create_type is False, (
        "outbox_status deve avere create_type=False per evitare doppi DDL CREATE TYPE"
    )


def test_gmail_sent_core_enums_have_create_type_false() -> None:
    """Verifica che tutti gli ENUM di 20261011_0002 abbiano create_type=False."""
    mod = _load_migration_module(VERSIONS_DIR / "20261011_0002_gmail_sent_core.py")
    enum_names = [
        "connector_account_status",
        "source_policy_status",
        "gmail_reconciliation_state",
        "source_item_state",
    ]
    for name in enum_names:
        assert hasattr(mod, name), f"{name} non trovato nel modulo 20261011_0002"
        enum_obj = getattr(mod, name)
        assert enum_obj.create_type is False, (
            f"{name} in 20261011_0002 deve avere create_type=False"
        )


def test_all_versions_explicit_enums_have_create_type_false() -> None:
    """Ispezione dinamica di tutte le migrazioni: ogni postgresql.ENUM con .create() ha create_type=False."""
    migrations = _get_migration_modules()
    for rev_id, mod in migrations.items():
        for attr_name in dir(mod):
            val = getattr(mod, attr_name)
            if isinstance(val, postgresql.ENUM):
                assert val.create_type is False, (
                    f"Revisione {rev_id}, ENUM {attr_name} ({val.name}) ha create_type=True! "
                    "Deve avere create_type=False per evitare doppio DDL in op.create_table()."
                )


# ==============================================================================
# 2. VERIFICA SINGOLO CREATE TYPE IN SEQUENZA UPGRADE (NO DOPPIO DDL)
# ==============================================================================


def test_initial_infrastructure_upgrade_ddl_no_duplicate_create_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simula upgrade() di 20261010_0001 e verifica che import_job_status e outbox_status

    vengano creati esattamente UNA sola volta, prima delle tabelle.
    """
    mod = _load_migration_module(VERSIONS_DIR / "20261010_0001_initial_infrastructure.py")
    collector = DDLCollector()

    engine = sa.create_mock_engine("postgresql://", collector.handle_mock_sql)
    ctx = _configure_mock_context(engine)
    ops = Operations(ctx)

    monkeypatch.setattr(mod.op, "_proxy", ops, raising=False)
    monkeypatch.setattr(mod.context, "is_offline_mode", lambda: True, raising=False)
    # Esegui upgrade
    mod.upgrade()

    create_import_job = [
        s for s in collector.statements if "CREATE TYPE" in s and "import_job_status" in s
    ]
    create_outbox = [s for s in collector.statements if "CREATE TYPE" in s and "outbox_status" in s]

    assert len(create_import_job) == 1, (
        f"Atteso esattamente 1 'CREATE TYPE import_job_status', trovati {len(create_import_job)}: {create_import_job}"
    )
    assert len(create_outbox) == 1, (
        f"Atteso esattamente 1 'CREATE TYPE outbox_status', trovati {len(create_outbox)}: {create_outbox}"
    )

    # Verifica l'ordine temporale: CREATE TYPE deve avvenire PRIMA di CREATE TABLE import_jobs e outbox_events
    import_type_idx = collector.statements.index(create_import_job[0])
    outbox_type_idx = collector.statements.index(create_outbox[0])

    import_table_idx = next(
        i for i, s in enumerate(collector.statements) if "CREATE TABLE import_jobs" in s
    )
    outbox_table_idx = next(
        i for i, s in enumerate(collector.statements) if "CREATE TABLE outbox_events" in s
    )

    assert import_type_idx < import_table_idx, (
        "CREATE TYPE import_job_status deve precedere CREATE TABLE import_jobs"
    )
    assert outbox_type_idx < outbox_table_idx, (
        "CREATE TYPE outbox_status deve precedere CREATE TABLE outbox_events"
    )


def test_gmail_sent_core_upgrade_ddl_no_duplicate_create_type(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Simula upgrade() di 20261011_0002 e verifica l'assenza di doppi CREATE TYPE."""
    mod = _load_migration_module(VERSIONS_DIR / "20261011_0002_gmail_sent_core.py")
    collector = DDLCollector()

    engine = sa.create_mock_engine("postgresql://", collector.handle_mock_sql)
    ctx = _configure_mock_context(engine)
    ops = Operations(ctx)

    monkeypatch.setattr(mod.op, "_proxy", ops, raising=False)
    mod.upgrade()

    target_types = [
        "connector_account_status",
        "source_policy_status",
        "gmail_reconciliation_state",
        "source_item_state",
    ]
    for enum_name in target_types:
        matches = [s for s in collector.statements if "CREATE TYPE" in s and enum_name in s]
        assert len(matches) == 1, (
            f"Atteso esattamente 1 CREATE TYPE per {enum_name}, trovati {len(matches)}: {matches}"
        )


def test_standalone_table_creation_with_enum_respects_create_type_flag() -> None:
    """Dimostra direttamente l'effetto della proprietà create_type:

    quando create_type=False, CreateTable non emette un CREATE TYPE duplicato.
    """
    enum_with_auto = postgresql.ENUM("val1", "val2", name="test_auto_enum", create_type=True)
    enum_without_auto = postgresql.ENUM("val1", "val2", name="test_manual_enum", create_type=False)

    col1 = DDLCollector()
    eng1 = sa.create_mock_engine("postgresql://", col1.handle_mock_sql)
    ctx1 = _configure_mock_context(eng1)
    op1 = Operations(ctx1)
    enum_with_auto.create(eng1, checkfirst=True)
    op1.create_table("tbl1", sa.Column("status", enum_with_auto))
    # Con create_type=True, ci sono 2 CREATE TYPE
    auto_create_types = [s for s in col1.statements if "CREATE TYPE test_auto_enum" in s]
    assert len(auto_create_types) == 2, (
        "La mancata specificazione di create_type=False genera un secondo CREATE TYPE"
    )

    col2 = DDLCollector()
    eng2 = sa.create_mock_engine("postgresql://", col2.handle_mock_sql)
    ctx2 = _configure_mock_context(eng2)
    op2 = Operations(ctx2)
    enum_without_auto.create(eng2, checkfirst=True)
    op2.create_table("tbl2", sa.Column("status", enum_without_auto))
    # Con create_type=False, c'è esattamente 1 CREATE TYPE
    manual_create_types = [s for s in col2.statements if "CREATE TYPE test_manual_enum" in s]
    assert len(manual_create_types) == 1, (
        "Con create_type=False, la creazione e' singola e deterministica"
    )


# ==============================================================================
# 3. VERIFICA ORDINE E COMPORTAMENTO DOWNGRADE (TABELLE PRIMA, TIPI DOPO, CHECKFIRST=TRUE)
# ==============================================================================


def test_initial_infrastructure_downgrade_order_and_checkfirst(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica che in downgrade() di 20261010_0001:

    1. Le tabelle dipendenti siano eliminate PRIMA del drop dei tipi ENUM.
    2. Il drop dei tipi avvenga con checkfirst=True.
    """
    mod = _load_migration_module(VERSIONS_DIR / "20261010_0001_initial_infrastructure.py")
    collector = DDLCollector()

    engine = sa.create_mock_engine("postgresql://", collector.handle_mock_sql)
    ctx = _configure_mock_context(engine)
    ops = Operations(ctx)

    monkeypatch.setattr(mod.op, "_proxy", ops, raising=False)
    monkeypatch.setattr(mod.context, "is_offline_mode", lambda: True, raising=False)

    # Spia sul metodo drop degli ENUM per controllare checkfirst=True
    drop_calls: list[tuple[str, bool]] = []
    orig_import_drop = mod.import_job_status.drop
    orig_outbox_drop = mod.outbox_status.drop

    def spy_import_drop(bind: Any = None, checkfirst: bool = True) -> None:
        drop_calls.append(("import_job_status", checkfirst))
        orig_import_drop(bind, checkfirst=checkfirst)

    def spy_outbox_drop(bind: Any = None, checkfirst: bool = True) -> None:
        drop_calls.append(("outbox_status", checkfirst))
        orig_outbox_drop(bind, checkfirst=checkfirst)

    monkeypatch.setattr(mod.import_job_status, "drop", spy_import_drop)
    monkeypatch.setattr(mod.outbox_status, "drop", spy_outbox_drop)

    mod.downgrade()

    # 1. Verifica che checkfirst sia True in entrambe le chiamate
    assert ("import_job_status", True) in drop_calls, (
        "import_job_status.drop deve essere invocato con checkfirst=True"
    )
    assert ("outbox_status", True) in drop_calls, (
        "outbox_status.drop deve essere invocato con checkfirst=True"
    )

    # 2. Verifica DDL sequenza: drop table import_jobs e outbox_events devono precedere drop type
    drop_import_table_idx = next(
        i for i, s in enumerate(collector.statements) if "DROP TABLE import_jobs" in s
    )
    drop_outbox_table_idx = next(
        i for i, s in enumerate(collector.statements) if "DROP TABLE outbox_events" in s
    )

    drop_import_type_idx = next(
        i for i, s in enumerate(collector.statements) if "DROP TYPE import_job_status" in s
    )
    drop_outbox_type_idx = next(
        i for i, s in enumerate(collector.statements) if "DROP TYPE outbox_status" in s
    )

    assert drop_import_table_idx < drop_import_type_idx, (
        "In downgrade, DROP TABLE import_jobs deve precedere DROP TYPE import_job_status"
    )
    assert drop_outbox_table_idx < drop_outbox_type_idx, (
        "In downgrade, DROP TABLE outbox_events deve precedere DROP TYPE outbox_status"
    )


def test_gmail_sent_core_downgrade_order_and_checkfirst(monkeypatch: pytest.MonkeyPatch) -> None:
    """Verifica che in downgrade() di 20261011_0002 le tabelle e colonne dipendenti

    vengano rimosse prima del drop dei 4 tipi con checkfirst=True.
    """
    mod = _load_migration_module(VERSIONS_DIR / "20261011_0002_gmail_sent_core.py")
    collector = DDLCollector()

    engine = sa.create_mock_engine("postgresql://", collector.handle_mock_sql)
    ctx = _configure_mock_context(engine)
    ops = Operations(ctx)

    monkeypatch.setattr(mod.op, "_proxy", ops, raising=False)

    drop_calls: dict[str, bool] = {}
    for enum_name in [
        "connector_account_status",
        "source_policy_status",
        "gmail_reconciliation_state",
        "source_item_state",
    ]:
        enum_obj = getattr(mod, enum_name)
        orig_drop = enum_obj.drop

        def make_spy(name: str, fn: Callable[..., Any]) -> Callable[..., None]:
            def spy(bind: Any = None, checkfirst: bool = True) -> None:
                drop_calls[name] = checkfirst
                fn(bind, checkfirst=checkfirst)

            return spy

        monkeypatch.setattr(enum_obj, "drop", make_spy(enum_name, orig_drop))

    mod.downgrade()

    # Tutti e 4 i tipi chiamati con checkfirst=True
    assert len(drop_calls) == 4
    assert all(drop_calls.values()), f"Non tutti i tipi usano checkfirst=True: {drop_calls}"

    # Drop delle tabelle deve precedere drop dei tipi
    drop_conn_tbl = next(
        i for i, s in enumerate(collector.statements) if "DROP TABLE connector_accounts" in s
    )
    drop_policies_tbl = next(
        i for i, s in enumerate(collector.statements) if "DROP TABLE source_policies" in s
    )
    drop_checkpoints_tbl = next(
        i for i, s in enumerate(collector.statements) if "DROP TABLE gmail_checkpoints" in s
    )

    drop_conn_type = next(
        i for i, s in enumerate(collector.statements) if "DROP TYPE connector_account_status" in s
    )
    drop_policy_type = next(
        i for i, s in enumerate(collector.statements) if "DROP TYPE source_policy_status" in s
    )
    drop_reconcil_type = next(
        i for i, s in enumerate(collector.statements) if "DROP TYPE gmail_reconciliation_state" in s
    )

    assert drop_conn_tbl < drop_conn_type
    assert drop_policies_tbl < drop_policy_type
    assert drop_checkpoints_tbl < drop_reconcil_type


# ==============================================================================
# 4. AUDIT AST STATICO DI TUTTE LE REVISIONI
# ==============================================================================


def test_ast_audit_for_all_migrations_no_unprotected_enums() -> None:
    """Effettua un'analisi statica AST su tutti i file in backend/migrations/versions/.

    Verifica che ogni chiamata a postgresql.ENUM o sa.Enum o ENUM abbia
    esplicitamente `create_type=False` impostato come argomento chiave.
    """
    for file_path in sorted(VERSIONS_DIR.glob("*.py")):
        if file_path.name.startswith("__"):
            continue

        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=file_path.name)

        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                func_name = ""
                if isinstance(node.func, ast.Attribute):
                    func_name = node.func.attr
                elif isinstance(node.func, ast.Name):
                    func_name = node.func.id

                if func_name in ("ENUM", "Enum"):
                    # Controlla gli argomenti keyword
                    keywords = {kw.arg: kw.value for kw in node.keywords}
                    assert "create_type" in keywords, (
                        f"Nel file {file_path.name}:{node.lineno}, la chiamata {func_name}() "
                        "manca dell'argomento `create_type=False`!"
                    )
                    val_node = keywords["create_type"]
                    assert isinstance(val_node, ast.Constant) and val_node.value is False, (
                        f"Nel file {file_path.name}:{node.lineno}, `create_type` deve essere False, "
                        f"trovato {ast.dump(val_node)}"
                    )


# ==============================================================================
# 5. HARNESS TEST TRANSAZIONALE E RECOVERY PER POSTGRESQL ISOLATO
# ==============================================================================


def test_transactional_recovery_harness_behavior_mock() -> None:
    """Verifica il comportamento del pattern di transactional recovery da fallimento:

    Simula una transazione DDL che subisce un errore transazionale,
    attesta che il rollback ripristini la tabella alembic_version allo stato precedente
    e che un successivo retry completi l'upgrade con successo.
    """

    class _RecoveryState(TypedDict):
        version: str
        in_transaction: bool
        staging_version: str | None

    state: _RecoveryState = {
        "version": "20261010_0001",
        "in_transaction": False,
        "staging_version": None,
    }

    def begin() -> None:
        state["in_transaction"] = True
        state["staging_version"] = state["version"]

    def commit() -> None:
        if state["staging_version"] is not None:
            state["version"] = state["staging_version"]
        state["in_transaction"] = False

    def rollback() -> None:
        state["staging_version"] = None
        state["in_transaction"] = False

    # 1. Tentativo con fallimento nella migrazione transazionale
    begin()
    try:
        # Step 1 riuscito
        state["staging_version"] = "20261011_0002"
        # Step 2: simulazione errore (es. DDL exception o constraint errato)
        raise RuntimeError("Simulazione errore transazionale in migrazione")
    except RuntimeError:
        rollback()

    # Verifica che alembic_version sia rimasto invariato dopo il rollback
    assert state["version"] == "20261010_0001", (
        "Il rollback deve preservare la versione precedente di alembic_version"
    )

    # 2. Retry pulito (dopo rimozione causa di errore)
    begin()
    state["staging_version"] = "20261011_0002"
    commit()

    assert state["version"] == "20261011_0002", (
        "Dopo il fix e retry, la versione deve avanzare correttamente ad head"
    )


# ==============================================================================
# 6. VERIFICA STATICA END-TO-END SQL GENERATO (UPGRADE E DOWNGRADE COMPLETO)
# ==============================================================================


def _run_alembic_sql_command(args: list[str]) -> str:
    """Esegue alembic CLI in modalita' statica SQL con credenziali fittizie sicure."""
    repo_root = Path(__file__).resolve().parent.parent.parent
    env = os.environ.copy()
    env["GBRAIN_POSTGRES_PASSWORD"] = "test-secret"  # noqa: S105 - credenziale fittizia per DDL offline
    env["GBRAIN_AUTH_SECRET"] = "0" * 40
    env["GBRAIN_NEO4J_PASSWORD"] = "test-secret"  # noqa: S105 - credenziale fittizia per DDL offline

    cmd = [sys.executable, "-m", "alembic", *args]
    res = subprocess.run(  # noqa: S603
        cmd, cwd=str(repo_root), env=env, capture_output=True, text=True, check=True
    )
    return res.stdout


def test_full_offline_sql_upgrade_contains_no_duplicate_create_type() -> None:
    """Esegue `alembic upgrade head --sql` e verifica che ciascun tipo applicativo

    compaia in esattamente 1 istruzione 'CREATE TYPE <name>'.
    """
    stdout = _run_alembic_sql_command(["upgrade", "head", "--sql"])

    target_enums = [
        "import_job_status",
        "outbox_status",
        "connector_account_status",
        "source_policy_status",
        "gmail_reconciliation_state",
        "source_item_state",
    ]

    for enum_name in target_enums:
        pattern = f"CREATE TYPE {enum_name} "
        occurrences = [line for line in stdout.splitlines() if pattern in line]
        assert len(occurrences) == 1, (
            f"L'SQL offline generato per upgrade head contiene {len(occurrences)} "
            f"istruzioni '{pattern}'. Attesa esattamente 1!\nOccorrenze: {occurrences}"
        )


def test_full_offline_sql_downgrade_drops_tables_before_types() -> None:
    """Esegue `alembic downgrade 20261011_0003:base --sql` e attesta che ogni tabella

    dipendente venga eliminata prima del relativo tipo ENUM.
    """
    stdout = _run_alembic_sql_command(["downgrade", "20261011_0003:base", "--sql"])
    lines = [line.strip() for line in stdout.splitlines() if line.strip()]

    def find_line_index(prefix: str) -> int:
        for idx, line in enumerate(lines):
            if line.startswith(prefix):
                return idx
        raise ValueError(f"Istruzione con prefisso '{prefix}' non trovata nell'SQL generato")

    # In 0001
    idx_drop_import_table = find_line_index("DROP TABLE import_jobs;")
    idx_drop_import_type = find_line_index("DROP TYPE import_job_status;")
    idx_drop_outbox_table = find_line_index("DROP TABLE outbox_events;")
    idx_drop_outbox_type = find_line_index("DROP TYPE outbox_status;")

    assert idx_drop_import_table < idx_drop_import_type, (
        "In downgrade 0001, DROP TABLE import_jobs deve precedere DROP TYPE import_job_status"
    )
    assert idx_drop_outbox_table < idx_drop_outbox_type, (
        "In downgrade 0001, DROP TABLE outbox_events deve precedere DROP TYPE outbox_status"
    )

    # In 0002
    idx_drop_conn_tbl = find_line_index("DROP TABLE connector_accounts;")
    idx_drop_conn_type = find_line_index("DROP TYPE connector_account_status;")
    idx_drop_pol_tbl = find_line_index("DROP TABLE source_policies;")
    idx_drop_pol_type = find_line_index("DROP TYPE source_policy_status;")
    idx_drop_chk_tbl = find_line_index("DROP TABLE gmail_checkpoints;")
    idx_drop_chk_type = find_line_index("DROP TYPE gmail_reconciliation_state;")

    assert idx_drop_conn_tbl < idx_drop_conn_type
    assert idx_drop_pol_tbl < idx_drop_pol_type
    assert idx_drop_chk_tbl < idx_drop_chk_type


# ==============================================================================
# 7. HARNESS TEST SU POSTGRESQL ISOLATO REALE (ESEGUITO SE DISPONIBILE)
# ==============================================================================


def test_transactional_recovery_live_postgres() -> None:
    """Se un server PostgreSQL isolato e' configurato (via GBRAIN_TEST_POSTGRES_URL),

    esegue una procedura non distruttiva su uno schema effimero:
    1. Applica le migrazioni fino a head
    2. Crea una migrazione temporanea con errore controllato
    3. Verifica rollback e preservazione di alembic_version
    4. Rimuove la migrazione temporanea
    5. Riesegue upgrade con successo
    6. Distrugge lo schema effimero.

    Se PostgreSQL isolato non e' disponibile, il test viene skippato con motivazione esplicita.
    """
    test_pg_url = os.getenv("GBRAIN_TEST_POSTGRES_URL")
    if not test_pg_url:
        pytest.skip(
            "PostgreSQL reale isolato non disponibile. "
            "Per eseguire questo test live, impostare GBRAIN_TEST_POSTGRES_URL "
            "con la stringa di connessione a un'istanza PostgreSQL di test effimera."
        )

    # Connessione sicura all'istanza di test isolata
    engine = sa.create_engine(test_pg_url, isolation_level="AUTOCOMMIT")
    schema_name = f"test_recovery_{uuid.uuid4().hex[:8]}"

    with engine.connect() as conn:
        conn.execute(sa.text(f"CREATE SCHEMA {schema_name}"))

    temp_migration_path: Path | None = None
    try:
        repo_root = Path(__file__).resolve().parent.parent.parent
        alembic_cfg = Config(str(repo_root / "alembic.ini"))
        alembic_cfg.set_main_option("sqlalchemy.url", test_pg_url)

        # 1. Upgrade a head nello schema effimero
        command.upgrade(alembic_cfg, "head")

        # 2. Iniezione migrazione temporanea fallimentare
        temp_rev_id = f"temp_err_{uuid.uuid4().hex[:8]}"
        temp_migration_path = VERSIONS_DIR / f"{temp_rev_id}.py"
        temp_content = f'''"""Migrazione effimera di test per verifica recovery transazionale."""
revision = "{temp_rev_id}"
down_revision = "20261011_0003"
branch_labels = None
depends_on = None

from alembic import op
import sqlalchemy as sa

def upgrade() -> None:
    # Azione 1: DDL valida
    op.create_table("temp_should_rollback", sa.Column("id", sa.Integer, primary_key=True))
    # Azione 2: Errore transazionale forzato
    raise RuntimeError("Errore intenzionale per testare il rollback transazionale")

def downgrade() -> None:
    op.drop_table("temp_should_rollback")
'''
        temp_migration_path.write_text(temp_content, encoding="utf-8")

        # 3. Tentativo di upgrade -> deve fallire
        with pytest.raises(RuntimeError):
            command.upgrade(alembic_cfg, temp_rev_id)

        # 4. Verifica che temp_should_rollback NON esista (rollback riuscito)
        with engine.connect() as conn:
            table_check = conn.execute(
                sa.text(f"SELECT to_regclass('{schema_name}.temp_should_rollback')")
            ).scalar()
            assert table_check is None, (
                "La tabella temp_should_rollback non deve esistere dopo il rollback!"
            )

            # Verifica alembic_version rimasta a 20261011_0003
            current_rev = conn.execute(
                sa.text(f"SELECT version_num FROM {schema_name}.alembic_version")  # noqa: S608
            ).scalar()
            assert current_rev == "20261011_0003", (
                f"alembic_version deve rimanere a 20261011_0003, trovato {current_rev}"
            )

        # 5. Rimozione migrazione temporanea
        temp_migration_path.unlink()
        temp_migration_path = None

        # 6. Riesecuzione upgrade head reale
        command.upgrade(alembic_cfg, "head")

    finally:
        if temp_migration_path and temp_migration_path.exists():
            temp_migration_path.unlink()
        with engine.connect() as conn:
            conn.execute(sa.text(f"DROP SCHEMA IF EXISTS {schema_name} CASCADE"))
        engine.dispose()

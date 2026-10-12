"""Test di regressione per il teardown completo e ordinato di Procrastinate.

Verifica che:
1. Il DDL completo della versione bloccata di Procrastinate venga analizzato e confrontato
   con il teardown presente nella migrazione iniziale (20261010_0001).
2. Tutti gli oggetti creati da Procrastinate (18 funzioni, 7 trigger, 4 tabelle, 3 tipi)
   vengano esplicitamente rimossi in downgrade().
3. In particolare, la funzione `procrastinate_defer_periodic_job_v2` e tutte le altre funzioni
   siano esplicitamente eliminate per impedire errori DuplicateFunction al successivo upgrade.
4. Nessun comando DROP utilizzi `CASCADE` indiscriminato.
5. L'ordine di teardown rispetti la gerarchia di dipendenza:
   Trigger -> Funzioni -> Tabelle (in ordine inverso FK) -> Tipi (compositi ed ENUM).
6. L'emissione di SQL offline e l'esecuzione di downgrade() eseguano i drop correttamente
   senza residui.
"""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
import sys
import tempfile
import uuid
from pathlib import Path
from typing import Any, Protocol, cast
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from procrastinate.schema import SchemaManager
from sqlalchemy.engine import make_url

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"
INITIAL_MIGRATION_PATH = VERSIONS_DIR / "20261010_0001_initial_infrastructure.py"
REPO_ROOT = Path(__file__).resolve().parent.parent.parent


class MigrationModule(Protocol):
    op: Any

    def downgrade(self) -> None: ...


def _load_migration_module(file_path: Path) -> MigrationModule:
    spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Impossibile caricare la migrazione: {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return cast(MigrationModule, module)


def _extract_procrastinate_schema_objects() -> dict[str, list[str]]:
    schema_sql = SchemaManager.get_schema()

    tables = re.findall(r"CREATE\s+TABLE\s+([a-zA-Z0-9_]+)", schema_sql, re.IGNORECASE)
    triggers = re.findall(r"CREATE\s+TRIGGER\s+([a-zA-Z0-9_]+)", schema_sql, re.IGNORECASE)
    functions = re.findall(
        r"CREATE\s+(?:OR\s+REPLACE\s+)?FUNCTION\s+([a-zA-Z0-9_]+)",
        schema_sql,
        re.IGNORECASE,
    )
    types = re.findall(r"CREATE\s+TYPE\s+([a-zA-Z0-9_]+)", schema_sql, re.IGNORECASE)

    return {
        "tables": sorted(set(tables)),
        "triggers": sorted(set(triggers)),
        "functions": sorted(set(functions)),
        "types": sorted(set(types)),
    }


def _extract_teardown_sql(mod: MigrationModule) -> str:
    executed_stmts: list[sa.TextClause] = []
    fake_op = MagicMock()

    def fake_execute(stmt: sa.TextClause) -> None:
        if isinstance(stmt, sa.TextClause):
            executed_stmts.append(stmt)

    fake_op.execute = fake_execute
    fake_op.drop_table = MagicMock()
    fake_op.drop_index = MagicMock()
    fake_op.get_bind = MagicMock()

    real_op = mod.op
    mod.op = fake_op
    try:
        mod.downgrade()
    finally:
        mod.op = real_op

    teardown_clauses = [
        str(stmt.text)
        for stmt in executed_stmts
        if "procrastinate_jobs" in str(stmt.text)
        or "procrastinate_events" in str(stmt.text)
        or "procrastinate_defer_periodic_job_v2" in str(stmt.text)
    ]
    assert len(teardown_clauses) == 1, (
        f"Attesa 1 clausola di teardown Procrastinate, trovate {len(teardown_clauses)}"
    )
    return teardown_clauses[0]


def test_no_official_drop_schema_in_procrastinate_library() -> None:
    """Certifica che Procrastinate 3.10.x non esponga metodi ufficiali di drop/teardown."""
    manager_methods = [m for m in dir(SchemaManager) if not m.startswith("_")]
    assert "apply_schema" in manager_methods
    assert "get_schema" in manager_methods
    assert "drop_schema" not in manager_methods
    assert "teardown_schema" not in manager_methods


def test_procrastinate_schema_inventory_matches_teardown() -> None:
    """Verifica che ogni oggetto DDL creato da Procrastinate abbia un DROP corrispondente."""
    objects = _extract_procrastinate_schema_objects()

    assert len(objects["tables"]) == 4
    assert len(objects["triggers"]) == 7
    assert len(objects["functions"]) == 18
    assert len(objects["types"]) == 3

    assert "procrastinate_defer_periodic_job_v2" in objects["functions"]

    mod = _load_migration_module(INITIAL_MIGRATION_PATH)
    teardown_sql = _extract_teardown_sql(mod)

    # 1. Nessun uso di CASCADE indiscriminato
    assert "CASCADE" not in teardown_sql.upper(), (
        "Il teardown Procrastinate non deve utilizzare CASCADE: ogni drop deve avere ownership "
        "e dipendenze chiare."
    )

    # 2. Ogni tabella deve avere DROP TABLE IF EXISTS
    for table_name in objects["tables"]:
        pattern = rf"DROP\s+TABLE\s+IF\s+EXISTS\s+{table_name}\b"
        assert re.search(pattern, teardown_sql, re.IGNORECASE), (
            f"Tabella Procrastinate mancante nel teardown: {table_name}"
        )

    # 3. Ogni trigger deve avere DROP TRIGGER IF EXISTS
    for trigger_name in objects["triggers"]:
        pattern = rf"DROP\s+TRIGGER\s+IF\s+EXISTS\s+{trigger_name}\s+ON\s+procrastinate_jobs\b"
        assert re.search(pattern, teardown_sql, re.IGNORECASE), (
            f"Trigger Procrastinate mancante nel teardown: {trigger_name}"
        )

    # 4. Ogni funzione deve avere DROP FUNCTION IF EXISTS
    for func_name in objects["functions"]:
        pattern = rf"DROP\s+FUNCTION\s+IF\s+EXISTS\s+{func_name}\b"
        assert re.search(pattern, teardown_sql, re.IGNORECASE), (
            f"Funzione Procrastinate mancante nel teardown: {func_name}"
        )

    # 5. Ogni tipo deve avere DROP TYPE IF EXISTS
    for type_name in objects["types"]:
        pattern = rf"DROP\s+TYPE\s+IF\s+EXISTS\s+{type_name}\b"
        assert re.search(pattern, teardown_sql, re.IGNORECASE), (
            f"Tipo Procrastinate mancante nel teardown: {type_name}"
        )


def test_procrastinate_teardown_dependency_ordering() -> None:
    """Verifica l'ordine topologico di eliminazione degli oggetti Procrastinate."""
    mod = _load_migration_module(INITIAL_MIGRATION_PATH)
    teardown_sql = _extract_teardown_sql(mod)
    lines = [
        line.strip()
        for line in teardown_sql.splitlines()
        if line.strip() and not line.strip().startswith("--")
    ]

    def line_index(pattern: str) -> int:
        for idx, line in enumerate(lines):
            if re.search(pattern, line, re.IGNORECASE):
                return idx
        raise ValueError(f"Pattern '{pattern}' non trovato nelle istruzioni di teardown")

    idx_trigger = line_index(r"DROP\s+TRIGGER\s+IF\s+EXISTS\s+procrastinate_trigger_delete_jobs_v1")
    idx_defer_periodic = line_index(
        r"DROP\s+FUNCTION\s+IF\s+EXISTS\s+procrastinate_defer_periodic_job_v2"
    )
    idx_fetch_job = line_index(r"DROP\s+FUNCTION\s+IF\s+EXISTS\s+procrastinate_fetch_job_v2")
    idx_table_events = line_index(r"DROP\s+TABLE\s+IF\s+EXISTS\s+procrastinate_events")
    idx_table_defers = line_index(r"DROP\s+TABLE\s+IF\s+EXISTS\s+procrastinate_periodic_defers")
    idx_table_jobs = line_index(r"DROP\s+TABLE\s+IF\s+EXISTS\s+procrastinate_jobs")
    idx_table_workers = line_index(r"DROP\s+TABLE\s+IF\s+EXISTS\s+procrastinate_workers")
    idx_type_status = line_index(r"DROP\s+TYPE\s+IF\s+EXISTS\s+procrastinate_job_status")

    # I trigger devono essere rimossi prima delle relative funzioni
    assert idx_trigger < idx_defer_periodic
    # Le funzioni (es. fetch_job che restituisce procrastinate_jobs) prima delle tabelle
    assert idx_fetch_job < idx_table_jobs
    # Tabelle: events e periodic_defers prima di jobs
    assert idx_table_events < idx_table_jobs
    assert idx_table_defers < idx_table_jobs
    # jobs prima di workers
    assert idx_table_jobs < idx_table_workers
    # Tipi dopo le tabelle
    assert idx_table_jobs < idx_type_status


def test_offline_sql_downgrade_procrastinate_teardown_statements() -> None:
    """Verifica che alembic downgrade --sql contenga tutti i DROP Procrastinate attesi."""
    env = os.environ.copy()
    env["GBRAIN_POSTGRES_PASSWORD"] = "test-secret"  # noqa: S105
    env["GBRAIN_AUTH_SECRET"] = "0" * 40
    env["GBRAIN_NEO4J_PASSWORD"] = "test-secret"  # noqa: S105
    cmd = [
        sys.executable,
        "-m",
        "alembic",
        "downgrade",
        "20261010_0001:base",
        "--sql",
    ]
    res = subprocess.run(  # noqa: S603
        cmd,
        cwd=str(REPO_ROOT),
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )
    stdout = res.stdout

    assert "DROP FUNCTION IF EXISTS procrastinate_defer_periodic_job_v2;" in stdout
    assert "DROP FUNCTION IF EXISTS procrastinate_fetch_job_v2;" in stdout
    assert "DROP TRIGGER IF EXISTS procrastinate_jobs_notify_queue_job_inserted_v1" in stdout
    assert "DROP TABLE IF EXISTS procrastinate_jobs;" in stdout
    assert "DROP TYPE IF EXISTS procrastinate_job_status;" in stdout
    assert "CASCADE" not in stdout.split("procrastinate_events")[1].split("app_configurations")[0]


def test_audit_all_procrastinate_functions_explicitly_named() -> None:
    """Verifica individualmente che ciascuna delle 18 funzioni Procrastinate sia nominata nel teardown."""
    objects = _extract_procrastinate_schema_objects()
    mod = _load_migration_module(INITIAL_MIGRATION_PATH)
    teardown_sql = _extract_teardown_sql(mod)

    # Elenco completo delle 18 funzioni attese nella versione bloccata
    expected_functions = [
        "procrastinate_defer_jobs_v1",
        "procrastinate_defer_periodic_job_v2",
        "procrastinate_fetch_job_v2",
        "procrastinate_finish_job_v1",
        "procrastinate_cancel_job_v1",
        "procrastinate_retry_job_v1",
        "procrastinate_retry_job_v2",
        "procrastinate_notify_queue_job_inserted_v1",
        "procrastinate_notify_queue_abort_job_v1",
        "procrastinate_trigger_function_status_events_insert_v1",
        "procrastinate_trigger_function_status_events_update_v1",
        "procrastinate_trigger_function_scheduled_events_v1",
        "procrastinate_trigger_abort_requested_events_procedure_v1",
        "procrastinate_unlink_periodic_defers_v1",
        "procrastinate_register_worker_v1",
        "procrastinate_unregister_worker_v1",
        "procrastinate_update_heartbeat_v1",
        "procrastinate_prune_stalled_workers_v1",
    ]

    assert sorted(objects["functions"]) == sorted(expected_functions)

    for func in expected_functions:
        drop_clause = f"DROP FUNCTION IF EXISTS {func};"
        assert drop_clause in teardown_sql, (
            f"Clausola DROP mancante per la funzione {func}: atteso '{drop_clause}'"
        )


def test_live_postgres_procrastinate_downgrade_and_reupgrade() -> None:
    """Se GBRAIN_TEST_POSTGRES_URL è configurato, verifica live su schema effimero il ciclo downgrade/upgrade."""
    test_pg_url = os.getenv("GBRAIN_TEST_POSTGRES_URL")
    if not test_pg_url:
        pytest.skip(
            "Test PostgreSQL reale isolato saltato: GBRAIN_TEST_POSTGRES_URL non impostata."
        )

    engine = sa.create_engine(test_pg_url, isolation_level="AUTOCOMMIT")
    schema_name = f"test_proc_td_{uuid.uuid4().hex[:8]}"

    with engine.connect() as conn:
        conn.execute(sa.text(f'CREATE SCHEMA "{schema_name}"'))

    with tempfile.TemporaryDirectory(prefix="gbrain_alembic_test_") as tmp_dir:
        temp_migrations = Path(tmp_dir) / "migrations"
        shutil.copytree(REPO_ROOT / "backend/migrations", temp_migrations)
        temp_env = temp_migrations / "env.py"
        temp_env.write_text(
            temp_env.read_text(encoding="utf-8").replace(
                'config.set_main_option("sqlalchemy.url", get_settings().database_url)',
                "# URL impostata dal test",
            ),
            encoding="utf-8",
        )

        u = make_url(test_pg_url)
        isolated_url = u.set(query={**u.query, "options": f"-c search_path={schema_name}"})
        raw_isolated_url = isolated_url.render_as_string(hide_password=False)
        escaped_isolated_url = raw_isolated_url.replace("%", "%%")

        alembic_cfg = Config(str(REPO_ROOT / "alembic.ini"))
        alembic_cfg.set_main_option("script_location", str(temp_migrations))
        alembic_cfg.set_main_option("sqlalchemy.url", escaped_isolated_url)

        migration_engine = sa.create_engine(
            isolated_url,
        )
        try:
            # 1. Upgrade fino a 0001 (che installa Procrastinate)
            command.upgrade(alembic_cfg, "20261010_0001")
            with migration_engine.connect() as conn:
                proc_func_count = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=:schema AND p.proname='procrastinate_defer_periodic_job_v2'"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                assert proc_func_count == 1, (
                    "procrastinate_defer_periodic_job_v2 deve esistere dopo upgrade"
                )

            # 2. Downgrade a base
            command.downgrade(alembic_cfg, "base")
            with migration_engine.connect() as conn:
                func_count = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=:schema"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                tbl_count = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname=:schema AND tablename <> 'alembic_version'"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                assert func_count == 0, (
                    f"Nessuna funzione deve rimanere dopo downgrade, trovate: {func_count}"
                )
                assert tbl_count == 0, (
                    f"Nessuna tabella deve rimanere dopo downgrade, trovate: {tbl_count}"
                )

            # 3. Secondo upgrade pulito senza DuplicateFunction
            command.upgrade(alembic_cfg, "20261010_0001")
            with migration_engine.connect() as conn:
                proc_func_count = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=:schema AND p.proname='procrastinate_defer_periodic_job_v2'"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                assert proc_func_count == 1, (
                    "Upgrade successivo dopo downgrade deve ricreare Procrastinate pulito"
                )
        finally:
            migration_engine.dispose()
            with engine.connect() as conn:
                conn.execute(sa.text(f'DROP SCHEMA "{schema_name}" CASCADE'))
            engine.dispose()

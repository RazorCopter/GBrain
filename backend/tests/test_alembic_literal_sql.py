"""Test regressivo per l'esecuzione di SQL grezzo con percentuali letterali in psycopg 3.

Verifica che:
1. Il testo SQL di Procrastinate contenente percentuali letterali (es. `(job id: %)`)
   non venga passato tramite `exec_driver_sql`, evitando l'errore:
   `psycopg.ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders, got '%)'`.
2. L'uso idiomatico di `sa.text()` con `op.execute()` compili correttamente le percentuali
   per il dialetto `postgresql+psycopg`, consentendo a psycopg 3 di interpretarle come letterali
   senza estrarre parametri o generare placeholder errati.
3. Il testo prodotto ed eseguito a livello di driver corrisponda fedelmente allo schema originale
   senza alcuna alterazione semantica.
4. L'audit AST di tutte le migrazioni certifichi l'assenza totale di chiamate a `exec_driver_sql`.
"""

from __future__ import annotations

import ast
import importlib.util
from pathlib import Path
from types import ModuleType
from unittest.mock import MagicMock

import pytest
import sqlalchemy as sa
from procrastinate.schema import SchemaManager
from psycopg import ProgrammingError
from psycopg._queries import PostgresQuery
from psycopg._transformer import Transformer

VERSIONS_DIR = Path(__file__).resolve().parent.parent / "migrations" / "versions"
_PG_DIALECT: sa.Dialect = sa.create_mock_engine(
    "postgresql+psycopg://", executor=lambda *a: None
).dialect


def _load_migration_module(file_path: Path) -> ModuleType:
    spec = importlib.util.spec_from_file_location(file_path.stem, file_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Impossibile caricare la migrazione: {file_path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_reproduce_psycopg_placeholder_failure_on_raw_percent_sql() -> None:
    """Dimostra che psycopg solleva ProgrammingError quando riceve SQL grezzo con '%'.

    Quando una query contenente `%` (non seguita da 's', 'b', 't' o un nome pyformat valido)
    viene passata a psycopg con parametri (anche dizionario vuoto `{}` come accade in
    `exec_driver_sql`), psycopg tenta di analizzare i placeholder e solleva:
    `psycopg.ProgrammingError: only '%s', '%b', '%t' are allowed as placeholders, got '%)'`.
    """
    raw_fragment = 'RAISE \'End status should be either "succeeded", "failed" or "aborted" (job id: %)\', job_id;'
    tx = Transformer()
    q = PostgresQuery(tx)

    with pytest.raises(
        ProgrammingError, match=r"only '%s', '%b', '%t' are allowed as placeholders, got '%\)'"
    ):
        q.convert(raw_fragment, {})

    # Lo stesso fallimento si riproduce sull'intero schema Procrastinate non compilato
    schema = SchemaManager.get_schema()
    q_schema = PostgresQuery(tx)
    with pytest.raises(
        ProgrammingError, match=r"only '%s', '%b', '%t' are allowed as placeholders, got '%\)'"
    ):
        q_schema.convert(schema, {})


def test_procrastinate_schema_literal_percent_via_sa_text() -> None:
    """Verifica che SchemaManager.get_schema() tramite sa.text() sia sicuro per psycopg 3.

    Non duplica il testo dello schema: usa direttamente `SchemaManager.get_schema()`.
    Dimostra che la compilazione con dialetto `postgresql+psycopg`:
    1. Riconosce i caratteri `%` letterali e li converte in `%%` per il driver.
    2. Non genera parametri di bind spuri (`params == {}`).
    3. Permette la conversione psycopg senza errori di placeholder.
    4. Ripristina il byte stream SQL originario con il singolo `%` letterale.
    """
    schema = SchemaManager.get_schema()
    assert "%" in schema, (
        "Lo schema Procrastinate deve contenere caratteri '%' per testare la regressione"
    )

    txt = sa.text(schema)
    dialect = _PG_DIALECT
    compiled = txt.compile(dialect=dialect)

    # 1. Nessun bindparam catturato erroneamente
    assert compiled.params == {}, "I parametri compilati devono essere vuoti"

    compiled_sql = str(compiled)
    # 2. Le percentuali letterali devono essere raddoppiate per psycopg
    assert compiled_sql.count("%") == schema.count("%") * 2

    # 3. Psycopg converte la query compilata senza errori
    tx = Transformer()
    q = PostgresQuery(tx)
    q.convert(compiled_sql, {})

    assert bool(q.params) is False, "La query convertita non deve contenere parametri psycopg"

    # 4. Il testo restituito a livello di wire verso PostgreSQL deve contenere il '%' letterale originale
    wire_sql = q.query.decode("utf-8")
    assert "job id: %)" in wire_sql
    assert "job id: %%)" not in wire_sql
    assert wire_sql == schema, (
        "Il testo inviato a PostgreSQL deve coincidere esattamente con lo schema originario"
    )


@pytest.mark.parametrize(
    "sql_fragment",
    [
        "RAISE 'Job was not found (job id: %)', job_id;",
        "RAISE 'Job was not found or not in \"doing\" status (job id: %)', job_id;",
        "SELECT '100% valid query' WHERE name LIKE 'test%';",
        "DO $$ BEGIN RAISE NOTICE 'progresso: 50%'; END $$;",
    ],
)
def test_equivalent_percent_fragments_safe_with_sa_text(sql_fragment: str) -> None:
    """Verifica frammenti equivalenti con percentuali letterali compilati per psycopg."""
    txt = sa.text(sql_fragment)
    dialect = _PG_DIALECT
    compiled_sql = str(txt.compile(dialect=dialect))

    tx = Transformer()
    q = PostgresQuery(tx)
    # Non deve sollevare ProgrammingError
    q.convert(compiled_sql, {})
    assert bool(q.params) is False
    assert q.query.decode("utf-8") == sql_fragment


def test_initial_infrastructure_upgrade_and_downgrade_uses_op_execute(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verifica che la migrazione 0001 esegua lo schema Procrastinate via op.execute(sa.text(...)).

    Verifica inoltre che bind.exec_driver_sql NON venga invocato né in upgrade né in downgrade.
    """
    mod = _load_migration_module(VERSIONS_DIR / "20261010_0001_initial_infrastructure.py")

    executed_op_stmts: list[sa.sql.elements.ClauseElement | str] = []

    def mock_op_execute(
        stmt: sa.sql.elements.ClauseElement | str, *args: object, **kwargs: object
    ) -> None:
        executed_op_stmts.append(stmt)

    mock_bind = MagicMock()
    mock_bind.exec_driver_sql = MagicMock(
        side_effect=RuntimeError("exec_driver_sql NON deve essere chiamato!")
    )

    monkeypatch.setattr(mod.op, "execute", mock_op_execute)
    monkeypatch.setattr(mod.op, "get_bind", lambda: mock_bind)
    monkeypatch.setattr(mod.op, "create_table", MagicMock())
    monkeypatch.setattr(mod.op, "create_index", MagicMock())
    monkeypatch.setattr(mod.op, "drop_table", MagicMock())
    monkeypatch.setattr(mod.op, "drop_index", MagicMock())

    # Esegui upgrade
    mod.upgrade()

    # Verifica che lo schema Procrastinate sia stato passato a op.execute come TextClause
    procrastinate_stmts = [
        s
        for s in executed_op_stmts
        if isinstance(s, sa.TextClause) and "procrastinate_jobs" in str(s)
    ]
    assert len(procrastinate_stmts) == 1, (
        f"Attesa esattamente 1 esecuzione dello schema Procrastinate via op.execute(sa.text(...)), "
        f"trovate {len(procrastinate_stmts)}"
    )
    # Verifica che exec_driver_sql non sia mai stato chiamato
    assert mock_bind.exec_driver_sql.call_count == 0

    # Esegui downgrade
    executed_op_stmts.clear()
    mod.downgrade()

    teardown_stmts = [
        s
        for s in executed_op_stmts
        if isinstance(s, sa.TextClause) and "DROP TABLE IF EXISTS procrastinate_events" in str(s)
    ]
    assert len(teardown_stmts) == 1, (
        f"Attesa esattamente 1 esecuzione del teardown Procrastinate via op.execute(sa.text(...)), "
        f"trovate {len(teardown_stmts)}"
    )
    assert mock_bind.exec_driver_sql.call_count == 0


def test_ast_audit_no_exec_driver_sql_in_any_migration() -> None:
    """Audit AST statico: nessuna migrazione deve usare exec_driver_sql per eseguire DDL/SQL grezzo."""
    for file_path in sorted(VERSIONS_DIR.glob("*.py")):
        if file_path.name.startswith("__"):
            continue

        tree = ast.parse(file_path.read_text(encoding="utf-8"), filename=file_path.name)
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                assert node.func.attr != "exec_driver_sql", (
                    f"Trovata chiamata a `exec_driver_sql` vietata nel file {file_path.name}:{node.lineno}! "
                    "Utilizzare `op.execute(sa.text(...))` per l'esecuzione di SQL testuale/DDL."
                )

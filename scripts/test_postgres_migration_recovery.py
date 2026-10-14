#!/usr/bin/env python3
"""Collaudo isolato delle migrazioni Alembic su PostgreSQL reale.

Il chiamante deve fornire un database di test dedicato su loopback. Il test crea uno
schema casuale, forza ``search_path`` su quello schema, usa una copia temporanea
dell'ambiente Alembic e rimuove esclusivamente le risorse create dal test.
"""

from __future__ import annotations

import argparse
import shutil
import tempfile
import uuid
from pathlib import Path

import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from sqlalchemy.engine import URL, make_url

HEAD_REVISION = "20261015_0007"
EXPECTED_ENUMS = {
    "import_job_status": (
        "pending",
        "running",
        "completed",
        "failed",
        "cancelled",
        "completed_with_warnings",
        "superseded",
    ),
    "outbox_status": ("pending", "processing", "completed", "failed"),
    "connector_account_status": (
        "pending_authorization",
        "active",
        "reauth_required",
        "revoked",
        "disabled",
    ),
    "source_policy_status": ("draft", "active", "paused", "disabled"),
    "gmail_reconciliation_state": (
        "not_required",
        "required_history_expired",
        "running",
        "complete",
    ),
    "source_item_state": ("active", "removed", "out_of_scope", "excluded"),
}


def _safe_url(url: URL) -> str:
    return url.render_as_string(hide_password=True)


def _validate_isolated_url(database_url: str) -> URL:
    url = make_url(database_url)
    if url.get_backend_name() != "postgresql":
        raise ValueError("Il test accetta esclusivamente PostgreSQL")
    if url.host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Il server di test deve essere raggiungibile esclusivamente via loopback")
    if not (url.database or "").startswith("gbrain_migration_test_"):
        raise ValueError("Il database dedicato deve iniziare con 'gbrain_migration_test_'")
    return url


def _revision(conn: sa.Connection, schema_name: str) -> str | None:
    return conn.execute(
        sa.text(f'SELECT version_num FROM "{schema_name}".alembic_version')  # noqa: S608
    ).scalar_one_or_none()


def _assert_head_and_enums(engine: sa.Engine, schema_name: str) -> None:
    with engine.connect() as conn:
        revision = _revision(conn, schema_name)
        if revision != HEAD_REVISION:
            raise RuntimeError(f"Revisione osservata {revision!r}, attesa {HEAD_REVISION!r}")
        rows = conn.execute(
            sa.text(
                "SELECT t.typname, e.enumlabel "
                "FROM pg_type t "
                "JOIN pg_namespace n ON n.oid=t.typnamespace "
                "JOIN pg_enum e ON e.enumtypid=t.oid "
                "WHERE n.nspname=:schema AND t.typname = ANY(:names) "
                "ORDER BY t.typname, e.enumsortorder"
            ),
            {"schema": schema_name, "names": sorted(EXPECTED_ENUMS)},
        ).all()
        observed: dict[str, list[str]] = {}
        for row in rows:
            observed.setdefault(str(row._mapping["typname"]), []).append(
                str(row._mapping["enumlabel"])
            )
        normalized = {name: tuple(labels) for name, labels in observed.items()}
        if normalized != EXPECTED_ENUMS:
            raise RuntimeError(
                f"ENUM non conformi: osservati={normalized!r}, attesi={EXPECTED_ENUMS!r}"
            )
        print(f"      revisione={revision}; ENUM e label esatti={normalized!r}")


def run_recovery_test(database_url: str) -> None:
    repo_root = Path(__file__).resolve().parent.parent
    source_migrations = repo_root / "backend" / "migrations"
    alembic_ini = repo_root / "alembic.ini"
    base_url = _validate_isolated_url(database_url)
    schema_name = f"test_recovery_{uuid.uuid4().hex[:12]}"
    isolated_url = base_url.update_query_dict({"options": f"-csearch_path={schema_name}"})

    print(f"[1/10] Connessione dedicata: {_safe_url(base_url)}")
    admin_engine = sa.create_engine(base_url, isolation_level="AUTOCOMMIT")
    migration_engine = sa.create_engine(isolated_url)
    schema_created = False

    try:
        with admin_engine.connect() as conn:
            conn.execute(sa.text(f'CREATE SCHEMA "{schema_name}"'))
            schema_created = True
        print(f"[2/10] Schema effimero creato: {schema_name}; search_path forzato")

        with tempfile.TemporaryDirectory(prefix="gbrain_alembic_") as temp_dir:
            temp_migrations = Path(temp_dir) / "migrations"
            shutil.copytree(source_migrations, temp_migrations)
            temp_versions = temp_migrations / "versions"
            temp_env = temp_migrations / "env.py"
            temp_env.write_text(
                temp_env.read_text(encoding="utf-8").replace(
                    'config.set_main_option("sqlalchemy.url", get_settings().database_url)',
                    "pass  # URL già impostata dal solo harness isolato; non caricare credenziali deployment",
                ),
                encoding="utf-8",
            )

            alembic_cfg = Config(str(alembic_ini))
            alembic_cfg.set_main_option("script_location", str(temp_migrations))
            raw_isolated_url = isolated_url.render_as_string(hide_password=False)
            # ConfigParser interpreta i caratteri percentuali dell'URL-encoding come
            # interpolazioni; raddoppiarli conserva esattamente il valore originale.
            alembic_cfg.set_main_option("sqlalchemy.url", raw_isolated_url.replace("%", "%%"))

            print("[3/10] upgrade head da schema completamente vuoto")
            command.upgrade(alembic_cfg, "head")
            _assert_head_and_enums(migration_engine, schema_name)

            print("[4/10] secondo upgrade head (atteso no-op)")
            command.upgrade(alembic_cfg, "head")
            _assert_head_and_enums(migration_engine, schema_name)

            temp_rev_id = f"temp_err_{uuid.uuid4().hex[:12]}"
            temp_migration_path = temp_versions / f"{temp_rev_id}.py"
            temp_migration_path.write_text(
                f'''"""Revisione esclusivamente temporanea per il test di rollback."""
from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql
revision = "{temp_rev_id}"
down_revision = "{HEAD_REVISION}"
branch_labels = None
depends_on = None
rollback_enum = postgresql.ENUM("created", name="temp_should_rollback_enum", create_type=False)
def upgrade() -> None:
    bind = op.get_bind()
    rollback_enum.create(bind, checkfirst=True)
    op.create_table("temp_should_rollback", sa.Column("id", sa.Integer, primary_key=True), sa.Column("state", rollback_enum, nullable=False))
    raise RuntimeError("errore intenzionale per verificare il rollback DDL")
def downgrade() -> None:
    op.drop_table("temp_should_rollback")
    rollback_enum.drop(op.get_bind(), checkfirst=True)
''',
                encoding="utf-8",
            )

            print("[5/10] upgrade della revisione temporanea errata (fallimento atteso)")
            try:
                command.upgrade(alembic_cfg, temp_rev_id)
            except RuntimeError as exc:
                if "errore intenzionale" not in str(exc):
                    raise
                print(f"      fallimento controllato: {exc}")
            else:
                raise RuntimeError("La revisione temporanea non ha prodotto il fallimento atteso")

            print("[6/10] verifica rollback di tabella, tipo e alembic_version")
            with migration_engine.connect() as conn:
                table_exists = conn.execute(
                    sa.text("SELECT to_regclass(:qualified)"),
                    {"qualified": f"{schema_name}.temp_should_rollback"},
                ).scalar_one()
                type_exists = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                        "WHERE n.nspname=:schema AND t.typname='temp_should_rollback_enum'"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                revision = _revision(conn, schema_name)
            if table_exists is not None or type_exists != 0 or revision != HEAD_REVISION:
                raise RuntimeError(
                    f"Rollback incompleto: table={table_exists!r}, type_count={type_exists}, revision={revision!r}"
                )
            print(f"      table=None; type_count=0; revision={revision}")

            temp_migration_path.unlink()
            print("[7/10] upgrade reale dopo il fallimento")
            command.upgrade(alembic_cfg, "head")
            _assert_head_and_enums(migration_engine, schema_name)

            print("[8/10] downgrade completo a base")
            command.downgrade(alembic_cfg, "base")
            with migration_engine.connect() as conn:
                user_tables = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_tables WHERE schemaname=:schema "
                        "AND tablename <> 'alembic_version'"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                enum_count = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                        "WHERE n.nspname=:schema AND t.typtype='e'"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                function_count = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=:schema"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                trigger_count = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_trigger tg JOIN pg_class c ON c.oid=tg.tgrelid "
                        "JOIN pg_namespace n ON n.oid=c.relnamespace "
                        "WHERE n.nspname=:schema AND NOT tg.tgisinternal"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                composite_type_count = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                        "WHERE n.nspname=:schema AND t.typtype='c' "
                        "AND NOT EXISTS (SELECT 1 FROM pg_class c WHERE c.relname=t.typname AND c.relkind='r')"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                defer_periodic_func_exists = conn.execute(
                    sa.text(
                        "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
                        "WHERE n.nspname=:schema AND p.proname='procrastinate_defer_periodic_job_v2'"
                    ),
                    {"schema": schema_name},
                ).scalar_one()
                version_rows = conn.execute(
                    sa.text(f'SELECT count(*) FROM "{schema_name}".alembic_version')  # noqa: S608
                ).scalar_one()
            if (
                user_tables
                or enum_count
                or function_count
                or trigger_count
                or composite_type_count
                or defer_periodic_func_exists
                or version_rows
            ):
                raise RuntimeError(
                    f"Downgrade incompleto: tables={user_tables}, enums={enum_count}, "
                    f"functions={function_count}, triggers={trigger_count}, "
                    f"composite_types={composite_type_count}, "
                    f"defer_periodic_func={defer_periodic_func_exists}, versions={version_rows}"
                )
            print(
                "      tabelle applicative=0; ENUM=0; funzioni=0; trigger=0; "
                "tipi compositi=0; righe alembic_version=0"
            )

            print("[9/10] nuovo upgrade head dopo downgrade completo")
            command.upgrade(alembic_cfg, "head")
            _assert_head_and_enums(migration_engine, schema_name)
            print("[10/10] Tutte le invarianti PostgreSQL reali sono soddisfatte")
    finally:
        migration_engine.dispose()
        if schema_created:
            with admin_engine.connect() as conn:
                conn.execute(sa.text(f'DROP SCHEMA "{schema_name}" CASCADE'))
            print(f"Cleanup: rimosso esclusivamente lo schema creato dal test: {schema_name}")
        admin_engine.dispose()


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--url", required=True, help="URL PostgreSQL dedicata su loopback")
    args = parser.parse_args()
    run_recovery_test(args.url)


if __name__ == "__main__":
    main()

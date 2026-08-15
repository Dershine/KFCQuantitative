from __future__ import annotations

import duckdb
import pytest
from filelock import Timeout

from kfcquant.db import Database
from kfcquant.migrations import Migration, MigrationRunner


def test_empty_database_initialization_and_repeat_are_idempotent(tmp_path):
    path = tmp_path / "empty.duckdb"
    database = Database(path, initial_cash=123_456)

    database.initialize()
    database.initialize()

    with duckdb.connect(str(path), read_only=True) as connection:
        versions = [
            row[0]
            for row in connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        ]
        account = connection.execute(
            "SELECT initial_cash, cash FROM paper_account WHERE account_id='default'"
        ).fetchone()
        signal_columns = {row[1] for row in connection.execute("PRAGMA table_info('signal_runs')").fetchall()}

    assert versions == [1, 2, 3]
    assert account == (123_456.0, 123_456.0)
    assert {"signal_kind", "strategy_version", "information_cutoff", "data_as_of", "lifecycle_state"} <= signal_columns


def test_existing_signal_schema_is_migrated_in_place(tmp_path):
    path = tmp_path / "legacy.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """CREATE TABLE signal_runs (
               run_id VARCHAR PRIMARY KEY, as_of TIMESTAMPTZ NOT NULL, status VARCHAR NOT NULL,
               data_fresh BOOLEAN NOT NULL, official_news_healthy BOOLEAN NOT NULL,
               mainstream_news_healthy BOOLEAN NOT NULL, tradable BOOLEAN NOT NULL,
               message VARCHAR NOT NULL, candidate_count INTEGER NOT NULL, metadata_json VARCHAR NOT NULL
               )"""
        )
        connection.execute(
            """INSERT INTO signal_runs VALUES (
               'legacy', '2026-08-10 14:40:00+08:00', 'success', true, true, true, true, 'ok', 1, '{}'
               )"""
        )

    database = Database(path)
    database.initialize()
    run = database.latest_signal_run()

    assert database.migration_version() == 3
    assert run["signal_kind"] == "preclose_entry"
    assert run["strategy_version"] == "preclose-v1"
    assert run["information_cutoff"] == run["as_of"]
    assert run["lifecycle_state"] == "published"


def test_existing_result_statuses_are_mapped_to_lifecycle_states(tmp_path):
    path = tmp_path / "legacy-statuses.duckdb"
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """CREATE TABLE signal_runs (
               run_id VARCHAR PRIMARY KEY, as_of TIMESTAMPTZ NOT NULL, status VARCHAR NOT NULL,
               data_fresh BOOLEAN NOT NULL, official_news_healthy BOOLEAN NOT NULL,
               mainstream_news_healthy BOOLEAN NOT NULL, tradable BOOLEAN NOT NULL,
               message VARCHAR NOT NULL, candidate_count INTEGER NOT NULL, metadata_json VARCHAR NOT NULL
               )"""
        )
        for index, status in enumerate(("success", "degraded", "running", "failed", "missed")):
            connection.execute(
                "INSERT INTO signal_runs VALUES (?, ?, ?, false, false, false, false, '', 0, '{}')",
                [status, f"2026-08-10 14:{40 + index}:00+08:00", status],
            )

    Database(path).initialize()

    with duckdb.connect(str(path), read_only=True) as connection:
        states = dict(connection.execute("SELECT status, lifecycle_state FROM signal_runs").fetchall())
    assert states == {
        "success": "published",
        "degraded": "published",
        "running": "evaluating",
        "failed": "failed",
        "missed": "missed",
    }


def test_additive_lifecycle_migration_remains_writable_by_previous_release(tmp_path):
    path = tmp_path / "rollback-compatible.duckdb"
    database = Database(path)
    database.initialize()

    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """INSERT INTO signal_runs (
               run_id, as_of, signal_kind, strategy_version, information_cutoff, data_as_of,
               status, data_fresh, official_news_healthy, mainstream_news_healthy,
               tradable, message, candidate_count, metadata_json
               ) VALUES ('old-writer', '2026-08-10 14:40:00+08:00', 'preclose_entry',
                         'preclose-v1', '2026-08-10 14:40:00+08:00', NULL,
                         'success', true, true, true, true, 'ok', 0, '{}')"""
        )

    run = database.latest_signal_run()
    assert run["run_id"] == "old-writer"
    assert run["lifecycle_state"] == "published"


def test_database_access_uses_shared_cross_process_lock(tmp_path):
    path = tmp_path / "locked.duckdb"
    lock_path = tmp_path / "database.lock"
    writer = Database(path, lock_path=lock_path)
    reader = Database(path, lock_timeout_seconds=0, lock_path=lock_path)
    writer.initialize()

    with writer.lock, pytest.raises(Timeout):
        reader.get_securities()


def test_failed_migration_rolls_back_and_can_resume(tmp_path):
    path = tmp_path / "failure.duckdb"
    with duckdb.connect(str(path)) as connection:
        runner = MigrationRunner(connection)
        runner.apply(
            [
                Migration(1, "base", ("CREATE TABLE stable (value INTEGER)",)),
            ]
        )
        with pytest.raises(duckdb.Error):
            runner.apply(
                [
                    Migration(1, "base", ("CREATE TABLE stable (value INTEGER)",)),
                    Migration(
                        2,
                        "broken",
                        (
                            "CREATE TABLE partial (value INTEGER)",
                            "THIS IS NOT VALID SQL",
                        ),
                    ),
                ]
            )

        version = connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0]
        partial_exists = connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='partial'"
        ).fetchone()[0]
        assert version == 1
        assert partial_exists == 0

        runner.apply(
            [
                Migration(1, "base", ("CREATE TABLE stable (value INTEGER)",)),
                Migration(2, "fixed", ("CREATE TABLE partial (value INTEGER)",)),
            ]
        )
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 2
        assert connection.execute(
            "SELECT count(*) FROM information_schema.tables WHERE table_name='partial'"
        ).fetchone()[0] == 1


def test_migration_runner_rejects_unordered_or_inconsistent_versions(tmp_path):
    path = tmp_path / "inconsistent.duckdb"
    migration_1 = Migration(1, "base", ("CREATE TABLE stable (value INTEGER)",))
    migration_2 = Migration(2, "second", ("CREATE TABLE second (value INTEGER)",))
    migration_3 = Migration(3, "third", ("CREATE TABLE third (value INTEGER)",))
    with duckdb.connect(str(path)) as connection:
        runner = MigrationRunner(connection)
        with pytest.raises(ValueError, match="ordered"):
            runner.apply([migration_2])

        runner.apply([migration_1])
        connection.execute("INSERT INTO schema_migrations(version) VALUES (3)")
        with pytest.raises(RuntimeError, match="version gap"):
            runner.apply([migration_1, migration_2, migration_3])

        connection.execute("DELETE FROM schema_migrations WHERE version=3")
        connection.execute("INSERT INTO schema_migrations(version) VALUES (2)")
        with pytest.raises(RuntimeError, match="newer"):
            runner.apply([migration_1])

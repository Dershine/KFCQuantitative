from __future__ import annotations

from datetime import datetime, timedelta

import duckdb
import pytest
from filelock import Timeout

from kfcquant.config import SHANGHAI_TZ
from kfcquant.db import MIGRATIONS, Database
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

    assert versions == [1, 2, 3, 4, 5, 6]
    assert account == (123_456.0, 123_456.0)
    assert {"signal_kind", "strategy_version", "information_cutoff", "data_as_of", "lifecycle_state"} <= signal_columns
    with duckdb.connect(str(path), read_only=True) as connection:
        lease_columns = {row[1] for row in connection.execute("PRAGMA table_info('job_leases')").fetchall()}
    assert {"job_run_id", "heartbeat_at", "lease_expires_at", "recovery_count"} <= lease_columns
    with duckdb.connect(str(path), read_only=True) as connection:
        attribution_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('strategy_attributions')").fetchall()
        }
    assert {
        "entity_kind",
        "entity_id",
        "strategy_id",
        "strategy_version",
        "parameter_hash",
        "parameter_snapshot_json",
    } <= attribution_columns
    with duckdb.connect(str(path), read_only=True) as connection:
        ingestion_columns = {
            row[1] for row in connection.execute("PRAGMA table_info('ingestion_manifests')").fetchall()
        }
    assert {
        "batch_id",
        "dataset_kind",
        "schema_version",
        "provider",
        "collected_at",
        "snapshot_path",
        "content_sha256",
        "row_count",
        "quality_report_json",
        "job_run_id",
    } <= ingestion_columns


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

    assert database.migration_version() == 6
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


def test_job_lease_migration_recovers_legacy_running_job_and_preserves_old_writer(tmp_path):
    path = tmp_path / "legacy-job.duckdb"
    started = datetime(2026, 8, 10, 14, 40, tzinfo=SHANGHAI_TZ)
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """CREATE TABLE job_runs (
               job_run_id VARCHAR PRIMARY KEY, job_name VARCHAR NOT NULL,
               scheduled_for TIMESTAMPTZ, started_at TIMESTAMPTZ NOT NULL,
               finished_at TIMESTAMPTZ, status VARCHAR NOT NULL,
               message VARCHAR NOT NULL, metadata_json VARCHAR NOT NULL
               )"""
        )
        connection.execute(
            "INSERT INTO job_runs VALUES ('legacy-running', 'sync-eod', NULL, ?, NULL, 'running', 'started', '{}')",
            [started],
        )

    database = Database(path)
    database.initialize()
    lease = database.table("job_leases").iloc[0]
    assert lease["heartbeat_at"].to_pydatetime() == started
    assert lease["lease_expires_at"].to_pydatetime() == started
    assert database.recover_expired_jobs(started + timedelta(seconds=1)) == ["legacy-running"]

    # Schema v4 keeps job_runs at eight columns so the previous release remains writable.
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """INSERT INTO job_runs VALUES (
               'old-writer', 'sync-calendar', NULL, ?, ?, 'success', 'ok', '{}'
               )""",
            [started, started],
        )
    assert database.latest_job("sync-calendar")["status"] == "success"


def test_failure_after_strategy_attribution_migration_rolls_back_and_resumes(tmp_path):
    path = tmp_path / "job-lease-migration-failure.duckdb"
    broken = (
        *MIGRATIONS,
        Migration(7, "broken_after_ingestion_manifest", ("CREATE TABLE partial (value INTEGER)", "BAD SQL")),
    )
    fixed = (*MIGRATIONS, Migration(7, "fixed_after_ingestion_manifest", ("CREATE TABLE partial (value INTEGER)",)))

    with duckdb.connect(str(path)) as connection:
        runner = MigrationRunner(connection)
        with pytest.raises(duckdb.Error):
            runner.apply(broken)
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 6
        assert not connection.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_name='partial'"
        ).fetchone()
        runner.apply(fixed)
        assert connection.execute("SELECT max(version) FROM schema_migrations").fetchone()[0] == 7


def test_strategy_attribution_migration_preserves_old_positional_writers(tmp_path):
    path = tmp_path / "strategy-attribution-compatible.duckdb"
    database = Database(path)
    database.initialize()

    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """INSERT INTO signal_runs (
               run_id, as_of, signal_kind, strategy_version, information_cutoff, data_as_of,
               status, data_fresh, official_news_healthy, mainstream_news_healthy,
               tradable, message, candidate_count, metadata_json
               ) VALUES ('old-run', '2026-08-10 14:40:00+08:00', 'preclose_entry',
                         'preclose-v1', '2026-08-10 14:40:00+08:00', NULL,
                         'success', true, true, true, true, 'ok', 0, '{}')"""
        )
        connection.execute(
            """INSERT INTO paper_orders VALUES (
               'old-order', 'old-run', '600000.SH', 'buy', 'filled',
               '2026-08-10 14:40:00+08:00', 10000, 'legacy', 'old-position'
               )"""
        )
        connection.execute(
            """INSERT INTO paper_positions VALUES (
               'old-position', '600000.SH', '2026-08-10 14:45:00+08:00', '2026-08-10',
               1000, 10, 10.01, 5, 'open', NULL, NULL, NULL, NULL
               )"""
        )
        connection.execute(
            """INSERT INTO candidate_outcomes VALUES (
               'old-candidate-outcome', 'old-run', '600000.SH', 'preclose_entry', 'miss',
               NULL, NULL, NULL, NULL, NULL, NULL, 'legacy', '2026-08-11 15:00:00+08:00'
               )"""
        )
        connection.execute(
            """INSERT INTO opportunity_outcomes VALUES (
               'old-opportunity-outcome', 'old-position', '600000.SH', '2026-08-10',
               false, false, 1, 0, NULL, NULL, '2026-08-11 15:00:00+08:00'
               )"""
        )

    database.initialize()

    assert database.table_with_strategy("paper_orders").iloc[0]["strategy_id"] == "preclose-entry"
    assert database.table_with_strategy("paper_positions").iloc[0]["strategy_id"] == "preclose-entry"
    assert database.table_with_strategy("candidate_outcomes").iloc[0]["strategy_id"] == "preclose-entry"
    assert database.table_with_strategy("opportunity_outcomes").iloc[0]["strategy_id"] == "preclose-entry"


def test_strategy_attribution_backfill_failure_rolls_back_and_recovers(tmp_path, monkeypatch):
    path = tmp_path / "strategy-attribution-backfill-recovery.duckdb"
    database = Database(path)
    database.initialize()
    with duckdb.connect(str(path)) as connection:
        connection.execute(
            """INSERT INTO signal_runs (
               run_id, as_of, signal_kind, strategy_version, information_cutoff, data_as_of,
               status, data_fresh, official_news_healthy, mainstream_news_healthy,
               tradable, message, candidate_count, metadata_json
               ) VALUES ('rollback-run', '2026-08-10 14:40:00+08:00', 'preclose_entry',
                         'preclose-v1', '2026-08-10 14:40:00+08:00', NULL,
                         'success', true, true, true, true, 'ok', 0, '{}')"""
        )

    original = Database._backfill_strategy_attributions

    def fail_after_backfill(connection):
        original(connection)
        raise RuntimeError("injected backfill failure")

    monkeypatch.setattr(Database, "_backfill_strategy_attributions", staticmethod(fail_after_backfill))
    with pytest.raises(RuntimeError, match="injected"):
        database.initialize()
    assert database.table("strategy_attributions").query("entity_id == 'rollback-run'").empty

    monkeypatch.setattr(Database, "_backfill_strategy_attributions", staticmethod(original))
    database.initialize()
    recovered = database.table("strategy_attributions").query("entity_id == 'rollback-run'").iloc[0]
    assert recovered["strategy_id"] == "preclose-entry"


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

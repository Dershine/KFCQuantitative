from __future__ import annotations

import duckdb
import pytest
from filelock import Timeout

from kfcquant.db import Database


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

    assert database.migration_version() == 2
    assert run["signal_kind"] == "preclose_entry"
    assert run["strategy_version"] == "preclose-v1"
    assert run["information_cutoff"] == run["as_of"]


def test_database_access_uses_shared_cross_process_lock(tmp_path):
    path = tmp_path / "locked.duckdb"
    lock_path = tmp_path / "database.lock"
    writer = Database(path, lock_path=lock_path)
    reader = Database(path, lock_timeout_seconds=0, lock_path=lock_path)
    writer.initialize()

    with writer.lock, pytest.raises(Timeout):
        reader.get_securities()

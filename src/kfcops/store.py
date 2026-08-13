from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deployments (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    requested_at TEXT NOT NULL,
    finished_at TEXT,
    target_sha TEXT NOT NULL,
    previous_sha TEXT,
    status TEXT NOT NULL,
    message TEXT NOT NULL,
    log TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    happened_at TEXT NOT NULL,
    action TEXT NOT NULL,
    target TEXT,
    result TEXT NOT NULL,
    detail TEXT NOT NULL
);
"""


class OpsStore:
    def __init__(self, path: Path):
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.executescript(SCHEMA)

    @contextmanager
    def connect(self):
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        try:
            yield connection
        finally:
            connection.close()

    def get(self, key: str, default: str = "") -> str:
        with self.connect() as connection:
            row = connection.execute("SELECT value FROM state WHERE key=?", (key,)).fetchone()
        return str(row["value"]) if row else default

    def set(self, key: str, value: str) -> None:
        with self.connect() as connection:
            connection.execute("INSERT OR REPLACE INTO state(key, value) VALUES (?, ?)", (key, value))
            connection.commit()

    def create_deployment(self, target_sha: str, previous_sha: str, status: str, message: str) -> int:
        now = datetime.now(UTC).isoformat()
        with self.connect() as connection:
            cursor = connection.execute(
                "INSERT INTO deployments(requested_at,target_sha,previous_sha,status,message) VALUES(?,?,?,?,?)",
                (now, target_sha, previous_sha or None, status, message),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def update_deployment(self, deployment_id: int, status: str, message: str, log: str = "") -> None:
        finished = (
            datetime.now(UTC).isoformat()
            if status in {"succeeded", "rolled_back", "manual_intervention_required"}
            else None
        )
        with self.connect() as connection:
            connection.execute(
                "UPDATE deployments SET status=?,message=?,log=log||?,finished_at=coalesce(?,finished_at) WHERE id=?",
                (status, message, log, finished, deployment_id),
            )
            connection.commit()

    def audit(self, action: str, target: str, result: str, detail: str) -> None:
        with self.connect() as connection:
            connection.execute(
                "INSERT INTO audit(happened_at,action,target,result,detail) VALUES(?,?,?,?,?)",
                (datetime.now(UTC).isoformat(), action, target, result, detail),
            )
            connection.commit()

    def recent_deployments(self, limit: int = 30) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM deployments ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def recent_audit(self, limit: int = 100) -> list[dict[str, object]]:
        with self.connect() as connection:
            rows = connection.execute("SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)).fetchall()
        return [dict(row) for row in rows]

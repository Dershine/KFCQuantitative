from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from enum import StrEnum

import duckdb

LOGGER = logging.getLogger(__name__)

MIGRATION_TABLE_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version INTEGER PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
)
"""


class RollbackPolicy(StrEnum):
    """How an older release may recover after this migration is applied."""

    IN_PLACE = "in_place"
    BACKUP_RESTORE = "backup_restore"
    REQUIRES_APPROVAL = "requires_approval"


@dataclass(frozen=True)
class Migration:
    version: int
    name: str
    statements: tuple[str, ...]
    rollback_policy: RollbackPolicy | None = None
    rollback_reason: str = ""


def migration_contract(migrations: list[Migration] | tuple[Migration, ...]) -> dict[str, object]:
    """Return a release's machine-readable schema and rollback contract."""

    MigrationRunner._validate_registry(migrations)
    missing = [migration.version for migration in migrations if migration.rollback_policy is None]
    if missing:
        raise ValueError(f"migrations require an explicit rollback policy: {missing}")
    missing_reason = [migration.version for migration in migrations if not migration.rollback_reason.strip()]
    if missing_reason:
        raise ValueError(f"migrations require a rollback reason: {missing_reason}")
    return {
        "contract_version": 1,
        "latest_schema_version": len(migrations),
        "migrations": [
            {
                "version": migration.version,
                "name": migration.name,
                "statements_sha256": hashlib.sha256(
                    json.dumps(migration.statements, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
                ).hexdigest(),
                "rollback_policy": migration.rollback_policy.value,
                "rollback_reason": migration.rollback_reason,
            }
            for migration in migrations
        ],
    }


class MigrationRunner:
    """Apply a consecutive migration registry one transaction at a time."""

    def __init__(self, connection: duckdb.DuckDBPyConnection):
        self.connection = connection

    @staticmethod
    def _validate_registry(migrations: list[Migration] | tuple[Migration, ...]) -> None:
        versions = [migration.version for migration in migrations]
        if versions != list(range(1, len(migrations) + 1)):
            raise ValueError("migration versions must be unique, ordered, and consecutive from 1")
        if any(not migration.name.strip() or not migration.statements for migration in migrations):
            raise ValueError("every migration requires a name and at least one statement")

    def apply(self, migrations: list[Migration] | tuple[Migration, ...]) -> None:
        self._validate_registry(migrations)
        self.connection.execute(MIGRATION_TABLE_SQL)
        applied = {
            int(row[0])
            for row in self.connection.execute("SELECT version FROM schema_migrations ORDER BY version").fetchall()
        }
        if applied and applied != set(range(1, max(applied) + 1)):
            raise RuntimeError(f"schema_migrations contains a version gap: {sorted(applied)}")
        known = {migration.version for migration in migrations}
        unknown = applied - known
        if unknown:
            raise RuntimeError(f"database schema is newer than this application: {sorted(unknown)}")

        for migration in migrations:
            if migration.version in applied:
                continue
            self.connection.execute("BEGIN TRANSACTION")
            try:
                for statement in migration.statements:
                    self.connection.execute(statement)
                self.connection.execute(
                    "INSERT INTO schema_migrations(version) VALUES (?)",
                    [migration.version],
                )
                self.connection.execute("COMMIT")
                LOGGER.info("applied database migration %s (%s)", migration.version, migration.name)
            except Exception:
                self.connection.execute("ROLLBACK")
                LOGGER.exception(
                    "database migration %s (%s) failed and was rolled back",
                    migration.version,
                    migration.name,
                )
                raise

"""Persistent content registry for Beacon ingestion runs."""

from __future__ import annotations

import os
import sqlite3
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path


DEFAULT_REGISTRY_PATH = Path(
    os.environ.get(
        "IMPACT_TOOLS_BEACON_REGISTRY",
        Path.home() / ".impact_tools" / "beacon_registry.sqlite3",
    )
).expanduser()


@dataclass(frozen=True)
class DatasetRegistration:
    """A Beacon dataset that has been registered via `ingest dataset`."""

    dataset_id: str
    name: str
    description: str | None
    reference_genome: str
    is_test: bool
    is_synthetic: bool
    granularity: str
    base_dir: str
    registered_at: str


@dataclass(frozen=True)
class VariantIngestion:
    """A single variant ingestion run for a Beacon dataset."""

    vcf_sha256: str
    dataset_id: str
    run_id: str
    vcf_path: str
    vcf_size_bytes: int | None
    variants_count: int | None
    staging_id: str
    old_id: str | None
    status: str
    started_at: str
    completed_at: str | None
    error: str | None


class BeaconRegistry:
    """SQLite-backed registry for Beacon ingest operations."""

    def __init__(self, path: Path = DEFAULT_REGISTRY_PATH) -> None:
        self.path = path.expanduser().resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path)
        self.path.chmod(0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout=30000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS dataset_registrations (
                dataset_id TEXT NOT NULL PRIMARY KEY,
                name TEXT NOT NULL,
                description TEXT,
                reference_genome TEXT NOT NULL,
                is_test INTEGER NOT NULL DEFAULT 0,
                is_synthetic INTEGER NOT NULL DEFAULT 0,
                granularity TEXT NOT NULL,
                base_dir TEXT NOT NULL,
                registered_at TEXT NOT NULL
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS variant_ingestions (
                vcf_sha256 TEXT NOT NULL PRIMARY KEY,
                dataset_id TEXT NOT NULL,
                run_id TEXT NOT NULL,
                vcf_path TEXT NOT NULL,
                vcf_size_bytes INTEGER,
                variants_count INTEGER,
                staging_id TEXT NOT NULL,
                old_id TEXT,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                completed_at TEXT,
                error TEXT
            )
            """
        )
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS processing_claims (
                stage TEXT NOT NULL,
                content_key TEXT NOT NULL,
                owner TEXT NOT NULL,
                claimed_at TEXT NOT NULL,
                PRIMARY KEY (stage, content_key)
            )
            """
        )
        self.connection.commit()

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> BeaconRegistry:
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Dataset registrations
    # ------------------------------------------------------------------

    def record_dataset_registration(
        self,
        *,
        dataset_id: str,
        name: str,
        description: str | None,
        reference_genome: str,
        is_test: bool,
        is_synthetic: bool,
        granularity: str,
        base_dir: str,
    ) -> None:
        """Record (or upsert) a successful dataset registration."""
        registered_at = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO dataset_registrations (
                    dataset_id, name, description, reference_genome,
                    is_test, is_synthetic, granularity, base_dir,
                    registered_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT (dataset_id) DO UPDATE SET
                    name = excluded.name,
                    description = excluded.description,
                    reference_genome = excluded.reference_genome,
                    is_test = excluded.is_test,
                    is_synthetic = excluded.is_synthetic,
                    granularity = excluded.granularity,
                    base_dir = excluded.base_dir,
                    registered_at = excluded.registered_at
                """,
                (
                    dataset_id,
                    name,
                    description,
                    reference_genome,
                    1 if is_test else 0,
                    1 if is_synthetic else 0,
                    granularity,
                    base_dir,
                    registered_at,
                ),
            )

    def find_dataset_registration(
        self,
        dataset_id: str,
    ) -> DatasetRegistration | None:
        row = self.connection.execute(
            """
            SELECT dataset_id, name, description, reference_genome,
                   is_test, is_synthetic, granularity, base_dir,
                   registered_at
            FROM dataset_registrations
            WHERE dataset_id = ?
            """,
            (dataset_id,),
        ).fetchone()
        if row is None:
            return None
        return DatasetRegistration(
            dataset_id=row["dataset_id"],
            name=row["name"],
            description=row["description"],
            reference_genome=row["reference_genome"],
            is_test=bool(row["is_test"]),
            is_synthetic=bool(row["is_synthetic"]),
            granularity=row["granularity"],
            base_dir=row["base_dir"],
            registered_at=row["registered_at"],
        )

    def list_dataset_registrations(
        self,
        limit: int = 100,
    ) -> list[DatasetRegistration]:
        rows = self.connection.execute(
            """
            SELECT dataset_id, name, description, reference_genome,
                   is_test, is_synthetic, granularity, base_dir,
                   registered_at
            FROM dataset_registrations
            ORDER BY registered_at DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return [
            DatasetRegistration(
                dataset_id=row["dataset_id"],
                name=row["name"],
                description=row["description"],
                reference_genome=row["reference_genome"],
                is_test=bool(row["is_test"]),
                is_synthetic=bool(row["is_synthetic"]),
                granularity=row["granularity"],
                base_dir=row["base_dir"],
                registered_at=row["registered_at"],
            )
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Variant ingestions
    # ------------------------------------------------------------------

    def start_variant_ingestion(
        self,
        *,
        vcf_sha256: str,
        dataset_id: str,
        run_id: str,
        vcf_path: str,
        vcf_size_bytes: int | None,
        staging_id: str,
    ) -> None:
        """Record the start of a variant ingestion run with status='RUNNING'."""
        started_at = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                INSERT INTO variant_ingestions (
                    vcf_sha256, dataset_id, run_id, vcf_path,
                    vcf_size_bytes, variants_count, staging_id, old_id,
                    status, started_at, completed_at, error
                )
                VALUES (?, ?, ?, ?, ?, NULL, ?, NULL,
                        'RUNNING', ?, NULL, NULL)
                ON CONFLICT (vcf_sha256) DO UPDATE SET
                    dataset_id = excluded.dataset_id,
                    run_id = excluded.run_id,
                    vcf_path = excluded.vcf_path,
                    vcf_size_bytes = excluded.vcf_size_bytes,
                    staging_id = excluded.staging_id,
                    old_id = NULL,
                    status = 'RUNNING',
                    started_at = excluded.started_at,
                    completed_at = NULL,
                    error = NULL
                """,
                (
                    vcf_sha256,
                    dataset_id,
                    run_id,
                    vcf_path,
                    vcf_size_bytes,
                    staging_id,
                    started_at,
                ),
            )

    def complete_variant_ingestion(
        self,
        *,
        vcf_sha256: str,
        variants_count: int,
        old_id: str | None,
        status: str = "OK",
    ) -> None:
        """Mark a previously started ingestion as completed."""
        completed_at = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                UPDATE variant_ingestions
                SET variants_count = ?,
                    old_id = ?,
                    status = ?,
                    completed_at = ?,
                    error = NULL
                WHERE vcf_sha256 = ?
                """,
                (variants_count, old_id, status, completed_at, vcf_sha256),
            )

    def fail_variant_ingestion(
        self,
        *,
        vcf_sha256: str,
        error: str,
        status: str = "FAILED",
    ) -> None:
        """Mark a previously started ingestion as failed."""
        completed_at = datetime.now(timezone.utc).isoformat()
        with self.connection:
            self.connection.execute(
                """
                UPDATE variant_ingestions
                SET status = ?,
                    completed_at = ?,
                    error = ?
                WHERE vcf_sha256 = ?
                """,
                (status, completed_at, error, vcf_sha256),
            )

    def find_variant_ingestion(
        self,
        vcf_sha256: str,
    ) -> VariantIngestion | None:
        row = self.connection.execute(
            """
            SELECT vcf_sha256, dataset_id, run_id, vcf_path,
                   vcf_size_bytes, variants_count, staging_id, old_id,
                   status, started_at, completed_at, error
            FROM variant_ingestions
            WHERE vcf_sha256 = ?
            """,
            (vcf_sha256,),
        ).fetchone()
        return _row_to_variant_ingestion(row) if row is not None else None

    def list_variant_ingestions(
        self,
        dataset_id: str | None = None,
        limit: int = 100,
    ) -> list[VariantIngestion]:
        """Return the most recent ingestions, optionally filtered by dataset."""
        if dataset_id is None:
            rows = self.connection.execute(
                """
                SELECT vcf_sha256, dataset_id, run_id, vcf_path,
                       vcf_size_bytes, variants_count, staging_id, old_id,
                       status, started_at, completed_at, error
                FROM variant_ingestions
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
        else:
            rows = self.connection.execute(
                """
                SELECT vcf_sha256, dataset_id, run_id, vcf_path,
                       vcf_size_bytes, variants_count, staging_id, old_id,
                       status, started_at, completed_at, error
                FROM variant_ingestions
                WHERE dataset_id = ?
                ORDER BY started_at DESC
                LIMIT ?
                """,
                (dataset_id, limit),
            ).fetchall()
        return [_row_to_variant_ingestion(row) for row in rows]

    # ------------------------------------------------------------------
    # Processing claims (advisory locks)
    # ------------------------------------------------------------------

    def try_claim(
        self,
        stage: str,
        content_key: str,
        *,
        stale_after: timedelta = timedelta(hours=12),
    ) -> str | None:
        """Atomically reserve a stage+content_key and return the owner token.

        Returns None if another owner already holds the claim and it is not
        yet considered stale.
        """
        now = datetime.now(timezone.utc)
        owner = uuid.uuid4().hex
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM processing_claims
                WHERE claimed_at < ?
                """,
                ((now - stale_after).isoformat(),),
            )
            cursor = self.connection.execute(
                """
                INSERT OR IGNORE INTO processing_claims (
                    stage, content_key, owner, claimed_at
                )
                VALUES (?, ?, ?, ?)
                """,
                (stage, content_key, owner, now.isoformat()),
            )
        return owner if cursor.rowcount == 1 else None

    def release_claim(
        self,
        stage: str,
        content_key: str,
        owner: str,
    ) -> None:
        """Release a claim only if the caller is the current owner."""
        with self.connection:
            self.connection.execute(
                """
                DELETE FROM processing_claims
                WHERE stage = ? AND content_key = ? AND owner = ?
                """,
                (stage, content_key, owner),
            )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _row_to_variant_ingestion(row: sqlite3.Row) -> VariantIngestion:
    return VariantIngestion(
        vcf_sha256=row["vcf_sha256"],
        dataset_id=row["dataset_id"],
        run_id=row["run_id"],
        vcf_path=row["vcf_path"],
        vcf_size_bytes=row["vcf_size_bytes"],
        variants_count=row["variants_count"],
        staging_id=row["staging_id"],
        old_id=row["old_id"],
        status=row["status"],
        started_at=row["started_at"],
        completed_at=row["completed_at"],
        error=row["error"],
    )
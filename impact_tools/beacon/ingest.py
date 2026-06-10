"""Beacon dataset and variant ingestion workflows."""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import json
import logging
import platform
import re
import socket
import sys
import time
from pathlib import Path
from importlib import resources
from impact_tools.beacon import ritools

LOGGER = logging.getLogger(__name__)

DATASET_ID_RE = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.-]*$")


@dataclasses.dataclass
class DatasetIngestConfig:
    """Configuration for registering a Beacon dataset."""

    dataset_id: str
    name: str
    description: str
    reference_genome: str = "GRCh38"
    is_test: bool = False
    is_synthetic: bool = False
    base_dir: Path = Path(".")
    granularity: str = "record"
    dry_run: bool = False


@dataclasses.dataclass
class BeaconIngestPaths:
    """Filesystem layout for one Beacon dataset ingestion."""

    dataset_id: str

    # Operational Beacon area managed by impact-tools
    base_dir: Path
    config_dir: Path
    work_dir: Path
    input_dir: Path

    # Dataset-specific working areas
    dataset_config_dir: Path
    dataset_work_dir: Path
    dataset_input_dir: Path

    # Dataset-specific generated artifacts
    datasets_csv: Path
    datasets_json: Path
    ritools_conf: Path

    # Global Beacon deployment files
    datasets_conf_yml: Path
    datasets_permissions_yml: Path


@dataclasses.dataclass
class DatasetIngestResult:
    """Generated artifacts for dataset ingestion."""

    dataset_id: str
    paths: BeaconIngestPaths
    generated_files: list[Path]
    metrics_file: Path | None = None


def validate_dataset_id(dataset_id: str) -> None:
    """Validate Beacon dataset identifier."""
    if not dataset_id:
        raise ValueError("dataset_id cannot be empty")

    if not DATASET_ID_RE.match(dataset_id):
        raise ValueError(
            "Invalid dataset_id. Use only letters, numbers, underscores, dots or hyphens."
        )


def validate_reference_genome(reference_genome: str) -> None:
    """Validate supported reference genome value."""
    if reference_genome not in {"GRCh37", "GRCh38"}:
        raise ValueError("reference_genome must be one of: GRCh37, GRCh38")


def validate_granularity(granularity: str) -> None:
    """Validate Beacon entry type granularity."""
    allowed = {"boolean", "count", "record"}
    if granularity not in allowed:
        raise ValueError(
            f"granularity must be one of: {', '.join(sorted(allowed))}"
        )


def build_dataset_paths(base_dir: Path, dataset_id: str) -> BeaconIngestPaths:
    """Build standard Beacon ingestion paths for one dataset."""
    config_dir = base_dir / "config"
    work_dir = base_dir / "work"
    input_dir = base_dir / "inputs"

    dataset_config_dir = config_dir / dataset_id
    dataset_work_dir = work_dir / dataset_id
    dataset_input_dir = input_dir / dataset_id

    return BeaconIngestPaths(
        dataset_id=dataset_id,
        base_dir=base_dir,
        config_dir=config_dir,
        work_dir=work_dir,
        input_dir=input_dir,
        dataset_config_dir=dataset_config_dir,
        dataset_work_dir=dataset_work_dir,
        dataset_input_dir=dataset_input_dir,
        datasets_csv=dataset_config_dir / "datasets.csv",
        datasets_json=dataset_config_dir / "datasets.json",
        ritools_conf=dataset_config_dir / "conf.py",
        datasets_conf_yml=config_dir / "datasets_conf.yml",
        datasets_permissions_yml=config_dir / "datasets_permissions.yml",
    )



def render_datasets_csv(config: DatasetIngestConfig) -> str:
    """Render datasets.csv content for RI-tools csv_to_bff.py."""
    # csv module avoids malformed CSV if name/description contain commas.
    from io import StringIO

    output = StringIO()
    writer = csv.DictWriter(output, fieldnames=["id", "name", "description"])
    writer.writeheader()
    writer.writerow(
        {
            "id": config.dataset_id,
            "name": config.name,
            "description": config.description,
        }
    )
    return output.getvalue()


def render_ritools_conf(config: DatasetIngestConfig) -> str:
    """Render RI-tools dataset-specific conf.py from template."""
    template = (
        resources.files("impact_tools.beacon")
        .joinpath("templates/ri_tools/conf.py")
        .read_text(encoding="utf-8")
    )

    return template.format(
        reference_genome=config.reference_genome,
        dataset_id=config.dataset_id,
    )


def render_datasets_conf_yml(config: DatasetIngestConfig) -> str:
    """Render Beacon datasets_conf.yml candidate entry."""
    is_synthetic = "true" if config.is_synthetic else "false"
    is_test = "true" if config.is_test else "false"

    return (
        f"{config.dataset_id}:\n"
        f"  isSynthetic: {is_synthetic}\n"
        f"  isTest: {is_test}\n"
    )


def render_datasets_permissions_yml(config: DatasetIngestConfig) -> str:
    """Render Beacon datasets_permissions.yml candidate entry."""
    return (
        f"{config.dataset_id}:\n"
        "  public:\n"
        f"    default_entry_types_granularity: {config.granularity}\n"
    )


def prepare_dataset_artifacts(config: DatasetIngestConfig) -> DatasetIngestResult:
    """Generate dataset registration artifacts without touching MongoDB."""
    validate_dataset_id(config.dataset_id)
    validate_reference_genome(config.reference_genome)
    validate_granularity(config.granularity)

    paths = build_dataset_paths(config.base_dir, config.dataset_id)

    generated_files = [
        paths.datasets_csv,
        paths.ritools_conf,
        paths.datasets_conf_yml,
        paths.datasets_permissions_yml,
    ]

    if config.dry_run:
        return DatasetIngestResult(
            dataset_id=config.dataset_id,
            paths=paths,
            generated_files=generated_files,
        )

    paths.dataset_config_dir.mkdir(parents=True, exist_ok=True)
    paths.dataset_work_dir.mkdir(parents=True, exist_ok=True)
    paths.dataset_input_dir.mkdir(parents=True, exist_ok=True)

    paths.datasets_csv.write_text(render_datasets_csv(config), encoding="utf-8")
    paths.ritools_conf.write_text(render_ritools_conf(config), encoding="utf-8")
    paths.datasets_conf_yml.write_text(
        render_datasets_conf_yml(config),
        encoding="utf-8",
    )
    paths.datasets_permissions_yml.write_text(
        render_datasets_permissions_yml(config),
        encoding="utf-8",
    )
    generate_datasets_json(config, paths)
    generated_files.append(paths.datasets_json)

    return DatasetIngestResult(
        dataset_id=config.dataset_id,
        paths=paths,
        generated_files=generated_files,
    )


def generate_datasets_json(
    config: DatasetIngestConfig,
    paths: BeaconIngestPaths,
) -> None:
    """Generate datasets.json from the prepared datasets.csv."""
    csv_to_bff_cfg = ritools.CsvToBffConfig(
        entity="datasets",
        input_csv=paths.datasets_csv,
        output_dir=paths.dataset_config_dir,
        dataset_id=config.dataset_id,
    )
    ritools.run_csv_to_bff(csv_to_bff_cfg)
    ritools.validate_datasets_json(paths.datasets_json, config.dataset_id)


def _utc_now_iso() -> str:
    """Return current UTC time as ISO-8601 string."""
    return dt.datetime.now(dt.timezone.utc).isoformat()


def _safe_filename_token(value: str) -> str:
    """Return a filesystem-safe token."""
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", value)


def _file_metrics(path: Path) -> dict:
    """Return existence and size metrics for a local file."""
    exists = path.exists()
    return {
        "path": str(path),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else None,
    }


def write_dataset_ingest_metrics(
    *,
    config: DatasetIngestConfig,
    result: DatasetIngestResult | None,
    apply_result: ApplyDatasetResult | None,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
    status: str,
    error: str | None = None,
) -> Path:
    """Write a JSON metrics report for one dataset ingest execution."""
    logs_dir = config.base_dir.resolve() / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    dataset_token = _safe_filename_token(config.dataset_id or "unknown_dataset")
    metrics_file = logs_dir / (
        f"beacon_ingest_dataset_{dataset_token}_{timestamp}.metrics.json"
    )

    report = {
        "workflow": "beacon.ingest.dataset",
        "status": status,
        "error": error,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(duration_seconds, 3),
        "command": " ".join(sys.argv),
        "dataset": {
            "dataset_id": config.dataset_id,
            "name": config.name,
            "description": config.description,
            "reference_genome": config.reference_genome,
            "is_test": config.is_test,
            "is_synthetic": config.is_synthetic,
            "granularity": config.granularity,
            "dry_run": config.dry_run,
        },
        "environment": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cwd": str(Path.cwd()),
        },
        "paths": {
            "base_dir": str(config.base_dir.resolve()),
            "dataset_config_dir": str(result.paths.dataset_config_dir) if result else None,
            "dataset_work_dir": str(result.paths.dataset_work_dir) if result else None,
            "dataset_input_dir": str(result.paths.dataset_input_dir) if result else None,
        },
        "generated_files": (
            [_file_metrics(path) for path in result.generated_files]
            if result is not None
            else []
        ),
        "remote": {
            "mongo_imported": apply_result.imported,
            "mongo_count": apply_result.mongo_count,
            "api_visible": apply_result.api_visible,
        } if apply_result is not None else None,
    }

    metrics_file.write_text(
        json.dumps(report, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    return metrics_file


def upload_dataset_metrics_to_remote(
    *,
    config: DatasetIngestConfig,
    metrics_file: Path,
) -> str:
    """Upload dataset ingest metrics to the remote Beacon log directory."""
    from impact_tools.beacon.remote import (
        load_beacon_deployment_config,
        managed_ssh,
        upload_metrics_file,
    )

    deployment = load_beacon_deployment_config()

    with managed_ssh(deployment.remote) as client:
        return upload_metrics_file(
            client,
            metrics_file,
            deployment.remote,
            config.dataset_id,
        )
    

# ---------------------------------------------------------------------------
# Remote orchestration: apply a prepared dataset to the running Beacon
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class ApplyDatasetResult:
    """Outcome of applying a dataset to the remote Beacon deployment."""

    dataset_id: str
    imported: int
    mongo_count: int
    api_visible: bool


def apply_dataset_to_remote(
    config: DatasetIngestConfig,
    paths: BeaconIngestPaths,
) -> ApplyDatasetResult:
    """Apply a prepared dataset to the remote Beacon deployment.

    Expects `prepare_dataset_artifacts(config)` to have run first, so the
    local files referenced by `paths` already exist.

    Errors are raised verbatim with no automatic rollback. Each remote
    operation leaves traces (backups, idempotent inserts) so partial
    failures can be inspected or replayed by re-running this function.
    """
    from impact_tools.beacon.remote import (
        load_beacon_deployment_config,
        managed_ssh,
        upload_ritools_conf,
        mongo_import_datasets,
        mongo_count_dataset,
        update_yaml_block_remote,
        restart_beacon_api,
        verify_dataset_via_api,
    )

    deployment = load_beacon_deployment_config()

    with managed_ssh(deployment.remote) as client:
        # 1. Upload per-dataset conf.py (used later by `ingest variants`).
        upload_ritools_conf(
            client,
            paths.ritools_conf,
            deployment.remote,
            config.dataset_id,
        )

        # 2. Import datasets.json into MongoDB.
        imported = mongo_import_datasets(
            client,
            deployment.mongo,
            deployment.containers,
            paths.datasets_json,
        )

        # 3. Verify the dataset is in Mongo.
        # (imported can be 0 if the dataset already existed; what matters
        # is the final count.)
        count = mongo_count_dataset(
            client,
            deployment.mongo,
            deployment.containers,
            config.dataset_id,
        )
        if count == 0:
            raise RuntimeError(
                f"Dataset '{config.dataset_id}' not found in MongoDB "
                f"after mongoimport (imported={imported})."
            )

        # 4. Update datasets_conf.yml on the VM.
        update_yaml_block_remote(
            client,
            deployment.remote.datasets_conf_yml,
            config.dataset_id,
            render_datasets_conf_yml(config),
        )

        # 5. Update datasets_permissions.yml on the VM.
        update_yaml_block_remote(
            client,
            deployment.remote.datasets_permissions_yml,
            config.dataset_id,
            render_datasets_permissions_yml(config),
        )

        # 6. Restart the Beacon API so it picks up the YAML changes.
        restart_beacon_api(client, deployment.remote)

        # 7. Verify the dataset is now exposed via the API.
        api_visible = verify_dataset_via_api(
            client,
            deployment.remote,
            config.dataset_id,
        )
        if not api_visible:
            raise RuntimeError(
                f"Dataset '{config.dataset_id}' not visible via API after "
                f"restart. Check beaconprod logs and datasets_conf.yml on "
                f"the VM."
            )

    return ApplyDatasetResult(
        dataset_id=config.dataset_id,
        imported=imported,
        mongo_count=count,
        api_visible=api_visible,
    )


def ingest_dataset(config: DatasetIngestConfig) -> DatasetIngestResult:
    """Register a Beacon dataset end-to-end.

    Two phases:
    1. prepare_dataset_artifacts(config): generate local files.
    2. apply_dataset_to_remote(config, paths): apply them to the running
       Beacon deployment (skipped if config.dry_run is True).

    A metrics JSON file is always written under <base-dir>/logs/.
    """
    started_at = _utc_now_iso()
    started_perf = time.perf_counter()

    result: DatasetIngestResult | None = None
    apply_result: ApplyDatasetResult | None = None
    status = "success"
    error_message: str | None = None
    metrics_file: Path | None = None

    try:
        result = prepare_dataset_artifacts(config)

        if not config.dry_run:
            apply_result = apply_dataset_to_remote(config, result.paths)

    except Exception as exc:
        status = "failed"
        error_message = f"{type(exc).__name__}: {exc}"
        raise

    finally:
        ended_at = _utc_now_iso()
        duration_seconds = time.perf_counter() - started_perf

        try:
            metrics_file = write_dataset_ingest_metrics(
                config=config,
                result=result,
                apply_result=apply_result,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration_seconds,
                status=status,
                error=error_message,
            )
            LOGGER.info("Metrics written: %s", metrics_file)

            if result is not None:
                result.metrics_file = metrics_file

            if not config.dry_run and metrics_file is not None:
                try:
                    remote_metrics_file = upload_dataset_metrics_to_remote(
                        config=config,
                        metrics_file=metrics_file,
                    )
                    LOGGER.info("Remote metrics written: %s", remote_metrics_file)
                except Exception as remote_metrics_exc:  # noqa: BLE001
                    LOGGER.warning(
                        "Could not upload dataset ingest metrics to remote log dir: %s",
                        remote_metrics_exc,
                    )

        except Exception as metrics_exc:  # noqa: BLE001
            LOGGER.warning(
                "Could not write dataset ingest metrics: %s",
                metrics_exc,
            )

    if result is None:
        raise RuntimeError("Dataset ingest failed before producing a result.")

    return result


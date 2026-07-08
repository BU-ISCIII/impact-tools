"""Beacon dataset and variant ingestion workflows."""

from __future__ import annotations

import csv
import dataclasses
import datetime as dt
import gzip
import json
import logging
import platform
import re
import shlex
import socket
import sys
import time

from pathlib import Path

from impact_tools.beacon import ritools
from impact_tools.beacon.config import BeaconDeploymentConfig
from impact_tools.beacon.html_report import write_dataset_ingest_report
from impact_tools.beacon.html_report import write_variant_ingest_report
from impact_tools.beacon.registry import BeaconRegistry
from impact_tools.ega.execution import (
    ProcessMetrics,
    collect_execution_environment,
    finish_process_metrics,
    start_process_metrics,
)


LOGGER = logging.getLogger(__name__)
# Data Use Ontology labels — DUO release 2022-09-15, EBISPOT/DUO (CC-BY)
# https://github.com/EBISPOT/DUO — copied verbatim from duo-basic.owl rdfs:label
DUO_LABELS: dict[str, str] = {
    "DUO:0000001": "data use permission",
    "DUO:0000004": "no restriction",
    "DUO:0000006": "health or medical or biomedical research",
    "DUO:0000007": "disease specific research",
    "DUO:0000011": "population origins or ancestry research only",
    "DUO:0000042": "general research use",
    "DUO:0000012": "research specific restrictions",
    "DUO:0000015": "no general methods research",
    "DUO:0000016": "genetic studies only",
    "DUO:0000018": "not for profit, non commercial use only",
    "DUO:0000019": "publication required",
    "DUO:0000020": "collaboration required",
    "DUO:0000021": "ethics approval required",
    "DUO:0000022": "geographical restriction",
    "DUO:0000024": "publication moratorium",
    "DUO:0000025": "time limit on use",
    "DUO:0000026": "user specific restriction",
    "DUO:0000027": "project specific restriction",
    "DUO:0000028": "institution specific restriction",
    "DUO:0000029": "return to database or resource",
    "DUO:0000043": "clinical care use",
    "DUO:0000044": "population origins or ancestry research prohibited",
    "DUO:0000045": "not for profit organisation use only",
    "DUO:0000046": "non-commercial use only",
}
# DUO ontology release version (matches DUO_LABELS source)
DUO_VERSION = "2022-09-15"

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
    permissions_level: str = "public"
    permissions_email: str | None = None
    duo_codes: tuple[str, ...] = ()
    output_dir: Path | None = None
    dry_run: bool = False
    generate_report: bool = True

    @property
    def logs_dir(self) -> Path:
        return (self.output_dir if self.output_dir is not None else self.base_dir) / "logs"


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


@dataclasses.dataclass
class DatasetIngestResult:
    """Generated artifacts for dataset ingestion."""

    dataset_id: str
    paths: BeaconIngestPaths
    generated_files: list[Path]
    metrics_file: Path | None = None
    report_file: Path | None = None


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


def prepare_dataset_artifacts(config: DatasetIngestConfig) -> DatasetIngestResult:
    """Generate dataset registration artifacts without touching MongoDB."""
    validate_dataset_id(config.dataset_id)
    validate_reference_genome(config.reference_genome)
    validate_granularity(config.granularity)

    paths = build_dataset_paths(config.base_dir, config.dataset_id)

    generated_files = [
        paths.datasets_csv,
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

    generate_datasets_json(config, paths)
    generated_files.append(paths.datasets_json)

    return DatasetIngestResult(
        dataset_id=config.dataset_id,
        paths=paths,
        generated_files=generated_files,
    )


def _inject_duo_into_datasets_json(
    datasets_json: Path,
    duo_codes: tuple[str, ...],
) -> None:
    """Inject dataUseConditions.duoDataUse into the generated datasets.json.

    csv_to_bff generates the base document; DUO is added here because a
    single CSV row cannot express a list of DUO objects (ri-tools emits one
    document per row). Labels come from the embedded DUO_LABELS table.
    """
    documents = json.loads(datasets_json.read_text(encoding="utf-8"))

    duo_block = {
        "duoDataUse": [
            {"id": code, "label": DUO_LABELS[code], "version": DUO_VERSION}
            for code in duo_codes
        ]
    }

    if isinstance(documents, list):
        for doc in documents:
            doc["dataUseConditions"] = duo_block
    else:
        documents["dataUseConditions"] = duo_block

    datasets_json.write_text(
        json.dumps(documents, indent=2, ensure_ascii=False),
        encoding="utf-8",
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
    if config.duo_codes:
        _inject_duo_into_datasets_json(paths.datasets_json, config.duo_codes)
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
    logs_dir = config.logs_dir
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


def write_dataset_ingest_html_report(
    *,
    metrics_file: Path,
) -> Path:
    """Generate an HTML report from a dataset ingest metrics JSON."""
    payload = json.loads(
        metrics_file.read_text(encoding="utf-8")
    )

    report_name = (
        metrics_file.name.removesuffix(".metrics.json")
        + ".report.html"
    )
    report_file = metrics_file.with_name(report_name)

    payload["metrics_file"] = str(metrics_file)
    payload["report_file"] = str(report_file)

    write_dataset_ingest_report(
        report_file,
        payload,
    )

    # Keep the machine-readable report aware of both artifacts.
    metrics_file.write_text(
        json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return report_file


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
    deployment: BeaconDeploymentConfig,
) -> ApplyDatasetResult:
    """Apply a prepared dataset to the Beacon deployment through PyMongo only.

    Expects `prepare_dataset_artifacts(config)` to have run first, so the
    local datasets.json file already exists.

    MongoDB is the only write path:
    - db.datasets stores dataset metadata plus isTest/isSynthetic.
    - db.datasetPermissions stores access permissions.
    """

    from impact_tools.beacon.api import (
        managed_beacon_api,
        verify_dataset_via_api,
    )
    from impact_tools.beacon.mongo import (
        managed_mongo,
        mongo_count_dataset,
        mongo_import_datasets,
        mongo_set_dataset_flags,
        mongo_set_dataset_permissions,
    )

    with managed_mongo(deployment.mongo) as database:
        imported = mongo_import_datasets(
            database,
            paths.datasets_json,
        )

        count = mongo_count_dataset(
            database,
            config.dataset_id,
        )

        if count == 0:
            raise RuntimeError(
                f"Dataset '{config.dataset_id}' not found in MongoDB "
                f"after import (imported={imported})."
            )

        flags_status = mongo_set_dataset_flags(
            database,
            config.dataset_id,
            is_test=config.is_test,
            is_synthetic=config.is_synthetic,
        )

        user_list = None

        if config.permissions_level == "controlled" and config.permissions_email:
            user_list = [
                {
                    "user_e-mail": config.permissions_email,
                    "default_entry_types_granularity": config.granularity,
                }
            ]

        perms_status = mongo_set_dataset_permissions(
            database,
            config.dataset_id,
            level=config.permissions_level,
            granularity=config.granularity,
            user_list=user_list,
        )
        LOGGER.info(
            "Dataset '%s': flags %s, permissions %s",
            config.dataset_id,
            flags_status,
            perms_status,
        )

        mongo_set_dataset_permissions(
            database,
            config.dataset_id,
            level=config.permissions_level,
            granularity=config.granularity,
            user_list=user_list,
        )

    with managed_beacon_api(deployment.api) as api_client:
        api_visible = verify_dataset_via_api(
            api_client,
            config.dataset_id,
        )

    if not api_visible:
        raise RuntimeError(
            f"Dataset '{config.dataset_id}' not visible via API after "
            "MongoDB registration. Check API logs and MongoDB dataset state."
        )

    with BeaconRegistry() as registry:
        registry.record_dataset_registration(
            dataset_id=config.dataset_id,
            name=config.name,
            description=config.description,
            reference_genome=config.reference_genome,
            is_test=config.is_test,
            is_synthetic=config.is_synthetic,
            granularity=config.granularity,
            base_dir=str(config.base_dir),
        )

    return ApplyDatasetResult(
        dataset_id=config.dataset_id,
        imported=imported,
        mongo_count=count,
        api_visible=api_visible,
    )


def ingest_dataset(
    config: DatasetIngestConfig,
    deployment: BeaconDeploymentConfig | None = None,
) -> DatasetIngestResult:
    """Register a Beacon dataset end-to-end.

    Two phases:
    1. prepare_dataset_artifacts(config): generate local files.
    2. apply_dataset_to_remote(config, paths): apply them to the running
       Beacon deployment (skipped if config.dry_run is True).

    A metrics JSON file and an HTML report is always written under <base-dir>/logs/.
    """
    started_at = _utc_now_iso()
    started_perf = time.perf_counter()

    result: DatasetIngestResult | None = None
    apply_result: ApplyDatasetResult | None = None
    status = "success"
    error_message: str | None = None
    metrics_file: Path | None = None
    report_file: Path | None = None

    try:
        result = prepare_dataset_artifacts(config)

        if not config.dry_run:
            if deployment is None:
                raise ValueError(
                    "Beacon deployment configuration is required "
                    "when dataset ingestion is not a dry run."
                )

            apply_result = apply_dataset_to_remote(
                config,
                result.paths,
                deployment,
            )

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
            if config.generate_report:
                try:
                    report_file = write_dataset_ingest_html_report(
                        metrics_file=metrics_file,
                    )
                    LOGGER.info("HTML report written: %s", report_file)

                    if result is not None:
                        result.report_file = report_file

                except Exception as report_exc:  # noqa: BLE001
                    LOGGER.warning(
                        "Could not write dataset ingest HTML report: %s",
                        report_exc,
                )

        except Exception as metrics_exc:  # noqa: BLE001
            LOGGER.warning(
                "Could not write dataset ingest metrics: %s",
                metrics_exc,
            )

    if result is None:
        raise RuntimeError("Dataset ingest failed before producing a result.")

    return result


# ---------------------------------------------------------------------------
# Remote orchestration: Variant ingestion helpers
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class VariantsIngestConfig:
    """Configuration for applying Beacon variants to a remote deployment."""

    dataset_id: str
    reference_genome: str = "GRCh38"
    output_dir: Path | None = None
    vcf: Path | None = None
    vcf_dir: Path | None = None
    cleanup_old: bool = False
    skip_filtering_terms: bool = False
    dry_run: bool = True
    run_profile: str = "local"
    generate_report: bool = True

    @property
    def logs_dir(self) -> Path:
        return (self.output_dir if self.output_dir is not None else Path(".")) / "logs"


@dataclasses.dataclass
class ApplyVariantsResult:
    """Outcome of applying variants to the remote Beacon deployment."""

    dataset_id: str
    staging_id: str
    old_id: str
    local_vcfs: list[str]
    vcf_count: int
    processed_count: int
    inserted_count: int
    skipped_count: int
    mongo_count: int | None = None
    api_visible: bool | None = None
    api_count_valid: bool | None = None
    deleted_old_variants: int | None = None
    process: ProcessMetrics | None = None
    execution: dict | None = None
    manifest_file: Path | None = None
    report_file: Path | None = None


@dataclasses.dataclass
class OldVariantBackup:
    """Existing _old_ variant backup stored in MongoDB."""

    dataset_id: str
    age_days: int
    variants: int
    default_delete: bool


def resolve_variant_inputs(config: VariantsIngestConfig) -> list[Path]:
    """Resolve and validate the VCF files selected for variant ingestion."""
    if (config.vcf is None) == (config.vcf_dir is None):
        raise ValueError("Provide exactly one of vcf or vcf_dir.")

    if config.vcf is not None:
        vcf_file = config.vcf.expanduser().resolve()

        if not vcf_file.is_file():
            raise FileNotFoundError(f"VCF file not found: {vcf_file}")

        if not (
            vcf_file.name.endswith(".vcf")
            or vcf_file.name.endswith(".vcf.gz")
        ):
            raise ValueError(f"Unsupported VCF file: {vcf_file}")

        return [vcf_file]

    vcf_dir = config.vcf_dir.expanduser().resolve()

    if not vcf_dir.is_dir():
        raise NotADirectoryError(f"VCF directory not found: {vcf_dir}")

    vcf_files = sorted(
        path
        for path in vcf_dir.iterdir()
        if path.is_file()
        and (
            path.name.endswith(".vcf")
            or path.name.endswith(".vcf.gz")
        )
    )

    if not vcf_files:
        raise ValueError(
            f"No .vcf or .vcf.gz files found in directory: {vcf_dir}"
        )

    return vcf_files


def build_variant_ingest_payload(
    *,
    config: VariantsIngestConfig,
    result: ApplyVariantsResult,
) -> dict:
    """Build the shared payload for Beacon variant ingest artifacts."""
    return {
        "workflow": "beacon.ingest.variants",
        "execution": result.execution,
        "run": {
            "dataset_id": result.dataset_id,
            "staging_id": result.staging_id,
            "old_id": result.old_id,
            "input_mode": "directory" if config.vcf_dir is not None else "file",
            "vcf_dir": (
                str(config.vcf_dir.expanduser().resolve())
                if config.vcf_dir is not None
                else None
            ),
            "local_vcfs": result.local_vcfs,
            "vcf_files": len(result.local_vcfs),
            "reference_genome": config.reference_genome,
            "output_dir": str(config.output_dir.resolve()) if config.output_dir else None,
            "cleanup_old": config.cleanup_old,
            "skip_filtering_terms": config.skip_filtering_terms,
            "dry_run": config.dry_run,
            "run_profile": config.run_profile,
        },
        "summary": {
            "vcf_count": result.vcf_count,
            "processed_count": result.processed_count,
            "inserted_count": result.inserted_count,
            "skipped_count": result.skipped_count,
            "mongo_count": result.mongo_count,
            "api_visible": result.api_visible,
            "api_count_valid": result.api_count_valid,
            "deleted_old_variants": result.deleted_old_variants,
            "process": (
                dataclasses.asdict(result.process)
                if result.process is not None
                else None
            ),
        },
        "manifest_file": str(result.manifest_file) if result.manifest_file else None,
        "report_file": str(result.report_file) if result.report_file else None,
    }


def build_variant_ingest_artifact_paths(
    *,
    config: VariantsIngestConfig,
    result: ApplyVariantsResult,
) -> tuple[Path, Path]:
    """Build standard artifact paths for one Beacon variant ingest execution."""
    logs_dir = config.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)

    run_token = result.staging_id.replace("/", "_").replace(":", "_")

    manifest_file = logs_dir / f"beacon_ingest_variants_manifest_{run_token}.json"
    report_file = logs_dir / f"beacon_ingest_variants_report_{run_token}.html"

    return manifest_file, report_file


def write_variant_ingest_manifest(
    *,
    config: VariantsIngestConfig,
    result: ApplyVariantsResult,
) -> Path:
    """Write a JSON manifest for one Beacon ingest variants execution."""
    manifest_file, _ = build_variant_ingest_artifact_paths(
        config=config,
        result=result,
    )

    result.manifest_file = manifest_file

    payload = build_variant_ingest_payload(
        config=config,
        result=result,
    )

    manifest_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )

    LOGGER.info("Beacon ingest manifest written: %s", manifest_file)

    return manifest_file


def write_variant_ingest_html_report(
    *,
    config: VariantsIngestConfig,
    result: ApplyVariantsResult,
) -> Path:
    """Write an HTML report for one Beacon variant ingest execution."""

    _, report_file = build_variant_ingest_artifact_paths(
        config=config,
        result=result,
    )

    result.report_file = report_file

    payload = build_variant_ingest_payload(
        config=config,
        result=result,
    )

    write_variant_ingest_report(
        report_file,
        payload,
        include_charts=True,
    )

    LOGGER.info("Beacon ingest HTML report written: %s", report_file)

    return report_file


def write_variant_ingest_artifacts(
    *,
    config: VariantsIngestConfig,
    result: ApplyVariantsResult,
) -> None:
    """Assign artifact paths to `result` and write manifest + HTML report.

    This ensures the manifest JSON contains a non-null `report_file` entry
    and both artifacts reference each other.
    """
    manifest_file, report_file = build_variant_ingest_artifact_paths(
        config=config,
        result=result,
    )

    result.manifest_file = manifest_file
    result.report_file = report_file if config.generate_report else None

    write_variant_ingest_manifest(config=config, result=result)
    if config.generate_report:
        write_variant_ingest_html_report(config=config, result=result)


def count_vcf_variants(vcf_path: Path) -> int:
    """Count variant records in a local VCF/VCF.GZ file.

    Header lines starting with '#' are ignored.
    """
    vcf_path = vcf_path.expanduser().resolve()

    if not vcf_path.is_file():
        raise FileNotFoundError(f"VCF file not found: {vcf_path}")

    opener = gzip.open if vcf_path.suffix == ".gz" else open

    count = 0
    with opener(vcf_path, "rt", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("#"):
                continue
            if line.strip():
                count += 1

    return count


def check_variant_counts(
    *,
    mongo_count: int,
    vcf_count: int,
    dataset_id: str,
) -> None:
    """Verify that MongoDB and VCF variant counts match."""
    if mongo_count != vcf_count:
        raise RuntimeError(
            "Variant count mismatch.\n"
            f"Dataset ID:   {dataset_id}\n"
            f"Mongo count:  {mongo_count}\n"
            f"VCF count:    {vcf_count}"
        )

    LOGGER.info(
        "Variant count check passed for %s: mongo_count=%d, vcf_count=%d",
        dataset_id,
        mongo_count,
        vcf_count,
    )


def list_old_variant_backups(
    database,
    dataset_id: str,
    *,
    older_than_days: int = 7,
) -> list[OldVariantBackup]:
    """List MongoDB backups matching <dataset>_old_<timestamp>."""

    from impact_tools.beacon.mongo import mongo_list_old_backups

    rows = mongo_list_old_backups(
        database,
        dataset_id,
    )

    now = dt.datetime.now()
    backups: list[OldVariantBackup] = []

    for row in rows:
        backup_id = row["datasetId"]
        prefix = f"{dataset_id}_old_"
        timestamp = backup_id[len(prefix):]

        try:
            created_at = dt.datetime.strptime(
                timestamp,
                "%Y%m%d_%H%M%S",
            )
        except ValueError:
            continue

        age_days = max(
            (now - created_at).days,
            0,
        )

        backups.append(
            OldVariantBackup(
                dataset_id=backup_id,
                age_days=age_days,
                variants=int(row["variants"]),
                default_delete=age_days >= older_than_days,
            )
        )

    return backups


def delete_old_variant_backups(
    database,
    backups: list[OldVariantBackup],
    selected_indices: list[int],
) -> dict[str, int]:
    """Delete selected old variant backups from MongoDB.

    selected_indices are 1-based indices from the displayed backups list.
    """

    from impact_tools.beacon.mongo import (
        mongo_delete_dataset_variants,
    )

    deleted: dict[str, int] = {}

    for index in selected_indices:
        backup = backups[index - 1]

        deleted_count = mongo_delete_dataset_variants(
            database,
            backup.dataset_id,
        )

        deleted[backup.dataset_id] = deleted_count

        LOGGER.info(
            "Deleted old variant backup %s (%d variants).",
            backup.dataset_id,
            deleted_count,
        )

    return deleted


def _parse_backup_selection(
    selection: str,
    max_index: int,
) -> list[int]:
    """Parse comma-separated 1-based backup selection like '1,3'."""
    selected: list[int] = []

    for item in selection.split(","):
        item = item.strip()

        if not item:
            continue

        try:
            index = int(item)
        except ValueError as exc:
            raise ValueError(f"Invalid backup selection: {item}") from exc

        if index < 1 or index > max_index:
            raise ValueError(
                f"Backup selection out of range: {index}. "
                f"Valid range is 1-{max_index}."
            )

        if index not in selected:
            selected.append(index)

    return selected


def offer_old_variant_backups_cleanup(
    dataset_id: str,
    *,
    deployment: BeaconDeploymentConfig,
    older_than_days: int = 7,
) -> dict[str, int]:
    """Interactively offer cleanup of old Beacon variant backups."""

    import click

    from impact_tools.beacon.mongo import managed_mongo

    if not click.get_text_stream("stdin").isatty():
        LOGGER.info(
            "Skipping interactive old-backup cleanup because "
            "stdin is not a TTY."
        )
        return {}

    with managed_mongo(deployment.mongo) as database:
        backups = list_old_variant_backups(
            database,
            dataset_id,
            older_than_days=older_than_days,
        )

        if not backups:
            LOGGER.info(
                "No old variant backups found for %s.",
                dataset_id,
            )
            return {}

        click.echo()
        click.echo(
            f"Old variant backups found for {dataset_id}:"
        )
        click.echo()

        default_indices: list[int] = []

        for index, backup in enumerate(
            backups,
            start=1,
        ):
            action = (
                "delete"
                if backup.default_delete
                else "keep"
            )

            if backup.default_delete:
                default_indices.append(index)

            click.echo(f"[{index}] {backup.dataset_id}")
            click.echo(
                f"    age: {backup.age_days} days"
            )
            click.echo(
                f"    variants: {backup.variants}"
            )
            click.echo(
                f"    default action: {action}"
            )
            click.echo()

        selected_indices: list[int] = []

        if default_indices:
            delete_default = click.confirm(
                f"Delete backups older than "
                f"{older_than_days} days?",
                default=True,
            )

            if delete_default:
                default_selection = ",".join(
                    str(index)
                    for index in default_indices
                )

                raw_selection = click.prompt(
                    "Select backups to delete",
                    default=default_selection,
                    show_default=True,
                )

                selected_indices = (
                    _parse_backup_selection(
                        raw_selection,
                        len(backups),
                    )
                )

            else:
                delete_any = click.confirm(
                    "Delete any backup?",
                    default=False,
                )

                if delete_any:
                    raw_selection = click.prompt(
                        "Select backups to delete",
                    )

                    selected_indices = (
                        _parse_backup_selection(
                            raw_selection,
                            len(backups),
                        )
                    )

        else:
            delete_any = click.confirm(
                f"No backups are older than "
                f"{older_than_days} days. "
                "Delete any backup?",
                default=False,
            )

            if delete_any:
                raw_selection = click.prompt(
                    "Select backups to delete",
                )

                selected_indices = (
                    _parse_backup_selection(
                        raw_selection,
                        len(backups),
                    )
                )

        if not selected_indices:
            LOGGER.info(
                "No old variant backups selected for deletion."
            )
            return {}

        return delete_old_variant_backups(
            database,
            backups,
            selected_indices,
        )


def apply_variants_to_remote(
    config: VariantsIngestConfig,
    deployment: BeaconDeploymentConfig,
) -> ApplyVariantsResult:
    """Apply genomic variants to the Beacon deployment.

    MongoDB operations use direct PyMongo connections. SSH is retained
    for filtering-term extraction (container exec) and API restart.

    Safe verify-and-swap workflow:

    1. Build staging_id and old_id.
    2. Validate that the target dataset is registered in MongoDB.
    3. Run RI-tools locally against staging_id via PyMongo.
    4. If dry_run=True, stop after RI-tools.
    5. Count and verify staging variants through PyMongo.
    6. Swap active dataset_id through old_id/staging_id renames.
    7. Reindex through PyMongo; extract filtering terms and restart via SSH.
    8. Verify API visibility and variant count through HTTP.
    9. Optionally delete old_id variants through PyMongo.
    """

    from impact_tools.beacon.api import (
        managed_beacon_api,
        verify_dataset_via_api,
        verify_variant_count_via_api,
    )
    from impact_tools.beacon.mongo import (
        managed_mongo,
        mongo_count_variants,
        mongo_delete_dataset_variants,
        mongo_list_datasets,
        mongo_rename_dataset_id,
        mongo_reindex,
    )


    process_start = start_process_metrics()

    validate_dataset_id(config.dataset_id)
    validate_reference_genome(config.reference_genome)

    if not config.skip_filtering_terms:
        raise RuntimeError(
            "Filtering terms extraction through SSH/container exec has been removed. "
            "Use --skip-filtering-terms for now, or implement filtering-terms extraction "
            "as a local PyMongo/API operation before running variants without that flag."
        )

    vcf_files = resolve_variant_inputs(config)

    LOGGER.info(
        "VCF files selected for ingestion: %d",
        len(vcf_files),
    )

    for vcf_file in vcf_files:
        LOGGER.info("  - %s", vcf_file)

    vcf_counts = {
        vcf_file: count_vcf_variants(vcf_file)
        for vcf_file in vcf_files
    }
    vcf_count = sum(vcf_counts.values())

    LOGGER.info(
        "Total VCF variants selected: %d across %d file(s).",
        vcf_count,
        len(vcf_files),
    )

    for vcf_file, count in vcf_counts.items():
        LOGGER.info(
            "  %s: %d variants",
            vcf_file.name,
            count,
        )

    run_id = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    staging_id = f"{config.dataset_id}_staging_{run_id}"
    old_id = f"{config.dataset_id}_old_{run_id}"

    validate_dataset_id(staging_id)
    validate_dataset_id(old_id)

    # Validate that the dataset is registered using direct PyMongo.
    with managed_mongo(deployment.mongo) as database:
        datasets = mongo_list_datasets(database)

    available_dataset_ids = {
        dataset["id"]
        for dataset in datasets
        if dataset.get("id")
    }

    if config.dataset_id not in available_dataset_ids:
        available_lines = []

        for dataset in datasets:
            dataset_id = dataset.get("id")

            if not dataset_id:
                continue

            dataset_name = dataset.get("name")

            if dataset_name:
                available_lines.append(
                    f"  - {dataset_id} | {dataset_name}"
                )
            else:
                available_lines.append(
                    f"  - {dataset_id}"
                )

        available_text = (
            "\n".join(available_lines)
            if available_lines
            else "  (no registered datasets)"
        )

        raise RuntimeError(
            f"Dataset '{config.dataset_id}' is not registered in MongoDB.\n"
            "Available datasets:\n"
            f"{available_text}\n"
            "Cannot apply variants to a non-existent dataset. "
            "Run `impact-tools beacon ingest dataset` before ingesting "
            "variants."
        )

    LOGGER.info(
        "Dataset registration validated in MongoDB: %s",
        config.dataset_id,
    )

    ritools_results: list[ritools.GenomicVariationsResult] = []

    # Run RI-tools locally against the directly exposed MongoDB.
    for index, local_vcf in enumerate(
        vcf_files,
        start=1,
    ):
        LOGGER.info(
            "Processing VCF %d/%d locally: %s",
            index,
            len(vcf_files),
            local_vcf.name,
        )

        ritools_result = ritools.run_genomic_variations_vcf(
            mongo=deployment.mongo,
            dataset_id=staging_id,
            input_vcf=local_vcf,
            reference_genome=config.reference_genome,
        )

        ritools_results.append(ritools_result)

    expected_mongo_count = sum(
        run.inserted
        for run in ritools_results
    )

    processed_count = sum(
        run.processed
        for run in ritools_results
    )

    skipped_count = sum(
        run.skipped
        for run in ritools_results
    )

    LOGGER.info(
        "RI-tools batch summary: "
        "processed=%d inserted=%d skipped=%d",
        processed_count,
        expected_mongo_count,
        skipped_count,
    )

    if config.dry_run:
        LOGGER.info(
            "Dry run enabled. Stopping after local RI-tools "
            "command for %s.",
            staging_id,
        )

        process_metrics = finish_process_metrics(
            process_start
        )
        execution = collect_execution_environment(
            config.run_profile
        ).as_dict()

        result = ApplyVariantsResult(
            dataset_id=config.dataset_id,
            staging_id=staging_id,
            old_id=old_id,
            local_vcfs=[
                str(path)
                for path in vcf_files
            ],
            vcf_count=vcf_count,
            processed_count=processed_count,
            inserted_count=expected_mongo_count,
            skipped_count=skipped_count,
            process=process_metrics,
            execution=execution,
        )

        write_variant_ingest_artifacts(
            config=config,
            result=result,
        )

        return result

    # Verify staging and perform the datasetId swap through PyMongo.
    with managed_mongo(deployment.mongo) as database:
        mongo_count = mongo_count_variants(
            database,
            staging_id,
        )

        check_variant_counts(
            mongo_count=mongo_count,
            vcf_count=expected_mongo_count,
            dataset_id=staging_id,
        )

        old_count = mongo_rename_dataset_id(
            database,
            old_dataset_id=config.dataset_id,
            new_dataset_id=old_id,
        )

        new_count = mongo_rename_dataset_id(
            database,
            old_dataset_id=staging_id,
            new_dataset_id=config.dataset_id,
        )

    LOGGER.info(
        "Variant dataset swap completed: "
        "active_to_old=%d staging_to_active=%d",
        old_count,
        new_count,
    )

    # Reindex through direct PyMongo — no SSH or container exec needed.
    with managed_mongo(deployment.mongo) as database:
        mongo_reindex(database)

    LOGGER.info(
        "Skipping filtering terms extraction (--skip-filtering-terms)."
    )

    # Verify the final state directly through the Beacon HTTP API.
    with managed_beacon_api(deployment.api) as api_client:
        api_visible = verify_dataset_via_api(
            api_client,
            config.dataset_id,
        )

        if not api_visible:
            raise RuntimeError(
                f"Dataset '{config.dataset_id}' not visible "
                "via API after variant ingest."
            )

        api_count_valid = verify_variant_count_via_api(
            api_client,
            config.dataset_id,
            expected_mongo_count,
        )

    if not api_count_valid:
        raise RuntimeError(
            f"Variant count for dataset '{config.dataset_id}' "
            "does not match via API."
        )

    deleted_old_variants = None

    if config.cleanup_old:
        with managed_mongo(deployment.mongo) as database:
            deleted_old_variants = (
                mongo_delete_dataset_variants(
                    database,
                    old_id,
                )
            )

    process_metrics = finish_process_metrics(
        process_start
    )
    execution = collect_execution_environment(
        config.run_profile
    ).as_dict()

    result = ApplyVariantsResult(
        dataset_id=config.dataset_id,
        staging_id=staging_id,
        old_id=old_id,
        local_vcfs=[
            str(path)
            for path in vcf_files
        ],
        vcf_count=vcf_count,
        processed_count=processed_count,
        inserted_count=expected_mongo_count,
        skipped_count=skipped_count,
        mongo_count=mongo_count,
        api_visible=api_visible,
        api_count_valid=api_count_valid,
        deleted_old_variants=deleted_old_variants,
        process=process_metrics,
        execution=execution,
    )

    write_variant_ingest_artifacts(
        config=config,
        result=result,
    )

    return result
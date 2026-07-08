"""Beacon pgx pipeline — batch-level orchestration.

The unit of execution is a *batch*: a set of samples processed together so
that allele frequencies, HWE, missingness and stratified statistics are
computed over the full cohort at once.

Pipeline overview
-----------------
  DRAGEN gVCF files (per sample)
        |
        v  STEP 1 — GLnexus joint genotyping  (Docker / Singularity)
  <batch_id>.joint.vcf.gz
        |
        v  STEP 2 — sample validation  (bcftools query vs expected_samples.txt)
        |
        +---------------------------+
        |                           |
        v                           v
  AF/QC Snakefile  [STEP 3]   Snakefile.pypgx  [STEP 4 · optional]
        |                           |
        v                           v
  <batch_id>.sites.all.vcf.gz   merged_alleles.csv
  <batch_id>.sites.pass.vcf.gz  merged_genotypes.csv
                                 merged_phenotypes.csv

No bioinformatics tools are called from the login node or the VM.
All subprocess calls happen inside the generated script via Docker
(--executor local) or Singularity inside a SLURM job (--executor hpc).

Upstream reference
------------------
pgx_pilot commit 2026-06-22 ("Set default AF cutoff to 0.0")
https://github.com/Genome-of-Europe/pgx_pilot
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import json
import logging
import platform
import re
import shlex
import socket
import sys
from importlib.resources import files
from pathlib import Path
from typing import Literal


log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Public constants
# ---------------------------------------------------------------------------

PGX_PILOT_COMMIT = "2026-06-22-set-default-af-cutoff-to-0.0"
GLNEXUS_DEFAULT_DOCKER_IMAGE = "ghcr.io/dnanexus-rnd/glnexus:v1.4.1"

# ---------------------------------------------------------------------------
# Type aliases
# ---------------------------------------------------------------------------

Sex = Literal["M", "F", "unknown"]
BatchStatus = Literal[
    "planned",
    "running",
    "af_failed",
    "pypgx_failed",
    "completed_with_warnings",
    "completed",
]
InputMode = Literal["gvcf_dir", "gvcf_list"]
Executor = Literal["local", "hpc"]
ContainerRuntime = Literal["docker", "singularity"]

VALID_SEX_VALUES: frozenset[str] = frozenset({"M", "F", "unknown"})

_BATCH_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}$")
_SAMPLE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._\-]{0,99}$")
_RELEASE_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_\-]{0,62}$")

# ---------------------------------------------------------------------------
# Section 1: Data models
# ---------------------------------------------------------------------------


def is_safe_batch_id(batch_id: str) -> bool:
    return bool(_BATCH_ID_RE.match(batch_id))

def is_safe_release_id(release_id: str) -> bool:
    return bool(_RELEASE_ID_RE.match(release_id))


@dataclasses.dataclass(frozen=True)
class BatchSampleMeta:
    """Sample metadata parsed from samples.tsv (before gVCF path resolution)."""

    sample_id: str
    sex: str
    country_code: str
    batch_id: str = ""
    ancestry_group: str | None = None


@dataclasses.dataclass(frozen=True)
class BatchSample:
    """Sample with resolved gVCF paths, ready for pipeline execution."""

    sample_id: str
    sex: str
    country_code: str
    gvcf: Path
    gvcf_index: Path | None = None
    batch_id: str = ""
    ancestry_group: str | None = None


@dataclasses.dataclass(frozen=True)
class PgxBatch:
    """A cumulative release processed as a single execution unit."""

    release_id: str
    samples: tuple[BatchSample, ...]
    workspace: Path
    ref_fasta: Path

    @property
    def sample_ids(self) -> tuple[str, ...]:
        return tuple(s.sample_id for s in self.samples)

    @property
    def included_batch_ids(self) -> tuple[str, ...]:
        """Original sequencing/processing batches included in this release."""
        return tuple(
            sorted({sample.batch_id for sample in self.samples if sample.batch_id})
        )

    @property
    def joint_vcf_path(self) -> Path:
        return self.workspace / "data" / f"{self.release_id}.joint.vcf.gz"

    @property
    def joint_vcf_index_path(self) -> Path:
        return self.workspace / "data" / f"{self.release_id}.joint.vcf.gz.tbi"

@dataclasses.dataclass(frozen=True)
class SlurmConfig:
    """SLURM submission parameters."""

    time_limit: str = "24:00:00"
    memory: str = "32G"
    cpus: int = 8
    job_name: str = "pgx_pipeline"
    extra_args: tuple[str, ...] = ()


@dataclasses.dataclass
class PgxPipelineConfig:
    """Top-level runtime configuration assembled from CLI and persistent config."""

    release_id: str
    output_dir: Path
    ref_fasta: Path
    pgx_image: Path
    gvcf_dir: Path | None = None
    gvcf_list: Path | None = None
    samples_tsv: Path | None = None
    executor: Executor = "local"
    slurm: SlurmConfig = dataclasses.field(default_factory=SlurmConfig)
    snakemake_jobs: int = 8
    no_pypgx: bool = True  # PyPGx offline bundle not yet validated; opt in with --pypgx
    no_report: bool = False
    prepare: bool = False
    dry_run: bool = False
    force: bool = False
    cleanup_temp: bool = False  # remove results/temp scratch after a successful run
    pgx_pilot_commit: str = PGX_PILOT_COMMIT
    pypgx_snakefile: Path | None = None
    glnexus_image: Path | None = None
    # "gatk" is the standard preset for DRAGEN gVCFs in public GLnexus v1.4.1.
    # If your image includes a "dragen_prod" preset, pass --glnexus-config dragen_prod.
    glnexus_config: str = "gatk"

    @property
    def workspace(self) -> Path:
        return self.output_dir / "pgx_runs" / self.release_id

    @property
    def input_mode(self) -> InputMode:
        if self.gvcf_dir is not None:
            return "gvcf_dir"
        return "gvcf_list"

    @property
    def container_runtime(self) -> ContainerRuntime:
        return "docker" if self.executor == "local" else "singularity"
    
    @property
    def execution_script_path(self) -> Path:
        """Path of the generated launcher script for the selected executor."""
        if self.executor == "hpc":
            return (
                self.workspace
                / "slurm"
                / f"pgx_{self.release_id}.sbatch"
            )

        return (
            self.workspace
            / "run"
            / f"pgx_{self.release_id}.sh"
        )


# ---------------------------------------------------------------------------
# Section 2: Validation
# ---------------------------------------------------------------------------


class ValidationError(ValueError):
    pass


def _validate_sample_id(sample_id: str) -> None:
    if sample_id in {".", ".."} or not _SAMPLE_ID_RE.match(sample_id):
        raise ValidationError(f"Unsafe sample_id {sample_id!r}.")


def parse_samples_tsv(
    tsv_path: Path,
) -> list[BatchSampleMeta]:
    """Parse samples.tsv into BatchSampleMeta objects.

    Columns (tab-separated): sample_id, sex, country_code[, batch_id[, ancestry_group]]
    A header row starting with 'sample_id' is skipped if present.

    Sex must be one of VALID_SEX_VALUES ('M', 'F', 'unknown').
    Non-matching values raise ValidationError; they are never silently coerced.
    ancestry_group is kept as-is; it is never inferred from country_code.
    """
    if not tsv_path.is_file():
        raise ValidationError(f"samples.tsv not found: {tsv_path}")

    records: list[BatchSampleMeta] = []
    seen_ids: dict[str, int] = {}
    errors: list[str] = []
    header_skipped = False

    with tsv_path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue

            fields = line.split("\t")

            if not header_skipped and fields[0].lower() == "sample_id":
                header_skipped = True
                continue

            if len(fields) < 3:
                errors.append(
                    f"Line {lineno}: expected ≥3 tab-separated fields, "
                    f"got {len(fields)}: {line!r}"
                )
                continue

            sample_id = fields[0].strip()
            sex_raw = fields[1].strip()
            country_code = fields[2].strip()
            tsv_batch_id = fields[3].strip() if len(fields) >= 4 else ""
            raw_ancestry = fields[4].strip() if len(fields) >= 5 else ""
            ancestry_group: str | None = raw_ancestry or None

            try:
                _validate_sample_id(sample_id)
            except ValidationError as exc:
                errors.append(f"Line {lineno}: {exc}")
                continue

            if sample_id in seen_ids:
                errors.append(
                    f"Line {lineno}: duplicate sample_id {sample_id!r} "
                    f"(first seen at line {seen_ids[sample_id]})."
                )
                continue
            seen_ids[sample_id] = lineno

            if sex_raw not in VALID_SEX_VALUES:
                errors.append(
                    f"Line {lineno}: unrecognised sex value {sex_raw!r} for sample "
                    f"{sample_id!r}. Accepted: {sorted(VALID_SEX_VALUES)}."
                )
                continue

            if tsv_batch_id and not is_safe_batch_id(tsv_batch_id):
                errors.append(
                    f"Line {lineno}: invalid batch_id {tsv_batch_id!r} "
                    f"for sample {sample_id!r}."
                )
                continue

            records.append(
                BatchSampleMeta(
                    sample_id=sample_id,
                    sex=sex_raw,
                    country_code=country_code,
                    batch_id=tsv_batch_id,
                    ancestry_group=ancestry_group,
                )
            )

    if errors:
        raise ValidationError(
            f"{len(errors)} error(s) in {tsv_path}:\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    if not records:
        raise ValidationError(f"No valid samples found in {tsv_path}")

    return records


def resolve_gvcfs_from_dir(
    gvcf_dir: Path,
    metas: list[BatchSampleMeta],
) -> list[BatchSample]:
    """Find one *.hard-filtered.gvcf.gz per sample under gvcf_dir (recursive).

    Uses a single filesystem traversal to avoid N×rglob calls on NFS.
    Logs a warning for any gVCF present under gvcf_dir that is not declared in
    metas, so the user can audit inclusions and exclusions.
    """
    # Single pass: index all gVCFs found under gvcf_dir.
    found: dict[str, list[Path]] = {}
    for candidate in gvcf_dir.rglob("*.hard-filtered.gvcf.gz"):
        if not candidate.is_file():
            continue
        sample_id = candidate.name.removesuffix(".hard-filtered.gvcf.gz")
        found.setdefault(sample_id, []).append(candidate)

    declared_ids = {m.sample_id for m in metas}
    for sample_id in found:
        if sample_id not in declared_ids:
            for path in found[sample_id]:
                log.warning(
                    "gVCF found for undeclared sample %r: %s "
                    "— add it to samples.tsv or it will be excluded from this batch.",
                    sample_id,
                    path,
                )

    errors: list[str] = []
    samples: list[BatchSample] = []

    for meta in metas:
        matches = found.get(meta.sample_id, [])

        if not matches:
            errors.append(
                f"No gVCF found for sample {meta.sample_id!r} under {gvcf_dir}. "
                f"Expected filename: {meta.sample_id}.hard-filtered.gvcf.gz"
            )
            continue
        if len(matches) > 1:
            errors.append(
                f"Multiple gVCFs found for {meta.sample_id!r}: "
                + ", ".join(str(m) for m in sorted(matches))
            )
            continue

        gvcf = matches[0]
        tbi = Path(f"{gvcf}.tbi")
        if not tbi.is_file():
            log.warning(
                "No .tbi index found for %s. "
                "GLnexus can ingest the gVCF without it, but downstream "
                "bcftools operations may require an index.",
                gvcf,
            )
        samples.append(
            BatchSample(
                sample_id=meta.sample_id,
                sex=meta.sex,
                country_code=meta.country_code,
                gvcf=gvcf,
                gvcf_index=tbi if tbi.is_file() else None,
                batch_id=meta.batch_id,
                ancestry_group=meta.ancestry_group,
            )
        )

    if errors:
        raise ValidationError(
            f"{len(errors)} gVCF resolution error(s):\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    return samples


def resolve_gvcfs_from_list(
    list_path: Path,
    metas: list[BatchSampleMeta],
) -> list[BatchSample]:
    """Parse a gVCF list file and cross-reference against metas from samples.tsv.

    File format (one entry per non-comment line, tab-separated):
        sample_id<TAB>/absolute/path/to/sample.hard-filtered.gvcf.gz

    Each path must be absolute and its .tbi index must exist.
    Every sample in metas must appear exactly once; extras are rejected.
    """
    if not list_path.is_file():
        raise ValidationError(f"gVCF list file not found: {list_path}")

    meta_by_id = {m.sample_id: m for m in metas}
    errors: list[str] = []
    seen: dict[str, int] = {}
    entries: dict[str, tuple[Path, Path | None]] = {}

    with list_path.open(encoding="utf-8") as fh:
        for lineno, raw in enumerate(fh, start=1):
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 2:
                errors.append(f"Line {lineno}: expected sample_id<TAB>path.")
                continue

            sample_id = fields[0].strip()
            gvcf = Path(fields[1].strip())

            if sample_id in seen:
                errors.append(f"Line {lineno}: duplicate sample_id {sample_id!r}.")
                continue
            seen[sample_id] = lineno

            if not gvcf.is_absolute():
                errors.append(f"Line {lineno}: path must be absolute: {gvcf}")
                continue
            if not gvcf.is_file():
                errors.append(f"Line {lineno}: gVCF not found: {gvcf}")
                continue
            tbi = Path(f"{gvcf}.tbi")
            if not tbi.is_file():
                log.warning(
                    "No .tbi index found for %s. "
                    "GLnexus can ingest the gVCF without it, but downstream "
                    "bcftools operations may require an index.",
                    gvcf,
                )
            if sample_id not in meta_by_id:
                errors.append(
                    f"Line {lineno}: {sample_id!r} not declared in samples.tsv."
                )
                continue

            entries[sample_id] = (gvcf, tbi if tbi.is_file() else None)

    missing = set(meta_by_id) - set(entries)
    if missing:
        errors.append(
            f"{len(missing)} sample(s) in samples.tsv have no gVCF entry: "
            + ", ".join(sorted(missing))
        )

    undeclared = set(seen) - set(meta_by_id)
    if undeclared:
        errors.append(
            f"{len(undeclared)} sample(s) in gVCF list not in samples.tsv: "
            + ", ".join(sorted(undeclared))
        )

    if errors:
        raise ValidationError(
            f"{len(errors)} error(s) in {list_path}:\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    return [
        BatchSample(
            sample_id=m.sample_id,
            sex=m.sex,
            country_code=m.country_code,
            gvcf=entries[m.sample_id][0],
            gvcf_index=entries[m.sample_id][1],
            batch_id=m.batch_id,
            ancestry_group=m.ancestry_group,
        )
        for m in metas
        if m.sample_id in entries
    ]



def validate_pre_job(config: PgxPipelineConfig) -> list[str]:
    """Run all pre-submission validations.

    Returns non-fatal warnings. Raises ValidationError on hard failures.
    Never calls subprocess or bioinformatics tools.
    """
    errors: list[str] = []
    warnings: list[str] = []

    if not is_safe_release_id(config.release_id):
        errors.append(
            f"Invalid release_id {config.release_id!r}. "
            "Use only [A-Za-z0-9_-], max 63 chars, start with alphanumeric."
        )

    if not config.ref_fasta.is_file():
        errors.append(f"Reference FASTA not found: {config.ref_fasta}")

    if config.container_runtime == "singularity":
        if not config.pgx_image.is_file():
            errors.append(f"pgx Singularity image not found: {config.pgx_image}")
        elif not str(config.pgx_image).endswith(".sif"):
            warnings.append(
                f"pgx_image {config.pgx_image.name!r} does not have .sif extension."
            )
        if config.glnexus_image is None:
            errors.append(
                "GLnexus Singularity image is required for --executor hpc.\n"
                "  Set it in your config (beacon.pgx.glnexus_image) or pass --glnexus-image.\n"
                "  Download the image with:\n"
                f"    singularity pull glnexus_v1.4.1.sif docker://{GLNEXUS_DEFAULT_DOCKER_IMAGE}"
            )
        elif not config.glnexus_image.is_file():
            errors.append(
                f"GLnexus Singularity image not found: {config.glnexus_image}\n"
                "  Download it with:\n"
                f"    singularity pull {config.glnexus_image.name} docker://{GLNEXUS_DEFAULT_DOCKER_IMAGE}"
            )
    # Docker: images are pulled automatically on first use; no file check needed.

    # Validate mutually exclusive input modes
    if config.gvcf_dir is not None and config.gvcf_list is not None:
        errors.append("--gvcf-dir and --gvcf-list are mutually exclusive.")
    elif config.gvcf_dir is None and config.gvcf_list is None:
        errors.append("One of --gvcf-dir or --gvcf-list is required.")

    if config.gvcf_dir is not None and not config.gvcf_dir.is_dir():
        errors.append(f"--gvcf-dir not found: {config.gvcf_dir}")

    if config.gvcf_list is not None and not config.gvcf_list.is_file():
        errors.append(f"--gvcf-list not found: {config.gvcf_list}")

    if config.samples_tsv is None:
        errors.append("--samples-tsv is required.")
    elif not config.samples_tsv.is_file():
        errors.append(f"--samples-tsv not found: {config.samples_tsv}")

    workspace = config.workspace
    if (
        workspace.exists()
        and config.execution_script_path.exists()
        and not config.force
    ):
        errors.append(
            f"Workspace already exists: {workspace}. "
            "Use --force to overwrite."
        )

    # targets.bed is a controlled workflow resource bundled with impact-tools.
    try:
        bundled_targets = files(
            "impact_tools.beacon.resources"
        ).joinpath("targets.bed")
        bundled_targets.read_bytes()
    except (FileNotFoundError, TypeError):
        errors.append(
            "Required bundled PGx resource is missing: "
            "impact_tools/beacon/resources/targets.bed"
        )

    try:
        config.output_dir.mkdir(parents=True, exist_ok=True)
    except PermissionError:
        errors.append(f"Output directory not writable: {config.output_dir}")

    if errors:
        raise ValidationError(
            f"Pre-job validation failed ({len(errors)} error(s)):\n"
            + "\n".join(f"  {e}" for e in errors)
        )

    return warnings


# ---------------------------------------------------------------------------
# Section 3: Workspace creation
# ---------------------------------------------------------------------------

_CONFIG_YAML_TEMPLATE = """\
# pgx_pilot config — release: {release_id}
# pgx_pilot upstream commit: {pgx_pilot_commit}
# Generated by impact-tools beacon pgx

input_vcf: "data/{release_id}.joint.vcf.gz"
genome_build: "GRCh38"
sample_info: "manifests/samples.tsv"
sample_id_file: "manifests/expected_samples.txt"
regions_bed: "{regions_bed}"
local_resources:
  ref_fasta: "{ref_fasta}"
output_prefix: "{release_id}"
qc_thresholds:
  qual: 30.0
  qd: 2.0
  mq: 40.0
  fs: 60.0
  readpos: -8.0
  hwe: 1.0e-6
  maf: 0.0
  min_dp: 10
  min_gq: 20
  ab_ratio: 0.2
  max_missing: 0.1
"""


def create_workspace(
    batch: PgxBatch,
    config: PgxPipelineConfig,
    *,
    input_mode: InputMode,
) -> None:
    """Create the batch workspace directory tree and write all manifest files."""
    ws = batch.workspace
    (ws / "manifests").mkdir(parents=True, exist_ok=True)
    (ws / "data").mkdir(exist_ok=True)
    (ws / "results" / "intermediate").mkdir(parents=True, exist_ok=True)
    (ws / "results" / "pgx").mkdir(exist_ok=True)
    (ws / "logs").mkdir(exist_ok=True)
    config.execution_script_path.parent.mkdir(
        parents=True,
        exist_ok=True,
    )
    (ws / "resources").mkdir(exist_ok=True)

    # Install bundled workflow files into workspace
    _install_snakefile(ws)
    _install_helper_scripts(ws)

    # Link or copy targets BED into workspace/resources/
    regions_bed = _install_targets_bed(ws)

    # Write config.yaml consumed by the Snakemake pipeline
    (ws / "config.yaml").write_text(
        _CONFIG_YAML_TEMPLATE.format(
            release_id=batch.release_id,
            pgx_pilot_commit=config.pgx_pilot_commit,
            regions_bed=regions_bed,
            ref_fasta=str(batch.ref_fasta),
        ),
        encoding="utf-8",
    )

    # Write manifests/samples.tsv consumed by pgx_pilot (all samples)
    _write_samples_tsv(ws / "manifests" / "samples.tsv", batch.samples)

    # Write expected sample list for in-container header validation
    expected = "\n".join(sorted(batch.sample_ids)) + "\n"
    (ws / "manifests" / "expected_samples.txt").write_text(
        expected, encoding="utf-8"
    )

    # Write gvcfs.list for traceability (sample_id + paths, with header)
    _write_gvcfs_list(ws / "manifests" / "gvcfs.list", batch.samples)
    # Write plain list consumed by glnexus_cli --list (one path per line, no header)
    _write_glnexus_inputs_list(ws / "manifests" / "glnexus_inputs.list", batch.samples)

    # Write batch.json manifest
    _write_batch_json(ws, batch, config, input_mode)

    log.info("Workspace ready: %s", ws)


def _install_snakefile(workspace: Path) -> None:
    src = files("impact_tools.beacon.resources").joinpath("Snakefile")
    dest = workspace / "Snakefile"
    bundled = src.read_text(encoding="utf-8")
    if not dest.exists() or dest.read_text(encoding="utf-8") != bundled:
        dest.write_text(bundled, encoding="utf-8")
        log.debug("Installed Snakefile -> %s", dest)


def _install_helper_scripts(workspace: Path) -> None:
    """Install the Python scripts required by the bundled Snakefile."""
    scripts_dir = workspace / "scripts"
    scripts_dir.mkdir(parents=True, exist_ok=True)

    resource_scripts = files(
        "impact_tools.beacon.resources"
    ).joinpath("scripts")

    script_names = (
        "generate_groups.py",
        "tag_variant_qc.py",
    )

    for script_name in script_names:
        src = resource_scripts.joinpath(script_name)
        dest = scripts_dir / script_name

        try:
            bundled = src.read_text(encoding="utf-8")
        except (FileNotFoundError, TypeError) as exc:
            raise ValidationError(
                f"Required PGx helper script is not bundled: "
                f"impact_tools/beacon/resources/scripts/{script_name}"
            ) from exc

        if not dest.exists() or dest.read_text(encoding="utf-8") != bundled:
            dest.write_text(bundled, encoding="utf-8")
            log.debug("Installed PGx helper script -> %s", dest)


def _install_targets_bed(workspace: Path) -> str:
    """Install the controlled pharmacogene targets BED into the workspace."""
    src = files(
        "impact_tools.beacon.resources"
    ).joinpath("targets.bed")

    dest = workspace / "resources" / "targets.bed"

    try:
        dest.write_bytes(src.read_bytes())
    except (FileNotFoundError, TypeError) as exc:
        raise ValidationError(
            "Required bundled PGx resource is missing: "
            "impact_tools/beacon/resources/targets.bed"
        ) from exc

    log.debug("Installed bundled targets.bed -> %s", dest)

    return "resources/targets.bed"


def _write_samples_tsv(dest: Path, samples: tuple[BatchSample, ...]) -> None:
    lines = ["sample_id\tsex\tcountry_code\tbatch_id\tancestry_group"]
    for s in samples:
        lines.append(
            f"{s.sample_id}\t{s.sex}\t{s.country_code}\t"
            f"{s.batch_id}\t{s.ancestry_group or ''}"
        )
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_gvcfs_list(dest: Path, samples: tuple[BatchSample, ...]) -> None:
    lines = ["# sample_id\tgvcf_path\tgvcf_index_path"]
    for s in samples:
        lines.append(f"{s.sample_id}\t{s.gvcf}\t{s.gvcf_index or ''}")
    dest.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_glnexus_inputs_list(dest: Path, samples: tuple[BatchSample, ...]) -> None:
    """Write one gVCF path per line for glnexus_cli --list (no header, no tabs)."""
    dest.write_text(
        "\n".join(str(s.gvcf) for s in samples) + "\n",
        encoding="utf-8",
    )


def _write_batch_json(
    workspace: Path,
    batch: PgxBatch,
    config: PgxPipelineConfig,
    input_mode: InputMode,
) -> None:
    payload = {
        "release_id": batch.release_id,
        "included_batch_ids": list(batch.included_batch_ids),
        "created_at": dt.datetime.now().astimezone().isoformat(),
        "input_mode": input_mode,
        "sample_count": len(batch.samples),
        "pgx_pilot_commit": config.pgx_pilot_commit,
        "ref_fasta": str(batch.ref_fasta),
        "pgx_image": str(config.pgx_image),
        "executor": config.executor,
        "container_runtime": config.container_runtime,
        "glnexus_image": str(config.glnexus_image) if config.glnexus_image else GLNEXUS_DEFAULT_DOCKER_IMAGE,
        "glnexus_config": config.glnexus_config,
        "no_pypgx": config.no_pypgx,
        "status": "planned",
        "samples": [
            {
                "sample_id": s.sample_id,
                "sex": s.sex,
                "country_code": s.country_code,
                "batch_id": s.batch_id,
                "ancestry_group": s.ancestry_group,
                "gvcf": str(s.gvcf) if s.gvcf else None,
            }
            for s in batch.samples
        ],
    }
    dest = workspace / "manifests" / "batch.json"
    dest.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


# ---------------------------------------------------------------------------
# Section 4: Script generation
# ---------------------------------------------------------------------------


def _container_exec_lines(
    runtime: ContainerRuntime,
    image: str,
    binds: list[tuple[str, str, bool]],
) -> list[str]:
    """Return shell continuation lines for the container invocation up to the image.

    Each line ends with ' \\' for shell line continuation.  The caller appends
    the command and its arguments as further continuation lines.

    Args:
        runtime: "docker" or "singularity".
        image:   Docker image reference or absolute path to a .sif file.
        binds:   List of (host_path, container_path, read_only) mount specs.
    """
    if runtime == "singularity":
        lines: list[str] = ["singularity exec \\", "  --cleanenv \\"]
        for src, dest, ro in binds:
            mount = f"{src}:{dest}:ro" if ro else f"{src}:{dest}"
            lines.append(f"  --bind {shlex.quote(mount)} \\")
    else:
        lines = [
            "docker run --rm \\",
            '  --user "$(id -u):$(id -g)" \\',
            "  -e HOME=/tmp \\",
        ]
        for src, dest, ro in binds:
            mount = f"{src}:{dest}:ro" if ro else f"{src}:{dest}"
            lines.append(f"  -v {shlex.quote(mount)} \\")
    lines.append(f"  {shlex.quote(image)} \\")
    return lines

_SBATCH_HEADER = """\
#!/usr/bin/env bash
# pgx_pilot batch pipeline — generated by impact-tools beacon pgx
# Release: {release_id}
# Generated: {generated_at}
# pgx_pilot commit: {pgx_pilot_commit}
#
#SBATCH --job-name={job_name}
#SBATCH --cpus-per-task={cpus}
#SBATCH --mem={memory}
#SBATCH --time={time_limit}
#SBATCH --output={log_dir}/pgx_{release_id}_%j.out
#SBATCH --error={log_dir}/pgx_{release_id}_%j.err
##SBATCH --partition=YOUR_PARTITION   # uncomment and set your cluster partition
##SBATCH --account=YOUR_ACCOUNT       # uncomment and set your compute account
"""

_LOCAL_HEADER = """\
#!/usr/bin/env bash
# pgx_pilot batch pipeline — generated by impact-tools beacon pgx
# Release: {release_id}
# Generated: {generated_at}
# pgx_pilot commit: {pgx_pilot_commit}
"""


def _pipeline_body_lines(
    batch: PgxBatch,
    config: PgxPipelineConfig,
    *,
    cpus_assignment: str,
    log_to_file: bool = False,
) -> list[str]:
    """Generate the pipeline body shared by local and HPC launchers."""
    ws = batch.workspace

    lines: list[str] = [
        "# --- Variables ---",
        f"WORKSPACE={shlex.quote(str(ws))}",
        f"PGX_IMAGE={shlex.quote(str(config.pgx_image))}",
        f"REF_FASTA={shlex.quote(str(batch.ref_fasta))}",
        f"RELEASE_ID={shlex.quote(batch.release_id)}",
        f"JOINT_VCF={shlex.quote(str(batch.joint_vcf_path))}",
        (
            "EXPECTED_SAMPLES="
            + shlex.quote(
                str(ws / "manifests" / "expected_samples.txt")
            )
        ),
        cpus_assignment,
        "",
    ]

    if log_to_file:
        lines += [
            'LOG_DIR="$WORKSPACE/logs"',
            'mkdir -p "$LOG_DIR"',
            'RUN_LOG="$LOG_DIR/pgx_${RELEASE_ID}.run.log"',
            'echo "Local PGx execution log: $RUN_LOG"',
            'exec > "$RUN_LOG" 2>&1',
            'echo "Started local PGx pipeline at $(date)"',
            'echo "Workspace: $WORKSPACE"',
            'echo "Release ID: $RELEASE_ID"',
            "",
        ]

    # Step 1: Joint genotyping with GLnexus
    lines += _glnexus_lines(batch, config)
    lines.append("")

    # Step 2: Validate samples in the joint VCF
    lines += _in_container_validation_lines(batch, config)
    lines.append("")

    # Step 3: AF/QC Snakemake workflow
    lines += _snakemake_lines(
        batch,
        config,
        snakefile="Snakefile",
    )
    lines.append("")

    # Step 4: Optional PyPGx workflow
    if not config.no_pypgx:
        lines += _pypgx_lines(batch, config)
        lines.append("")

    # Final output validation
    lines += _output_validation_lines(batch, config)

    # Optional cleanup of scratch intermediates. Placed after output validation
    # so that, under `set -euo pipefail`, temp files survive any earlier failure.
    if config.cleanup_temp:
        lines.append("")
        lines += _cleanup_temp_lines(batch)

    return lines


def write_sbatch(
    batch: PgxBatch,
    config: PgxPipelineConfig,
) -> Path:
    """Generate the HPC SLURM launcher using Singularity."""
    slurm = config.slurm
    ws = batch.workspace
    log_dir = ws / "logs"

    header = _SBATCH_HEADER.format(
        release_id=batch.release_id,
        generated_at=dt.datetime.now().astimezone().isoformat(),
        pgx_pilot_commit=config.pgx_pilot_commit,
        job_name=slurm.job_name,
        cpus=slurm.cpus,
        memory=slurm.memory,
        time_limit=slurm.time_limit,
        log_dir=log_dir,
    )

    lines: list[str] = [header]

    for extra in slurm.extra_args:
        lines.append(f"#SBATCH {extra}")

    lines += [
        "",
        "set -euo pipefail",
        "",
        "# module load singularity   # uncomment if required by the HPC",
        "",
    ]

    lines += _pipeline_body_lines(
        batch,
        config,
        cpus_assignment=(
            'CPUS="${SLURM_CPUS_PER_TASK:-'
            + str(slurm.cpus)
            + '}"'
        ),
    )

    script_path = config.execution_script_path
    script_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o750)

    log.info("SBATCH script written: %s", script_path)
    return script_path


def write_local_script(
    batch: PgxBatch,
    config: PgxPipelineConfig,
) -> Path:
    """Generate the local Docker launcher."""
    header = _LOCAL_HEADER.format(
        release_id=batch.release_id,
        generated_at=dt.datetime.now().astimezone().isoformat(),
        pgx_pilot_commit=config.pgx_pilot_commit,
    )

    lines: list[str] = [
        header,
        "",
        "set -euo pipefail",
        "",
    ]

    lines += _pipeline_body_lines(
        batch,
        config,
        cpus_assignment=f"CPUS={config.snakemake_jobs}",
        log_to_file=True
    )

    script_path = config.execution_script_path
    script_path.write_text(
        "\n".join(lines) + "\n",
        encoding="utf-8",
    )
    script_path.chmod(0o750)

    log.info("Local execution script written: %s", script_path)
    return script_path


def write_execution_script(
    batch: PgxBatch,
    config: PgxPipelineConfig,
) -> Path:
    """Generate the launcher corresponding to the selected executor."""
    if config.executor == "hpc":
        return write_sbatch(batch, config)

    return write_local_script(batch, config)


def _glnexus_lines(batch: PgxBatch, config: PgxPipelineConfig) -> list[str]:
    """Return sbatch lines for GLnexus joint genotyping.

    Skips execution if the joint VCF and its index already exist, so the script
    is safe to re-run after a partial failure.
    """
    ws = batch.workspace
    runtime = config.container_runtime
    image = (
        str(config.glnexus_image)
        if config.glnexus_image is not None
        else GLNEXUS_DEFAULT_DOCKER_IMAGE
    )

    # Mount workspace at /workspace.
    # For gvcf_dir mode: one bind covers all samples under the same root.
    # For gvcf_list mode: bind each unique parent dir (paths may be scattered).
    binds: list[tuple[str, str, bool]] = [(str(ws), "/workspace", False)]
    if config.gvcf_dir is not None:
        gvcf_root = str(config.gvcf_dir.resolve())
        binds.append((gvcf_root, gvcf_root, True))
    else:
        for d in sorted({str(s.gvcf.parent) for s in batch.samples}):
            binds.append((d, d, True))

    container_joint_vcf = f"/workspace/data/{batch.release_id}.joint.vcf.gz"
    container_glnexus_inputs = "/workspace/manifests/glnexus_inputs.list"
    container_glnexus_work = "/workspace/data/glnexus_work"
    container_glnexus_bed = "/workspace/resources/targets.bed"

    inner_cmd = (
        f"set -euo pipefail && "
        f"glnexus_cli "
        f"--config {shlex.quote(config.glnexus_config)} "
        f"--dir {shlex.quote(container_glnexus_work)} "
        f"--bed {shlex.quote(container_glnexus_bed)} "
        f"--list {shlex.quote(container_glnexus_inputs)} "
        f"| bcftools view -O z -o {shlex.quote(container_joint_vcf)} && "
        f"bcftools index -t {shlex.quote(container_joint_vcf)}"
    )

    exec_lines = _container_exec_lines(runtime, image, binds)

    lines = [
        "# ================================================================",
        "# STEP 1: Joint genotyping — GLnexus",
        "# ================================================================",
        'if [ -f "$JOINT_VCF" ] && [ -f "${JOINT_VCF}.tbi" ]; then',
        '  echo "Joint VCF already exists — skipping GLnexus."',
        "else",
        '  echo "Running GLnexus joint genotyping..."',
        f'  rm -rf {shlex.quote(str(ws / "data" / "glnexus_work"))}',
        '  rm -f "$JOINT_VCF" "${JOINT_VCF}.tbi"',
    ]
    for line in exec_lines:
        lines.append("  " + line)
    lines.append(f"    bash -c {shlex.quote(inner_cmd)}")
    lines += [
        "fi",
        "",
        '[ -f "$JOINT_VCF" ] || { echo "ERROR: Joint VCF not found after GLnexus: $JOINT_VCF" >&2; exit 1; }',
        '[ -f "${JOINT_VCF}.tbi" ] || { echo "ERROR: index not found: ${JOINT_VCF}.tbi" >&2; exit 1; }',
        'echo "Joint VCF ready: $JOINT_VCF"',
    ]
    return lines


def _in_container_validation_lines(
    batch: PgxBatch,
    config: PgxPipelineConfig,
) -> list[str]:
    ref_parent = batch.ref_fasta.parent
    binds = [
        (str(batch.workspace), str(batch.workspace), False),
        (str(ref_parent), str(ref_parent), True),
    ]
    lines = [
        "# --- In-container validation ---",
        'echo "Validating joint VCF sample list..."',
    ]
    validation_script = r"""
set -euo pipefail
ACTUAL=$(bcftools query -l "$1" | sort)
EXPECTED=$(sort "$2")
if [ "$ACTUAL" != "$EXPECTED" ]; then
  echo "ERROR: sample list mismatch in joint VCF" >&2
  diff <(echo "$EXPECTED") <(echo "$ACTUAL") >&2
  exit 1
fi
echo "Sample validation OK ($(echo "$ACTUAL" | wc -l) samples)"
"""
    lines += _container_exec_lines(config.container_runtime, str(config.pgx_image), binds)
    lines += [
        f"  bash -c {shlex.quote(validation_script)} _ \\",
        '  "$JOINT_VCF" "$EXPECTED_SAMPLES"',
    ]
    return lines


def _snakemake_lines(
    batch: PgxBatch,
    config: PgxPipelineConfig,
    snakefile: str,
) -> list[str]:
    ws = batch.workspace
    ref_parent = batch.ref_fasta.parent
    binds = [
        (str(ws), "/pipeline", False),
        (str(ref_parent), str(ref_parent), True),
    ]
    lines = [
        f"# --- AF/QC pipeline ({snakefile}) ---",
        f'echo "Running Snakemake ({snakefile})..."',
    ]
    lines += _container_exec_lines(config.container_runtime, str(config.pgx_image), binds)
    lines += [
        "  snakemake \\",
        f"    --snakefile /pipeline/{snakefile} \\",
        "    --directory /pipeline \\",
        '    --cores "$CPUS" \\',
        "    --rerun-incomplete \\",
        "    --printshellcmds",
    ]
    return lines


def _pypgx_lines(batch: PgxBatch, config: PgxPipelineConfig) -> list[str]:
    ws = batch.workspace
    ref_parent = batch.ref_fasta.parent
    snakefile_arg = "/pipeline/Snakefile.pypgx"
    if config.pypgx_snakefile is not None:
        snakefile_arg = str(config.pypgx_snakefile)
    binds = [
        (str(ws), "/pipeline", False),
        (str(ref_parent), str(ref_parent), True),
    ]
    lines = [
        "# --- PyPGx pipeline ---",
        "# NOTE: PyPGx bundle (v0.26.0) must be pre-downloaded and available",
        "# inside the container or via a bind-mount. Nodes must not access the internet.",
        'echo "Running Snakemake (Snakefile.pypgx)..."',
    ]
    lines += _container_exec_lines(config.container_runtime, str(config.pgx_image), binds)
    lines += [
        "  snakemake \\",
        f"    --snakefile {shlex.quote(snakefile_arg)} \\",
        "    --directory /pipeline \\",
        '    --cores "$CPUS" \\',
        "    --rerun-incomplete \\",
        "    --printshellcmds",
    ]
    return lines


def _cleanup_temp_lines(batch: PgxBatch) -> list[str]:
    """Remove the scratch intermediates under results/temp (host-side, post-run)."""
    temp_dir = batch.workspace / "results" / "temp"
    return [
        "# --- Cleanup: remove scratch intermediates (--cleanup) ---",
        'echo "Removing intermediate files: results/temp"',
        f"rm -rf {shlex.quote(str(temp_dir))}",
    ]


def _output_validation_lines(
    batch: PgxBatch, config: PgxPipelineConfig
) -> list[str]:
    ws = batch.workspace
    ref_parent = batch.ref_fasta.parent
    release_id = batch.release_id
    binds = [
        (str(ws), "/pipeline", False),
        (str(ref_parent), str(ref_parent), True),
    ]
    lines = [
        "# --- Output validation ---",
        'echo "Validating outputs..."',
    ]
    lines += _container_exec_lines(config.container_runtime, str(config.pgx_image), binds)
    lines += [
        "  bash -c '" + f"""
    set -euo pipefail
    PASS=/pipeline/results/{release_id}.sites.pass.vcf.gz
    ALL=/pipeline/results/{release_id}.sites.all.vcf.gz
    for F in "$PASS" "$ALL"; do
      [ -s "$F" ] || {{ echo "ERROR: missing or empty output: $F" >&2; exit 1; }}
      [ -f "${{F}}.tbi" ] || {{ echo "ERROR: missing index: ${{F}}.tbi" >&2; exit 1; }}
    done
    # Verify sites-only (no sample columns)
    N=$(bcftools query -l "$PASS" | wc -l)
    [ "$N" -eq 0 ] || {{ echo "ERROR: sites.pass.vcf.gz contains $N sample column(s)" >&2; exit 1; }}
    echo "Output validation OK"
  '""",
    ]
    return lines


# ---------------------------------------------------------------------------
# Section 5: Orchestration
# ---------------------------------------------------------------------------


def _derive_batch_id(samples_tsv: Path) -> str:
    """Derive a safe batch_id from the TSV's batch_id column or its filename."""
    # Try batch_id column: if all rows agree on one value, use it
    try:
        with samples_tsv.open(encoding="utf-8") as fh:
            header_skipped = False
            batch_ids: set[str] = set()
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#"):
                    continue
                fields = line.split("\t")
                if not header_skipped and fields[0].lower() == "sample_id":
                    header_skipped = True
                    continue
                if len(fields) >= 4 and fields[3].strip():
                    batch_ids.add(fields[3].strip())
        if len(batch_ids) == 1:
            candidate = batch_ids.pop()
            if is_safe_batch_id(candidate):
                return candidate
    except OSError:
        pass

    # Fall back to TSV filename stem (strip known suffixes)
    stem = samples_tsv.stem  # removes last extension (.tsv)
    for suffix in (".samples", "_samples", ".meta", "_meta", ".batch"):
        if stem.endswith(suffix):
            stem = stem[: -len(suffix)]
            break
    safe = re.sub(r"[^A-Za-z0-9_\-]", "_", stem)
    safe = re.sub(r"^[^A-Za-z0-9]+", "", safe)
    return (safe or "batch")[:63]


def build_batch(config: PgxPipelineConfig) -> PgxBatch:
    """Validate inputs and build a PgxBatch from the pipeline config."""
    assert config.samples_tsv is not None  # validated by validate_pre_job

    metas = parse_samples_tsv(config.samples_tsv)

    if config.input_mode == "gvcf_dir":
        assert config.gvcf_dir is not None
        samples = tuple(resolve_gvcfs_from_dir(config.gvcf_dir, metas))
    else:
        assert config.gvcf_list is not None
        samples = tuple(resolve_gvcfs_from_list(config.gvcf_list, metas))

    return PgxBatch(
        release_id=config.release_id,
        samples=samples,
        workspace=config.workspace,
        ref_fasta=config.ref_fasta,
    )


def plan_batch(config: PgxPipelineConfig) -> tuple[PgxBatch, Path]:
    """Validate, build workspace and generate sbatch script. No subprocess calls.

    Returns (batch, execution_script_path).
    """
    import shutil

    warnings = validate_pre_job(config)
    for w in warnings:
        log.warning(w)

    if config.workspace.exists() and config.force:
        shutil.rmtree(config.workspace)
        log.info("Removed existing workspace (--force): %s", config.workspace)

    batch = build_batch(config)

    log.info(
        "Release %r: %d sample(s), input_mode=%s, executor=%s",
        batch.release_id,
        len(batch.samples),
        config.input_mode,
        config.executor,
    )

    create_workspace(batch, config, input_mode=config.input_mode)

    script_path = write_execution_script(batch, config)

    return batch, script_path


# ---------------------------------------------------------------------------
# Section 7: Metrics and reporting
# ---------------------------------------------------------------------------


def write_pgx_metrics(
    *,
    config: PgxPipelineConfig,
    batch: PgxBatch | None,
    execution_script_path: Path | None,
    job_id: str | None,
    status: BatchStatus,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
    warnings: list[str],
    error: str | None,
) -> Path:
    """Write a JSON metrics file for one batch PGx execution."""
    logs_dir = config.workspace / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    metrics_file = logs_dir / f"pgx_{config.release_id}_{timestamp}.metrics.json"

    outputs: dict = {}
    if batch is not None:
        ws = batch.workspace
        outputs = {
            "sites_all": str(ws / "results" / f"{batch.release_id}.sites.all.vcf.gz"),
            "sites_pass": str(ws / "results" / f"{batch.release_id}.sites.pass.vcf.gz"),
            "intermediate": str(
                ws / "results" / "intermediate"
                / f"{batch.release_id}.full_sample_data.vcf.gz"
            ),
            "pgx_alleles": str(ws / "results" / "pgx" / "merged_alleles.csv"),
            "pgx_genotypes": str(ws / "results" / "pgx" / "merged_genotypes.csv"),
            "pgx_phenotypes": str(ws / "results" / "pgx" / "merged_phenotypes.csv"),
        }

    report = {
        "workflow": "beacon.pgx",
        "release_id": config.release_id,
        "status": status,
        "error": error,
        "warnings": warnings,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(duration_seconds, 3),
        "command": " ".join(sys.argv),
        "job_id": job_id,
        "execution_script": (
            str(execution_script_path)
            if execution_script_path is not None
            else None
        ),
        "sbatch_script": (
            str(execution_script_path)
            if execution_script_path is not None
            and config.executor == "hpc"
            else None
        ),
        "config": {
            "release_id": config.release_id,
            "input_mode": config.input_mode,
            "output_dir": str(config.output_dir),
            "ref_fasta": str(config.ref_fasta),
            "pgx_image": str(config.pgx_image),
            "executor": config.executor,
            "container_runtime": config.container_runtime,
            "glnexus_image": str(config.glnexus_image) if config.glnexus_image else GLNEXUS_DEFAULT_DOCKER_IMAGE,
            "glnexus_config": config.glnexus_config,
            "no_pypgx": config.no_pypgx,
            "pgx_pilot_commit": config.pgx_pilot_commit,
            "slurm": dataclasses.asdict(config.slurm),
        },
        "environment": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
        },
        "summary": {
            "sample_count": len(batch.samples) if batch else 0,
            "sample_ids": list(batch.sample_ids) if batch else [],
            "included_batch_ids": list(batch.included_batch_ids) if batch else [],
        },
        "outputs": outputs,
        "metrics_file": str(metrics_file),
        "report_file": None,
    }

    metrics_file.write_text(
        json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return metrics_file


def write_pgx_html_report(*, metrics_file: Path) -> Path:
    """Generate an HTML report from a PGx metrics JSON."""
    from impact_tools.beacon.html_report import write_pgx_report  # lazy to avoid circular

    payload = json.loads(metrics_file.read_text(encoding="utf-8"))

    report_name = metrics_file.name.removesuffix(".metrics.json") + ".report.html"
    report_file = metrics_file.with_name(report_name)

    payload["metrics_file"] = str(metrics_file)
    payload["report_file"] = str(report_file)

    write_pgx_report(report_file, payload)

    metrics_file.write_text(
        json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    return report_file

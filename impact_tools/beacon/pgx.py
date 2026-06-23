"""Beacon pgx_pilot workspace preparation and execution."""

from __future__ import annotations

import datetime as dt
import json
import platform
import socket
import sys
import dataclasses
import logging
import shlex
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from impact_tools.beacon.html_report import write_pgx_report
from pathlib import Path
from typing import Literal
from importlib.resources import files

log = logging.getLogger(__name__)

Sex = Literal["M", "F"]

PGX_IMAGE = "goe/pgx-pipeline:latest"
BCFTOOLS_IMAGE = "docker.io/staphb/bcftools:1.21"
DEFAULT_SEX_AMBIGUOUS_MIN = 5000
DEFAULT_SEX_AMBIGUOUS_MAX = 7000

def _fmt_size(path: Path) -> str:
    """Return human-readable file size, or 'N/A' if the file is missing."""
    try:
        size = path.stat().st_size
    except OSError:
        return "N/A"
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"


_CONFIG_YAML_TEMPLATE = """\
input_vcf: "data/{sample_id}.vcf.gz"
sample_id: "{sample_id}"
sample_id_file: "data/sample_id.txt"
genome_build: "GRCh38"
country_code: "{country_code}"
sample_info: "data/samples.tsv"

resources:
  ref_fasta_url: "https://hgdownload.soe.ucsc.edu/goldenPath/hg38/bigZips/hg38.fa.gz"

regions_bed: "resources/targets.bed"
output_prefix: "{sample_id}"
qc_thresholds:
  qual: 30.0
  qd: 2.0
  mq: 40.0
  fs: 60.0
  readpos: -8.0
  hwe: 1.0e-6
  maf: 0.01
  min_dp: 10
  min_gq: 20
  ab_ratio: 0.2
  max_missing: 0.1
"""


@dataclasses.dataclass
class SampleRecord:
    sample_id: str
    sex: Sex
    country_code: str
    vcf_basename: str = ""


@dataclasses.dataclass(frozen=True)
class SampleSource:
    """A sample contained in a lifted single- or multi-sample VCF."""

    sample_id: str
    vcf_basename: str


@dataclasses.dataclass
class PgxConfig:
    base_dir: Path
    country_code: str = "ES"
    sex_ambiguous_min: int = DEFAULT_SEX_AMBIGUOUS_MIN
    sex_ambiguous_max: int = DEFAULT_SEX_AMBIGUOUS_MAX
    bcftools_image: str = BCFTOOLS_IMAGE
    pgx_image: str = PGX_IMAGE
    pgx_repo: Path | None = None
    snakemake_jobs: int = 8
    workers: int = 4
    vcf: tuple[Path, ...] = ()
    vcf_dir: Path | None = None
    output_dir: Path | None = None
    run_profile: str = "local"

    @property
    def liftover_dir(self) -> Path:
        return self.base_dir / "liftover"

    @property
    def logs_dir(self) -> Path:
        return (self.output_dir if self.output_dir is not None else self.base_dir) / "logs"

    @property
    def pgx_resources_dir(self) -> Path:
        return self.base_dir / "pgx_resources"

    def resolve_vcf(self, vcf_basename: str) -> Path:
        """Return the full path for a VCF given its basename.

        Resolution order: --vcf → --vcf-dir → liftover dir.
        """
        if self.vcf:
            for p in self.vcf:
                if p.name == vcf_basename:
                    return p
        if self.vcf_dir is not None:
            return self.vcf_dir / vcf_basename
        return self.liftover_dir / vcf_basename

    @property
    def pgx_runs_dir(self) -> Path:
        return self.base_dir / "pgx_runs"

    @property
    def samples_tsv(self) -> Path:
        return self.base_dir / "inputs" / "samples.tsv"

    @property
    def snakefile(self) -> Path:
        return self.pgx_runs_dir / "Snakefile"


@dataclasses.dataclass
class SexInferenceResult:
    sample_id: str
    vcf_basename: str
    n_chry: int
    sex: Sex | None  # None = ambiguous zone, requires manual input


@dataclasses.dataclass
class WorkspaceResult:
    sample_id: str
    workspace: Path
    status: str  # "ok", "warn", "error"
    duration_seconds: float | None = None
    error: str | None = None


@dataclasses.dataclass
class PgxRunResult:
    sample_id: str
    output_all: Path | None
    output_pass: Path | None
    status: str  # "ok", "warn", "error"
    duration_seconds: float | None = None
    return_code: int | None = None
    error: str | None = None


@dataclasses.dataclass
class PgxPipelineResult:
    prepare_results: list[WorkspaceResult]
    run_results: list[PgxRunResult]
    metrics_file: Path | None = None
    report_file: Path | None = None

    @property
    def failed(self) -> int:
        return sum(
            1
            for result in [*self.prepare_results, *self.run_results]
            if result.status == "error"
        )

    @property
    def warned(self) -> int:
        return sum(
            1
            for result in [*self.prepare_results, *self.run_results]
            if result.status == "warn"
        )

    @property
    def succeeded(self) -> int:
        return sum(
            1
            for result in [*self.prepare_results, *self.run_results]
            if result.status == "ok"
        )


def _pgx_file_metrics(path: Path | None) -> dict | None:
    """Return existence and size metrics for one PGx output file."""
    if path is None:
        return None

    exists = path.exists()

    return {
        "path": str(path),
        "exists": exists,
        "size_bytes": path.stat().st_size if exists else None,
    }


def write_pgx_metrics(
    *,
    config: PgxConfig,
    discovered_sources: list[SampleSource],
    existing_records: list[SampleRecord],
    new_records: list[SampleRecord],
    inferences: list[SexInferenceResult | None],
    result: PgxPipelineResult | None,
    prepare_only: bool,
    run_only: bool,
    started_at: str,
    ended_at: str,
    duration_seconds: float,
    status: str,
    error: str | None = None,
) -> Path:
    """Write a JSON metrics report for one Beacon PGx execution."""
    logs_dir = config.logs_dir
    logs_dir.mkdir(parents=True, exist_ok=True)

    timestamp = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
    metrics_file = logs_dir / f"beacon_pgx_{timestamp}.metrics.json"

    all_records = [*existing_records, *new_records]

    source_by_sample = {
        source.sample_id: source
        for source in discovered_sources
    }
    record_by_sample = {
        record.sample_id: record
        for record in all_records
    }
    inference_by_sample = {
        inference.sample_id: inference
        for inference in inferences
        if inference is not None
    }

    prepare_results = (
        result.prepare_results
        if result is not None
        else []
    )
    run_results = (
        result.run_results
        if result is not None
        else []
    )

    prepare_by_sample = {
        item.sample_id: item
        for item in prepare_results
    }
    run_by_sample = {
        item.sample_id: item
        for item in run_results
    }

    sample_ids = [
        source.sample_id
        for source in discovered_sources
    ]

    for record in all_records:
        if record.sample_id not in sample_ids:
            sample_ids.append(record.sample_id)

    existing_sample_ids = {
        record.sample_id
        for record in existing_records
    }

    samples = []

    for sample_id in sample_ids:
        source = source_by_sample.get(sample_id)
        record = record_by_sample.get(sample_id)
        inference = inference_by_sample.get(sample_id)
        workspace = prepare_by_sample.get(sample_id)
        pgx_run = run_by_sample.get(sample_id)

        if sample_id in existing_sample_ids:
            sex_resolution = "existing"
        elif inference is None:
            sex_resolution = "failed"
        elif inference.sex is None and record is not None:
            sex_resolution = "manual"
        else:
            sex_resolution = "automatic"

        samples.append(
            {
                "sample_id": sample_id,
                "vcf_basename": (
                    source.vcf_basename
                    if source is not None
                    else (
                        record.vcf_basename
                        if record is not None
                        else None
                    )
                ),
                "sex": record.sex if record is not None else None,
                "country_code": (
                    record.country_code
                    if record is not None
                    else None
                ),
                "sex_inference": {
                    "status": sex_resolution,
                    "n_chry": (
                        inference.n_chry
                        if inference is not None
                        else None
                    ),
                    "inferred_sex": (
                        inference.sex
                        if inference is not None
                        else None
                    ),
                },
                "workspace": (
                    {
                        "status": workspace.status,
                        "path": str(workspace.workspace),
                        "duration_seconds": (
                            round(workspace.duration_seconds, 3)
                            if workspace.duration_seconds is not None
                            else None
                        ),
                        "error": workspace.error,
                    }
                    if workspace is not None
                    else None
                ),
                "pgx_run": (
                    {
                        "status": pgx_run.status,
                        "duration_seconds": (
                            round(pgx_run.duration_seconds, 3)
                            if pgx_run.duration_seconds is not None
                            else None
                        ),
                        "return_code": pgx_run.return_code,
                        "error": pgx_run.error,
                        "output_all": _pgx_file_metrics(
                            pgx_run.output_all
                        ),
                        "output_pass": _pgx_file_metrics(
                            pgx_run.output_pass
                        ),
                    }
                    if pgx_run is not None
                    else None
                ),
            }
        )

    if prepare_only:
        mode = "prepare"
    elif run_only:
        mode = "run"
    else:
        mode = "full"

    report = {
        "workflow": "beacon.pgx",
        "status": status,
        "error": error,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(duration_seconds, 3),
        "command": " ".join(sys.argv),
        "config": {
            "base_dir": str(config.base_dir.resolve()),
            "country_code": config.country_code,
            "sex_ambiguous_min": config.sex_ambiguous_min,
            "sex_ambiguous_max": config.sex_ambiguous_max,
            "bcftools_image": config.bcftools_image,
            "pgx_image": config.pgx_image,
            "pgx_repo": (
                str(config.pgx_repo)
                if config.pgx_repo is not None
                else None
            ),
            "snakemake_jobs": config.snakemake_jobs,
            "workers": config.workers,
            "mode": mode,
        },
        "environment": {
            "hostname": socket.gethostname(),
            "platform": platform.platform(),
            "python": sys.version.split()[0],
            "cwd": str(Path.cwd()),
        },
        "summary": {
            "lifted_vcfs": len(
                {
                    source.vcf_basename
                    for source in discovered_sources
                }
            ),
            "samples_discovered": len(discovered_sources),
            "existing_samples": len(existing_records),
            "new_samples": len(new_records),
            "sex_inference_attempted": len(inferences),
            "sex_inference_failed": sum(
                inference is None
                for inference in inferences
            ),
            "sex_ambiguous": sum(
                inference is not None
                and inference.sex is None
                for inference in inferences
            ),
            "prepare_steps": len(prepare_results),
            "run_steps": len(run_results),
            "succeeded": (
                result.succeeded
                if result is not None
                else 0
            ),
            "warned": (
                result.warned
                if result is not None
                else 0
            ),
            "failed": (
                result.failed
                if result is not None
                else 0
            ),
        },
        "samples": samples,
        "metrics_file": str(metrics_file),
        "report_file": None,
    }

    metrics_file.write_text(
        json.dumps(
            report,
            indent=2,
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    return metrics_file


def write_pgx_html_report(
    *,
    metrics_file: Path,
) -> Path:
    """Generate an HTML report from a PGx metrics JSON."""
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

    write_pgx_report(
        report_file,
        payload,
    )

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
# Discovery and samples.tsv I/O
# ---------------------------------------------------------------------------

def discover_lifted_vcfs(base_dir: Path) -> list[Path]:
    """Return sorted list of *.GRCh38.clean.vcf.gz under <base_dir>/liftover/."""
    liftover_dir = base_dir / "liftover"
    if not liftover_dir.exists():
        return []
    return sorted(liftover_dir.glob("*.GRCh38.clean.vcf.gz"))


def _discover_vcfs_from_config(config: PgxConfig) -> list[Path]:
    """Resolve VCF inputs. Priority: --vcf > --vcf-dir > liftover/."""
    if config.vcf:
        return sorted(config.vcf)
    if config.vcf_dir is not None:
        d = config.vcf_dir
        if not d.exists():
            return []
        return sorted(
            p for p in d.iterdir()
            if p.is_file() and (p.suffix == ".vcf" or p.name.endswith(".vcf.gz"))
        )
    return discover_lifted_vcfs(config.base_dir)


def _docker_data_path(config: PgxConfig, path: Path) -> str:
    """Translate a path below base_dir to its /data path inside Docker."""
    relative = path.resolve().relative_to(config.base_dir.resolve())
    return f"/data/{relative.as_posix()}"


def list_vcf_samples(config: PgxConfig, vcf: Path) -> list[str]:
    """Return every sample declared in a VCF header."""
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{config.base_dir.resolve()}:/data:ro",
        config.bcftools_image,
        "bcftools", "query", "-l",
        _docker_data_path(config, vcf),
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if proc.returncode != 0:
        raise ValueError(
            f"Could not read samples from {vcf}\n"
            f"STDERR: {proc.stderr.strip()}"
        )

    samples = [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip()
    ]

    if not samples:
        raise ValueError(f"No samples found in VCF: {vcf}")

    if len(samples) != len(set(samples)):
        raise ValueError(f"Duplicated sample names inside VCF: {vcf}")

    return samples


def discover_lifted_samples(config: PgxConfig) -> list[SampleSource]:
    """Expand VCF files into one SampleSource per contained sample.

    Sources are resolved from --vcf, --vcf-dir, or liftover dir (in that order).
    """
    vcfs = _discover_vcfs_from_config(config)

    if not vcfs:
        if config.vcf:
            raise ValueError(f"No VCF files resolved from the --vcf arguments provided.")
        if config.vcf_dir is not None:
            raise ValueError(f"No VCF files found in --vcf-dir: {config.vcf_dir}")
        raise ValueError(
            f"No *.GRCh38.clean.vcf.gz files found in {config.liftover_dir}. "
            "Pass --vcf or --vcf-dir to specify VCF inputs explicitly."
        )

    sources: list[SampleSource] = []
    seen: dict[str, Path] = {}

    for vcf in vcfs:
        samples = list_vcf_samples(config, vcf)

        log.info(
            "%s: %d sample(s)",
            vcf.name,
            len(samples),
        )

        for sample_id in samples:
            if (
                not sample_id
                or "/" in sample_id
                or "\\" in sample_id
                or sample_id in {".", ".."}
            ):
                raise ValueError(
                    f"Unsafe sample identifier {sample_id!r} in {vcf}"
                )

            previous = seen.get(sample_id)
            if previous is not None:
                raise ValueError(
                    f"Sample {sample_id!r} occurs in more than one VCF:\n"
                    f"  - {previous}\n"
                    f"  - {vcf}"
                )

            seen[sample_id] = vcf
            sources.append(
                SampleSource(
                    sample_id=sample_id,
                    vcf_basename=vcf.name,
                )
            )

    log.info(
        "Discovered %d sample(s) across %d lifted VCF file(s).",
        len(sources),
        len(vcfs),
    )

    return sources


def read_samples_tsv(tsv_path: Path) -> list[SampleRecord]:
    """Parse samples.tsv (sample_id<TAB>sex<TAB>country_code) into SampleRecord objects."""
    records: list[SampleRecord] = []
    with tsv_path.open() as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            fields = line.split("\t")
            if len(fields) < 2:
                continue
            sample_id = fields[0]
            sex: Sex = "M" if fields[1].upper() == "M" else "F"
            country_code = fields[2] if len(fields) >= 3 else "ES"
            vcf_basename = fields[3] if len(fields) >= 4 else ""
            records.append(SampleRecord(sample_id=sample_id, sex=sex, country_code=country_code, vcf_basename=vcf_basename))
    return records


def append_sample_to_tsv(tsv_path: Path, record: SampleRecord) -> None:
    """Append one sample line to samples.tsv, creating the file with a header if needed."""
    if not tsv_path.exists():
        tsv_path.parent.mkdir(parents=True, exist_ok=True)
        tsv_path.write_text(
            "# samples.tsv — IMPaCT cohort sample metadata\n"
            "# sample_id<TAB>sex<TAB>country_code<TAB>vcf_basename\n"
            "# sex: M or F (inferred from non-ref chrY variant count)\n"
        )
    with tsv_path.open("a") as fh:
        fh.write(f"{record.sample_id}\t{record.sex}\t{record.country_code}\t{record.vcf_basename}\n")


# ---------------------------------------------------------------------------
# Sex inference
# ---------------------------------------------------------------------------

def count_chry_variants(
    config: PgxConfig,
    source: SampleSource,
) -> SexInferenceResult | None:
    """Count non-reference chrY genotypes for one selected sample."""
    vcf = config.resolve_vcf(source.vcf_basename)
    docker_vcf = _docker_data_path(config, vcf)

    script = r"""
VCF="$1"
SAMPLE="$2"

n=$(
    bcftools query \
        -s "$SAMPLE" \
        -r chrY \
        -f '[%GT\n]' \
        "$VCF" 2>/dev/null |
    grep -cvE '^(0[/|]0|\.[/|]\.|\.)(\t|$)' || true
)

printf '%s\n' "$n"
"""

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{config.base_dir.resolve()}:/data:ro",
        config.bcftools_image,
        "bash", "-c",
        script,
        "_",
        docker_vcf,
        source.sample_id,
    ]

    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        check=False,
    )

    if proc.returncode != 0 or not proc.stdout.strip():
        log.error(
            "chrY count failed for %s in %s: %s",
            source.sample_id,
            source.vcf_basename,
            proc.stderr.strip(),
        )
        return None

    try:
        n_chry = int(proc.stdout.strip())
    except ValueError:
        log.error(
            "Non-integer chrY count for %s: %r",
            source.sample_id,
            proc.stdout.strip(),
        )
        return None

    return SexInferenceResult(
        sample_id=source.sample_id,
        vcf_basename=source.vcf_basename,
        n_chry=n_chry,
        sex=infer_sex(n_chry, config),
    )


def infer_sex(
    n_chry: int,
    config: PgxConfig,
) -> Sex | None:
    """Infer sex from the number of non-reference chrY genotypes."""
    if n_chry > config.sex_ambiguous_max:
        return "M"

    if n_chry < config.sex_ambiguous_min:
        return "F"

    return None


def infer_sex_batch(
    config: PgxConfig,
    sources: list[SampleSource],
) -> list[SexInferenceResult | None]:
    """Infer sex independently for every sample in every lifted VCF."""
    if config.workers <= 1 or len(sources) <= 1:
        return [count_chry_variants(config, source) for source in sources]

    ordered: dict[int, SexInferenceResult | None] = {}

    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        future_to_idx = {
            pool.submit(count_chry_variants, config, source): i
            for i, source in enumerate(sources)
        }

        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            source = sources[i]

            try:
                ordered[i] = future.result()
            except Exception as exc:
                log.error(
                    "Sex inference error for %s: %s",
                    source.sample_id,
                    exc,
                )
                ordered[i] = None

    return [ordered[i] for i in range(len(sources))]


# ---------------------------------------------------------------------------
# Resources cache
# ---------------------------------------------------------------------------

def ensure_pgx_resources_dir(config: PgxConfig) -> Path:
    """Return a writable resources cache dir, seeding it from pgx_repo/resources/.

    Creates base_dir/pgx_resources/ if it doesn't exist and copies any files
    from pgx_repo/resources/ that are not already present (skips existing files
    so cached downloads are preserved).
    """
    cache = config.pgx_resources_dir
    cache.mkdir(parents=True, exist_ok=True)

    if config.pgx_repo is not None:
        src = config.pgx_repo / "resources"
        if src.is_dir():
            for item in src.iterdir():
                dest = cache / item.name
                if not dest.exists():
                    if item.is_file():
                        shutil.copy2(item, dest)
                    elif item.is_dir():
                        shutil.copytree(item, dest)

    return cache


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_pgx_layout(config: PgxConfig) -> None:
    """Warn if liftover dir is absent and no explicit VCF inputs were provided."""
    if config.vcf or config.vcf_dir is not None:
        return
    if not config.liftover_dir.exists():
        log.warning(
            "Liftover directory not found: %s. "
            "Pass --vcf <file> or --vcf-dir <dir> to specify VCF inputs explicitly, "
            "or run `impact-tools beacon liftover` first.",
            config.liftover_dir,
        )


def validate_pgx_run_prereqs(config: PgxConfig) -> None:
    """Check prerequisites needed for the pgx_pilot run step."""
    if config.pgx_repo is None or not config.pgx_repo.exists():
        raise ValueError(
            f"pgx_pilot repo not found: {config.pgx_repo}\n"
            "Pass --pgx-repo or set the PGX_REPO environment variable."
        )
    try:
        subprocess.run(
            ["docker", "image", "inspect", config.pgx_image],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except subprocess.CalledProcessError:
        raise ValueError(
            f"Docker image {config.pgx_image!r} not found locally.\n"
            f"Build it with: cd {config.pgx_repo} && docker build -t {config.pgx_image} ."
        )

def install_snakefile(config: PgxConfig) -> None:
    """Install or update the bundled Snakefile under pgx_runs/."""
    dest = config.snakefile

    config.pgx_runs_dir.mkdir(
        parents=True,
        exist_ok=True,
    )

    src = files("impact_tools.beacon.resources").joinpath(
        "Snakefile"
    )
    bundled_content = src.read_text()

    if dest.exists():
        installed_content = dest.read_text()

        if installed_content == bundled_content:
            log.info(
                "Snakefile is already up to date at %s.",
                dest,
            )
            return

        log.info(
            "Updating installed Snakefile: %s",
            dest,
        )

    dest.write_text(
        bundled_content,
        encoding="utf-8",
    )

    log.info(
        "Installed bundled Snakefile -> %s",
        dest,
    )


# ---------------------------------------------------------------------------
# Workspace preparation
# ---------------------------------------------------------------------------

def prepare_workspace(
    config: PgxConfig,
    record: SampleRecord,
) -> WorkspaceResult:
    """Create the pgx_pilot workspace for one sample."""
    ws = config.pgx_runs_dir / record.sample_id
    started = time.monotonic()

    if not record.vcf_basename:
        log.error(
            "[%s] Source VCF basename is missing",
            record.sample_id,
        )
        return WorkspaceResult(
            sample_id=record.sample_id,
            workspace=ws,
            status="error",
            duration_seconds=time.monotonic() - started,
            error="Source VCF basename is missing.",
        )

    vcf_src = config.resolve_vcf(record.vcf_basename).resolve()
    tbi_src = Path(f"{vcf_src}.tbi")

    if not vcf_src.is_file():
        log.error(
            "[%s] Missing lifted VCF: %s",
            record.sample_id,
            vcf_src,
        )
        return WorkspaceResult(
            sample_id=record.sample_id,
            workspace=ws,
            status="error",
            duration_seconds=time.monotonic() - started,
            error=f"Missing lifted VCF: {vcf_src}",
        )

    if not tbi_src.is_file():
        log.error(
            "[%s] Missing .tbi index: %s",
            record.sample_id,
            tbi_src,
        )
        return WorkspaceResult(
            sample_id=record.sample_id,
            workspace=ws,
            status="error",
            duration_seconds=time.monotonic() - started,
            error=f"Missing VCF index: {tbi_src}",
        )

    data_dir = ws / "data"
    results_dir = ws / "results"

    data_dir.mkdir(parents=True, exist_ok=True)
    results_dir.mkdir(parents=True, exist_ok=True)

    links = [
        (
            data_dir / f"{record.sample_id}.vcf.gz",
            vcf_src,
        ),
        (
            data_dir / f"{record.sample_id}.vcf.gz.tbi",
            tbi_src,
        ),
    ]

    for link, target in links:
        if link.exists() or link.is_symlink():
            link.unlink()

        link.symlink_to(target)

    # Used by bcftools view -S to select this sample from a joint VCF.
    (data_dir / "sample_id.txt").write_text(
        f"{record.sample_id}\n",
        encoding="utf-8",
    )

    # Metadata consumed by pgx_pilot.
    (data_dir / "samples.tsv").write_text(
        f"{record.sample_id}\t"
        f"{record.sex}\t"
        f"{record.country_code}\n",
        encoding="utf-8",
    )

    (ws / "config.yaml").write_text(
        _CONFIG_YAML_TEMPLATE.format(
            sample_id=record.sample_id,
            country_code=record.country_code,
        ),
        encoding="utf-8",
    )

    log.info(
        "[%s] Workspace ready -> %s "
        "(source VCF: %s)",
        record.sample_id,
        ws,
        record.vcf_basename,
    )

    return WorkspaceResult(
        sample_id=record.sample_id,
        workspace=ws,
        status="ok",
        duration_seconds=time.monotonic() - started,
    )

# ---------------------------------------------------------------------------
# pgx_pilot execution
# ---------------------------------------------------------------------------

def run_pgx_pilot(config: PgxConfig, record: SampleRecord) -> PgxRunResult:
    """Run the pgx_pilot Snakemake pipeline for one sample via Docker."""
    ws = config.pgx_runs_dir / record.sample_id
    vcf_in = config.resolve_vcf(
        record.vcf_basename or f"{record.sample_id}.GRCh38.clean.vcf.gz"
    )
    out_all = ws / "results" / f"{record.sample_id}.sites.all.vcf.gz"
    out_pass = ws / "results" / f"{record.sample_id}.sites.pass.vcf.gz"
    log_file = config.base_dir / "logs" / f"{record.sample_id}_pgx.log"

    if not ws.exists() or not (ws / "config.yaml").exists():
        log.error("[%s] Workspace missing — run prepare step first", record.sample_id)
        return PgxRunResult(
            sample_id=record.sample_id,
            output_all=None,
            output_pass=None,
            status="error",
            error="Workspace or config.yaml is missing.",
        )

    for path in [ws / "results", ws / ".snakemake"]:
        if path.exists():
            try:
                shutil.rmtree(path)
            except PermissionError:
                log.warning(
                    "[%s] Could not remove %s (Docker may own it). "
                    "Run: sudo rm -rf %s",
                    record.sample_id, path, path,
                )
    (ws / "results").mkdir(exist_ok=True)

    resources_cache = ensure_pgx_resources_dir(config)

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{ws}:/pipeline:z",
        "-v", f"{vcf_in.parent}:{vcf_in.parent}:ro,z",
        "-v", f"{config.snakefile}:/pipeline/Snakefile:ro,z",
        "-v", f"{config.pgx_repo}/scripts:/pipeline/scripts:ro,z",
        "-v", f"{resources_cache}:/pipeline/resources:z",
        "-w", "/pipeline",
        config.pgx_image,
        "snakemake", "-s", "Snakefile",
        "-j", str(config.snakemake_jobs),
        "--rerun-incomplete",
    ]

    log.info(
        "[%s] Input:  %s  (%s)",
        record.sample_id, vcf_in.name, _fmt_size(vcf_in),
    )
    log.info("[%s] Running pgx_pilot (log: %s)", record.sample_id, log_file)
    t0 = time.monotonic()
    with log_file.open("w") as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=lf, check=False)
    elapsed = time.monotonic() - t0

    if proc.returncode != 0:
        log.error(
            "[%s] pgx_pilot failed (rc=%d, %.1fs) — check %s",
            record.sample_id, proc.returncode, elapsed, log_file,
        )
        return PgxRunResult(
            sample_id=record.sample_id,
            output_all=None,
            output_pass=None,
            status="error",
            duration_seconds=elapsed,
            return_code=proc.returncode,
            error=f"pgx_pilot failed. Check log: {log_file}",
        )

    if not out_pass.exists():
        log.warning(
            "[%s] Snakemake exited OK but %s not found (%.1fs) — check %s",
            record.sample_id, out_pass.name, elapsed, log_file,
        )
        return PgxRunResult(
            sample_id=record.sample_id,
            output_all=out_all if out_all.exists() else None,
            output_pass=None,
            status="warn",
            duration_seconds=elapsed,
            return_code=proc.returncode,
            error=f"Expected output not found: {out_pass}",
        )

    log.info(
        "[%s] pgx_pilot OK  (%.1fs)  all=%s  pass=%s",
        record.sample_id, elapsed,
        _fmt_size(out_all) if out_all.exists() else "N/A",
        _fmt_size(out_pass),
    )
    return PgxRunResult(
        sample_id=record.sample_id,
        output_all=out_all if out_all.exists() else None,
        output_pass=out_pass,
        status="ok",
        duration_seconds=elapsed,
        return_code=proc.returncode,
    )


# ---------------------------------------------------------------------------
# Parallel batch helpers
# ---------------------------------------------------------------------------

def _parallel(fn, config: PgxConfig, items: list, error_factory) -> list:
    """Run one function per item while preserving input order."""
    ordered: dict[int, object] = {}

    if config.workers <= 1 or len(items) <= 1:
        for index, item in enumerate(items):
            try:
                ordered[index] = fn(config, item)
            except Exception as exc:
                log.error(
                    "Unexpected error processing item %d: %s",
                    index,
                    exc,
                )
                ordered[index] = error_factory(item)

        return [ordered[index] for index in range(len(items))]

    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        future_to_idx = {
            pool.submit(fn, config, item): index
            for index, item in enumerate(items)
        }

        for future in as_completed(future_to_idx):
            index = future_to_idx[future]

            try:
                ordered[index] = future.result()
            except Exception as exc:
                log.error(
                    "Unexpected error processing item %d: %s",
                    index,
                    exc,
                )
                ordered[index] = error_factory(items[index])

    return [ordered[index] for index in range(len(items))]


def prepare_workspaces(
    config: PgxConfig,
    records: list[SampleRecord],
) -> list[WorkspaceResult]:
    """Prepare workspaces in parallel; results are in the same order as records."""

    def _err(record: SampleRecord) -> WorkspaceResult:
        return WorkspaceResult(
            sample_id=record.sample_id,
            workspace=config.pgx_runs_dir / record.sample_id,
            status="error",
            error="Unexpected workspace preparation error.",
        )

    return _parallel(
        prepare_workspace,
        config,
        records,
        _err,
    )


def run_pgx_pilots(
    config: PgxConfig,
    records: list[SampleRecord],
) -> list[PgxRunResult]:
    """Run pgx_pilot in parallel; results are in the same order as records."""

    def _err(record: SampleRecord) -> PgxRunResult:
        return PgxRunResult(
            sample_id=record.sample_id,
            output_all=None,
            output_pass=None,
            status="error",
            error="Unexpected pgx_pilot execution error.",
        )

    return _parallel(
        run_pgx_pilot,
        config,
        records,
        _err,
    )
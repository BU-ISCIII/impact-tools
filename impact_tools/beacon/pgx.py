"""Beacon pgx_pilot workspace preparation and execution."""

from __future__ import annotations

import dataclasses
import logging
import shutil
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal
from importlib.resources import files

log = logging.getLogger(__name__)

Sex = Literal["M", "F"]

PGX_IMAGE = "goe/pgx-pipeline:latest"
BCFTOOLS_IMAGE = "staphb/bcftools:1.21"
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

    @property
    def liftover_dir(self) -> Path:
        return self.base_dir / "liftover"

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


@dataclasses.dataclass
class PgxRunResult:
    sample_id: str
    output_all: Path | None
    output_pass: Path | None
    status: str  # "ok", "warn", "error"


@dataclasses.dataclass
class PgxPipelineResult:
    prepare_results: list[WorkspaceResult]
    run_results: list[PgxRunResult]

    @property
    def failed(self) -> int:
        return sum(
            1 for r in [*self.prepare_results, *self.run_results]
            if r.status == "error"
        )

    @property
    def warned(self) -> int:
        return sum(
            1 for r in [*self.prepare_results, *self.run_results]
            if r.status == "warn"
        )


# ---------------------------------------------------------------------------
# Discovery and samples.tsv I/O
# ---------------------------------------------------------------------------

def discover_lifted_vcfs(base_dir: Path) -> list[Path]:
    """Return sorted list of *.GRCh38.clean.vcf.gz under <base_dir>/liftover/."""
    return sorted((base_dir / "liftover").glob("*.GRCh38.clean.vcf.gz"))


def vcf_to_sample_id(vcf: Path) -> str:
    """Derive the sample identifier from a lifted VCF filename."""
    name = vcf.name
    return name.removesuffix(".GRCh38.clean.vcf.gz") if name.endswith(".GRCh38.clean.vcf.gz") else vcf.stem


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
            records.append(SampleRecord(sample_id=sample_id, sex=sex, country_code=country_code))
    return records


def append_sample_to_tsv(tsv_path: Path, record: SampleRecord) -> None:
    """Append one sample line to samples.tsv, creating the file with a header if needed."""
    if not tsv_path.exists():
        tsv_path.parent.mkdir(parents=True, exist_ok=True)
        tsv_path.write_text(
            "# samples.tsv — IMPaCT cohort sample metadata\n"
            "# sample_id<TAB>sex<TAB>country_code\n"
            "# sex: M or F (inferred from non-ref chrY variant count)\n"
        )
    with tsv_path.open("a") as fh:
        fh.write(f"{record.sample_id}\t{record.sex}\t{record.country_code}\n")


# ---------------------------------------------------------------------------
# Sex inference
# ---------------------------------------------------------------------------

def count_chry_variants(config: PgxConfig, vcf_basename: str) -> SexInferenceResult | None:
    """Count non-ref chrY variants in a single-sample VCF using bcftools via Docker.

    The sample ID is read from the VCF header so it matches what pgx_pilot will use.
    Returns None if the Docker call fails.
    """
    base = str(config.base_dir)
    inner = (
        f"VCF=/data/liftover/{vcf_basename}\n"
        "sample=$(bcftools query -l \"$VCF\" | head -1)\n"
        "n=$(bcftools query -r chrY -f '[%GT\\n]' \"$VCF\" 2>/dev/null"
        " | grep -cvE '^(0[/|]0|\\.[/|]\\.|\\.)(\\t|$)' || true)\n"
        "printf '%s\\t%s\\n' \"$sample\" \"$n\"\n"
    )
    cmd = [
        "docker", "run", "--rm",
        "-v", f"{base}:/data",
        config.bcftools_image,
        "bash", "-c", inner,
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        log.error("count_chry_variants failed for %s: %s", vcf_basename, proc.stderr.strip())
        return None

    parts = proc.stdout.strip().split("\t")
    if len(parts) != 2:
        log.error("Unexpected output from chrY count: %r", proc.stdout.strip())
        return None

    sample_id, n_str = parts
    try:
        n = int(n_str)
    except ValueError:
        log.error("Non-integer chrY count for %s: %r", vcf_basename, n_str)
        return None

    return SexInferenceResult(
        sample_id=sample_id,
        vcf_basename=vcf_basename,
        n_chry=n,
        sex=infer_sex(n, config),
    )


def infer_sex(n_chry: int, config: PgxConfig) -> Sex | None:
    """Return M, F, or None if the count falls in the ambiguous zone."""
    if n_chry > config.sex_ambiguous_max:
        return "M"
    if n_chry < config.sex_ambiguous_min:
        return "F"
    return None


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------

def validate_pgx_layout(config: PgxConfig) -> None:
    """Check that the liftover output directory exists."""
    if not config.liftover_dir.exists():
        raise ValueError(
            f"Liftover directory not found: {config.liftover_dir}\n"
            "Run `impact-tools beacon liftover` first."
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
    """Copy the bundled Snakefile to pgx_runs/ if not already present."""
    dest = config.snakefile
    if dest.exists():
        log.info("Snakefile already exists at %s — skipping.", dest)
        return
    config.pgx_runs_dir.mkdir(parents=True, exist_ok=True)
    src = files("impact_tools.beacon.resources").joinpath("Snakefile")
    dest.write_text(src.read_text())
    log.info("Installed bundled Snakefile -> %s", dest)


# ---------------------------------------------------------------------------
# Workspace preparation
# ---------------------------------------------------------------------------

def prepare_workspace(config: PgxConfig, record: SampleRecord) -> WorkspaceResult:
    """Create the pgx_pilot workspace for one sample."""
    ws = config.pgx_runs_dir / record.sample_id
    vcf_name = record.vcf_basename if record.vcf_basename else record.sample_id
    vcf_src = config.liftover_dir / f"{vcf_name}.GRCh38.clean.vcf.gz"
    tbi_src = Path(str(vcf_src) + ".tbi")

    if not vcf_src.exists():
        log.error("[%s] Missing lifted VCF: %s", record.sample_id, vcf_src)
        return WorkspaceResult(record.sample_id, ws, "error")
    if not tbi_src.exists():
        log.error("[%s] Missing .tbi index: %s", record.sample_id, tbi_src)
        return WorkspaceResult(record.sample_id, ws, "error")

    (ws / "data").mkdir(parents=True, exist_ok=True)
    (ws / "results").mkdir(parents=True, exist_ok=True)

    for link, target in [
        (ws / "data" / f"{record.sample_id}.vcf.gz", vcf_src),
        (ws / "data" / f"{record.sample_id}.vcf.gz.tbi", tbi_src),
    ]:
        if link.is_symlink():
            link.unlink()
        link.symlink_to(target)

    (ws / "data" / "samples.tsv").write_text(
        f"{record.sample_id}\t{record.sex}\t{record.country_code}\n"
    )

    (ws / "config.yaml").write_text(
        _CONFIG_YAML_TEMPLATE.format(
            sample_id=record.sample_id,
            country_code=record.country_code,
        )
    )

    log.info("[%s] Workspace ready -> %s", record.sample_id, ws)
    return WorkspaceResult(record.sample_id, ws, "ok")


# ---------------------------------------------------------------------------
# pgx_pilot execution
# ---------------------------------------------------------------------------

def run_pgx_pilot(config: PgxConfig, record: SampleRecord) -> PgxRunResult:
    """Run the pgx_pilot Snakemake pipeline for one sample via Docker."""
    ws = config.pgx_runs_dir / record.sample_id
    vcf_name = record.vcf_basename if record.vcf_basename else record.sample_id
    vcf_in = config.liftover_dir / f"{vcf_name}.GRCh38.clean.vcf.gz"
    out_all = ws / "results" / f"{record.sample_id}.sites.all.vcf.gz"
    out_pass = ws / "results" / f"{record.sample_id}.sites.pass.vcf.gz"
    log_file = config.base_dir / "logs" / f"{record.sample_id}_pgx.log"

    if not ws.exists() or not (ws / "config.yaml").exists():
        log.error("[%s] Workspace missing — run prepare step first", record.sample_id)
        return PgxRunResult(record.sample_id, None, None, "error")

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

    cmd = [
        "docker", "run", "--rm",
        "-v", f"{ws}:/pipeline",
        "-v", f"{vcf_in.parent}:{vcf_in.parent}:ro",
        "-v", f"{config.snakefile}:/pipeline/Snakefile:ro",
        "-v", f"{config.pgx_repo}/scripts:/pipeline/scripts:ro",
        "-v", f"{config.pgx_repo}/resources:/pipeline/resources:ro",
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
        return PgxRunResult(record.sample_id, None, None, "error")

    if not out_pass.exists():
        log.warning(
            "[%s] Snakemake exited OK but %s not found (%.1fs) — check %s",
            record.sample_id, out_pass.name, elapsed, log_file,
        )
        return PgxRunResult(
            record.sample_id, out_all if out_all.exists() else None, None, "warn"
        )

    log.info(
        "[%s] pgx_pilot OK  (%.1fs)  all=%s  pass=%s",
        record.sample_id, elapsed,
        _fmt_size(out_all) if out_all.exists() else "N/A",
        _fmt_size(out_pass),
    )
    return PgxRunResult(
        record.sample_id,
        out_all if out_all.exists() else None,
        out_pass,
        "ok",
    )


# ---------------------------------------------------------------------------
# Parallel batch helpers
# ---------------------------------------------------------------------------

def _parallel(fn, config: PgxConfig, items: list, error_factory) -> list:
    """Run fn(config, item) for each item, in parallel when config.workers > 1."""
    if config.workers <= 1 or len(items) <= 1:
        return [fn(config, item) for item in items]

    ordered: dict[int, object] = {}
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        future_to_idx = {pool.submit(fn, config, item): i for i, item in enumerate(items)}
        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            try:
                ordered[i] = future.result()
            except Exception as exc:
                log.error("Unexpected error processing item %d: %s", i, exc)
                ordered[i] = error_factory(items[i])
    return [ordered[i] for i in range(len(items))]


def infer_sex_batch(
    config: PgxConfig, vcf_basenames: list[str]
) -> list[SexInferenceResult | None]:
    """Run count_chry_variants in parallel; results are in the same order as input."""
    if config.workers <= 1 or len(vcf_basenames) <= 1:
        return [count_chry_variants(config, b) for b in vcf_basenames]

    ordered: dict[int, SexInferenceResult | None] = {}
    with ThreadPoolExecutor(max_workers=config.workers) as pool:
        future_to_idx = {
            pool.submit(count_chry_variants, config, b): i
            for i, b in enumerate(vcf_basenames)
        }
        for future in as_completed(future_to_idx):
            i = future_to_idx[future]
            try:
                ordered[i] = future.result()
            except Exception as exc:
                log.error("Sex inference error for %s: %s", vcf_basenames[i], exc)
                ordered[i] = None
    return [ordered[i] for i in range(len(vcf_basenames))]


def prepare_workspaces(
    config: PgxConfig, records: list[SampleRecord]
) -> list[WorkspaceResult]:
    """Prepare workspaces in parallel; results are in the same order as records."""
    def _err(r: SampleRecord) -> WorkspaceResult:
        return WorkspaceResult(r.sample_id, config.pgx_runs_dir / r.sample_id, "error")

    return _parallel(prepare_workspace, config, records, _err)  # type: ignore[arg-type]


def run_pgx_pilots(
    config: PgxConfig, records: list[SampleRecord]
) -> list[PgxRunResult]:
    """Run pgx_pilot in parallel; results are in the same order as records."""
    def _err(r: SampleRecord) -> PgxRunResult:
        return PgxRunResult(r.sample_id, None, None, "error")

    return _parallel(run_pgx_pilot, config, records, _err)  # type: ignore[arg-type]

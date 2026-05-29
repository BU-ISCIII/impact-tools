"""Beacon liftover utilities."""

from __future__ import annotations

import dataclasses
import gzip
import logging
import re
import subprocess
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Literal

log = logging.getLogger(__name__)

Build = Literal["GRCh37", "GRCh38", "unknown"]

CHR1_LENGTH_TO_BUILD: dict[str, Build] = {
    "249250621": "GRCh37",
    "248956422": "GRCh38",
}

BCFTOOLS_IMAGE = "staphb/bcftools:1.21"
CROSSMAP_IMAGE = "quay.io/biocontainers/crossmap:0.7.3--pyhdfd78af_0"
CHAIN_FILENAME = "hg19ToHg38.over.chain.gz"
FASTA_FILENAME = "GRCh38_full_analysis_set_plus_decoy_hla.fa"
_INFO_REMOVE = (
    "INFO/AC,INFO/AN,INFO/AF,INFO/MLEAC,INFO/MLEAF,"
    "INFO/ExcessHet,INFO/InbreedingCoeff"
)


@dataclasses.dataclass
class LiftoverConfig:
    base_dir: Path
    chain: Path | None = None
    fasta: Path | None = None
    hpc_mount: str = "/data/ucct/bi"
    bcftools_image: str = BCFTOOLS_IMAGE
    crossmap_image: str = CROSSMAP_IMAGE
    cleanup: bool = False
    workers: int = 4

    @property
    def resources_dir(self) -> Path:
        return self.base_dir / "liftover" / "resources"

    @property
    def resolved_chain(self) -> Path:
        return self.chain if self.chain is not None else (self.resources_dir / CHAIN_FILENAME)

    @property
    def resolved_fasta(self) -> Path:
        return self.fasta if self.fasta is not None else (self.resources_dir / FASTA_FILENAME)


@dataclasses.dataclass
class SampleLiftoverResult:
    sample_id: str
    input_vcf: Path
    output_vcf: Path | None
    status: str  # "ok", "warn", "error"
    n_variants: int | None = None


@dataclasses.dataclass
class LiftoverRunResult:
    results: list[SampleLiftoverResult]

    @property
    def failed(self) -> int:
        return sum(1 for r in self.results if r.status == "error")

    @property
    def warned(self) -> int:
        return sum(1 for r in self.results if r.status == "warn")


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


def open_text(path: Path):
    """Open plain or gzipped text files as text."""
    if path.name.endswith(".gz"):
        return gzip.open(path, "rt", errors="replace")
    return path.open("rt", errors="replace")


def detect_vcf_build(vcf_path: Path) -> tuple[Build, str, str | None]:
    """Detect genome build from chr1/1 contig length in a VCF header.

    Returns:
        Tuple with detected build, contig style and chr1 length.
    """
    contig_style = "unknown"

    with open_text(vcf_path) as handle:
        for line in handle:
            if line.startswith("#CHROM"):
                break

            if not line.startswith("##contig=<"):
                continue

            contig_id_match = re.search(r"ID=([^,>]+)", line)
            length_match = re.search(r"length=([0-9]+)", line)

            if not contig_id_match or not length_match:
                continue

            contig_id = contig_id_match.group(1)
            contig_length = length_match.group(1)

            if contig_id == "chr1":
                contig_style = "UCSC"
            elif contig_id == "1":
                contig_style = "Ensembl"
            else:
                continue

            return CHR1_LENGTH_TO_BUILD.get(contig_length, "unknown"), contig_style, contig_length

    return "unknown", contig_style, None


def discover_input_vcfs(base_dir: Path) -> list[Path]:
    """Discover input VCF files under <base_dir>/inputs."""
    inputs_dir = base_dir / "inputs"
    return sorted(inputs_dir.glob("*.vcf.gz"))


def validate_liftover_layout(base_dir: Path) -> None:
    """Validate that the minimal liftover working directory layout exists."""
    required = [
        base_dir, 
        base_dir / "inputs",
        base_dir / "liftover" / "resources",
        base_dir / "logs",
    ]

    missing = [path for path in required if not path.exists()]
    if missing:
        missing_text = "\n".join(f"  - {path}" for path in missing)
        raise ValueError(f"Missing required directories:\n{missing_text}")


def create_liftover_layout(base_dir: Path) -> None:
    """Create the minimal liftover working directory layout"""

    required_dirs = [
        base_dir / "inputs",
        base_dir / "liftover" / "resources",
        base_dir / "logs",
    ]

    created_dirs = []

    for path in required_dirs:
        if not path.exists():
            path.mkdir(parents=True, exist_ok=True)
            created_dirs.append(path)

    if created_dirs:
        log.info("Created liftover workspace directories:")
        for path in created_dirs:
            log.info("  - %s", path)


def check_liftover_inputs(base_dir: Path) -> list[tuple[Path, Build, str, str | None]]:
    """Validate and inspect liftover input VCFs."""
    vcfs = discover_input_vcfs(base_dir)
    if not vcfs:
        raise ValueError(
            f"No *.vcf.gz files found in {base_dir / 'inputs'}\n"
            "Add input VCF files or symlinks under inputs/ and rerun the command.\n"
            "Expected layout:\n"
            "<base-dir>/\n"
            "├── inputs/       ← place your *.vcf.gz files here\n"
            "├── liftover/\n"
            "└── logs/"
        )

    results: list[tuple[Path, Build, str, str | None]] = []

    for vcf in vcfs:
        if vcf.is_symlink():
            resolved = vcf.resolve(strict=False)
            try:
                accessible = resolved.exists()
            except OSError:
                accessible = False
            if not accessible:
                log.warning(
                    "  %s: symlink target not accessible (%s) — skipping build detection",
                    vcf.name, resolved,
                )
                results.append((vcf, "unknown", "unknown", None))
                continue

        try:
            build, contig_style, chr1_length = detect_vcf_build(vcf)
        except OSError as exc:
            log.warning(
                "  %s: Could not read file to detect build: %s — skipping",
                vcf.name, exc,
            )
            results.append((vcf, "unknown", "unknown", None))
            continue

        results.append((vcf, build, contig_style, chr1_length))

    return results


def validate_resources(config: LiftoverConfig) -> None:
    """Check that required liftover resources exist and are co-located."""
    if config.resolved_chain.parent != config.resolved_fasta.parent:
        raise ValueError(
            "--chain and --fasta must be in the same directory "
            "(the Docker mount strategy maps a single /resources volume). "
            f"chain: {config.resolved_chain.parent}, "
            f"fasta: {config.resolved_fasta.parent}"
        )

    fai = Path(str(config.resolved_fasta) + ".fai")
    missing = [
        p for p in [config.resolved_chain, config.resolved_fasta, fai]
        if not p.exists()
    ]
    if missing:
        missing_text = "\n".join(f"  - {p}" for p in missing)
        raise ValueError(
            f"Missing required liftover resources:\n{missing_text}\n"
            "\nSpecify directory with --chain and --fasta if they are already available elsewhere.\n"
            "\nor\n"
            "\nDownload them with:\n"
            f"  wget -P {config.resolved_chain.parent} "
            "https://hgdownload.soe.ucsc.edu/goldenPath/hg19/liftOver/hg19ToHg38.over.chain.gz\n"
            f"  wget -P {config.resolved_fasta.parent} "
            "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/technical/reference/GRCh38_reference_genome/GRCh38_full_analysis_set_plus_decoy_hla.fa\n"
            f"  wget -P {config.resolved_fasta.parent} "
            "https://ftp.1000genomes.ebi.ac.uk/vol1/ftp/technical/reference/GRCh38_reference_genome/GRCh38_full_analysis_set_plus_decoy_hla.fa.fai"
        )


def validate_docker_available() -> None:
    """Check that Docker is reachable before starting the pipeline."""
    try:
        subprocess.run(
            ["docker", "--version"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=True,
        )
    except FileNotFoundError as exc:
        raise ValueError("Docker executable not found in PATH.") from exc
    except subprocess.CalledProcessError as exc:
        raise ValueError("Docker is installed but returned a non-zero exit code.") from exc


def _prepare_auxiliary_files(liftover_dir: Path, fasta_fai: Path) -> tuple[Path, Path]:
    """Create rename_chrs.txt and contigs_GRCh38.txt if not already present."""
    rename_file = liftover_dir / "rename_chrs.txt"
    if not rename_file.exists():
        rows = [f"{i} chr{i}" for i in [*range(1, 23), "X", "Y"]]
        rows.append("MT chrM")
        rename_file.write_text("\n".join(rows) + "\n")
        log.debug("Created %s", rename_file)

    contigs_file = liftover_dir / "contigs_GRCh38.txt"
    if not contigs_file.exists():
        lines = []
        with fasta_fai.open() as fh:
            for line in fh:
                parts = line.split("\t")
                if len(parts) >= 2:
                    lines.append(f"##contig=<ID={parts[0]},length={parts[1].rstrip()}>")
        contigs_file.write_text("\n".join(lines) + "\n")
        log.debug("Created %s", contigs_file)

    return rename_file, contigs_file


def _fix_contig_headers(lifted: Path, fixed: Path, contigs_content: str) -> None:
    """Replace ##contig lines in lifted VCF with the full GRCh38 contig set.

    CrossMap only preserves HLA contigs; this restores all GRCh38 headers.
    contigs_content is pre-loaded by the caller to avoid re-reading for every sample.
    """
    with lifted.open("rt", errors="replace") as src, fixed.open("wt") as dst:
        for line in src:
            if line.startswith("##contig"):
                continue
            if line.startswith("##fileformat"):
                dst.write(line)
                dst.write(contigs_content)
                continue
            dst.write(line)


def _run_cmd(cmd: list[str], log_file: Path, *, mode: str = "a") -> bool:
    """Run a command, capturing stdout+stderr to log_file. Returns True on success."""
    with log_file.open(mode) as lf:
        proc = subprocess.run(cmd, stdout=lf, stderr=lf)
    return proc.returncode == 0


def _run_sample(
    config: LiftoverConfig,
    sample_id: str,
    contigs_content: str,
) -> SampleLiftoverResult:
    """Run the 6-step liftover pipeline for one sample."""
    base = str(config.base_dir)
    hpc = config.hpc_mount
    resources = str(config.resolved_chain.parent)

    liftover_dir = config.base_dir / "liftover"
    logs_dir = config.base_dir / "logs"

    in_vcf = config.base_dir / "inputs" / f"{sample_id}.vcf.gz"
    lifted = liftover_dir / f"{sample_id}.GRCh38.vcf"
    fixed = liftover_dir / f"{sample_id}.GRCh38.fixed.vcf"
    clean = liftover_dir / f"{sample_id}.GRCh38.clean.vcf.gz"
    lift_log = logs_dir / f"{sample_id}_liftover.log"

    log.info("[%s] Input:  %s  (%s)", sample_id, in_vcf.name, _fmt_size(in_vcf))
    t0_sample = time.monotonic()

    # Step 1: rename contigs Ensembl -> UCSC (1 -> chr1)
    log.info("[%s] Step 1: rename contigs", sample_id)
    t1 = time.monotonic()
    cmd1 = [
        "docker", "run", "--rm",
        "-v", f"{base}:/data",
    ]

    if Path(hpc).exists():
        cmd1 += ["-v", f"{hpc}:{hpc}:ro"]

    cmd1 += [
        config.bcftools_image,
        "bcftools", "annotate",
        "--rename-chrs", "/data/liftover/rename_chrs.txt",
        "-Oz", "-o", f"/data/liftover/{sample_id}.renamed.vcf.gz",
        f"/data/inputs/{sample_id}.vcf.gz",
    ]
    if not _run_cmd(cmd1, lift_log, mode="w"):
        log.error("[%s] Step 1 failed — check %s", sample_id, lift_log)
        return SampleLiftoverResult(sample_id, in_vcf, None, "error")
    log.info("[%s] Step 1 done  (%.1fs)", sample_id, time.monotonic() - t1)

    # Step 2: CrossMap liftover GRCh37 -> GRCh38
    log.info("[%s] Step 2: CrossMap liftover", sample_id)
    t2 = time.monotonic()
    cmd2 = [
        "docker", "run", "--rm",
        "-v", f"{base}:/data",
        "-v", f"{resources}:/resources:ro",
        config.crossmap_image,
        "CrossMap", "vcf",
        f"/resources/{config.resolved_chain.name}",
        f"/data/liftover/{sample_id}.renamed.vcf.gz",
        f"/resources/{config.resolved_fasta.name}",
        f"/data/liftover/{sample_id}.GRCh38.vcf",
    ]
    if not _run_cmd(cmd2, lift_log):
        log.error("[%s] Step 2 failed — check %s", sample_id, lift_log)
        return SampleLiftoverResult(sample_id, in_vcf, None, "error")
    log.info("[%s] Step 2 done  (%.1fs)", sample_id, time.monotonic() - t2)

    for line in lift_log.read_text(errors="replace").splitlines():
        if "Total entries" in line or "Failed to map" in line:
            log.info("[%s]   %s", sample_id, line.strip())

    # Step 3: fix ##contig headers (CrossMap only preserves HLA contigs)
    log.info("[%s] Step 3: fix ##contig headers", sample_id)
    t3 = time.monotonic()
    _fix_contig_headers(lifted, fixed, contigs_content)
    log.info("[%s] Step 3 done  (%.1fs)", sample_id, time.monotonic() - t3)

    # Step 4: sort + bgzip + tabix
    log.info("[%s] Step 4: sort + bgzip + tabix", sample_id)
    t4 = time.monotonic()
    cmd4 = [
        "docker", "run", "--rm",
        "-v", f"{base}:/data",
        config.bcftools_image,
        "bash", "-c",
        (
            f"bcftools sort /data/liftover/{sample_id}.GRCh38.fixed.vcf "
            f"-Oz -o /data/liftover/{sample_id}.GRCh38.sorted.vcf.gz && "
            f"bcftools index -t /data/liftover/{sample_id}.GRCh38.sorted.vcf.gz"
        ),
    ]
    if not _run_cmd(cmd4, lift_log):
        log.error("[%s] Step 4 failed — check %s", sample_id, lift_log)
        return SampleLiftoverResult(sample_id, in_vcf, None, "error")
    log.info("[%s] Step 4 done  (%.1fs)", sample_id, time.monotonic() - t4)

    # Step 5: clean obsolete INFO tags
    log.info("[%s] Step 5: clean obsolete INFO tags", sample_id)
    t5 = time.monotonic()
    cmd5 = [
        "docker", "run", "--rm",
        "-v", f"{base}:/data",
        config.bcftools_image,
        "bash", "-c",
        (
            f"bcftools annotate -x '{_INFO_REMOVE}' "
            f"/data/liftover/{sample_id}.GRCh38.sorted.vcf.gz "
            f"-Oz -o /data/liftover/{sample_id}.GRCh38.clean.vcf.gz && "
            f"bcftools index -t /data/liftover/{sample_id}.GRCh38.clean.vcf.gz"
        ),
    ]
    if not _run_cmd(cmd5, lift_log):
        log.error("[%s] Step 5 failed — check %s", sample_id, lift_log)
        return SampleLiftoverResult(sample_id, in_vcf, None, "error")
    log.info("[%s] Step 5 done  (%.1fs)", sample_id, time.monotonic() - t5)

    # Step 6: validate output
    log.info("[%s] Step 6: validate output", sample_id)
    status = "ok"
    n_variants = None

    tbi = Path(str(clean) + ".tbi")
    if not clean.exists() or not tbi.exists():
        log.warning("[%s] Missing output file or .tbi index", sample_id)
        status = "warn"

    if clean.exists():
        has_chr1 = False
        n_variants = 0
        with gzip.open(clean, "rt", errors="replace") as fh:
            for line in fh:
                if line.startswith("##contig=<ID=chr1,"):
                    has_chr1 = True
                if not line.startswith("#"):
                    n_variants += 1

        if not has_chr1:
            log.warning("[%s] chr1 ##contig header not found — Step 3 may have failed", sample_id)
            status = "warn"

        log.info("[%s] Variants in clean VCF: %d", sample_id, n_variants)
        log.info("[%s] Output: %s  (%s)", sample_id, clean.name, _fmt_size(clean))
        log.info("[%s] Index:  %s  (%s)", sample_id, tbi.name, _fmt_size(tbi))

    elapsed = time.monotonic() - t0_sample
    if status == "ok":
        log.info("[%s] Validation OK -> %s  [total: %.1fs]", sample_id, clean, elapsed)
    else:
        log.info("[%s] Finished with status=%s  [total: %.1fs]", sample_id, status, elapsed)

    return SampleLiftoverResult(
        sample_id, in_vcf, clean if clean.exists() else None, status, n_variants
    )


def _cleanup_intermediates(liftover_dir: Path, sample_ids: list[str]) -> None:
    """Remove intermediate files generated during liftover."""
    suffixes = [
        ".renamed.vcf.gz",
        ".GRCh38.vcf",
        ".GRCh38.vcf.unmap",
        ".GRCh38.fixed.vcf",
        ".GRCh38.sorted.vcf.gz",
        ".GRCh38.sorted.vcf.gz.tbi",
    ]
    for sample_id in sample_ids:
        for suffix in suffixes:
            f = liftover_dir / f"{sample_id}{suffix}"
            if f.exists():
                try:
                    f.unlink()
                    log.debug("Removed %s", f)
                except PermissionError:
                    log.warning(
                        "Could not remove %s (Docker created it as root). "
                        "Run: sudo rm -f %s",
                        f, f,
                    )
    log.info("Intermediate files cleaned up.")


def run_liftover(config: LiftoverConfig) -> LiftoverRunResult:
    """Run the full CrossMap liftover pipeline for all samples in base_dir/inputs."""
    validate_resources(config)
    validate_docker_available()

    vcfs = discover_input_vcfs(config.base_dir)
    if not vcfs:
        raise ValueError(f"No *.vcf.gz files found in {config.base_dir / 'inputs'}")

    liftover_dir = config.base_dir / "liftover"
    fai = Path(str(config.resolved_fasta) + ".fai")
    _, contigs_file = _prepare_auxiliary_files(liftover_dir, fai)
    contigs_content = contigs_file.read_text()

    sample_ids = [
        (vcf.name[:-7] if vcf.name.endswith(".vcf.gz") else vcf.stem)
        for vcf in vcfs
    ]

    log.info("Queuing %d sample(s)  (workers=%d)", len(sample_ids), config.workers)
    t0_total = time.monotonic()

    ordered: dict[int, SampleLiftoverResult] = {}

    if config.workers <= 1 or len(sample_ids) == 1:
        for i, sid in enumerate(sample_ids):
            log.info("==========================================")
            log.info("Processing %s", sid)
            log.info("==========================================")
            ordered[i] = _run_sample(config, sid, contigs_content)
    else:
        with ThreadPoolExecutor(max_workers=config.workers) as pool:
            future_to_idx = {
                pool.submit(_run_sample, config, sid, contigs_content): i
                for i, sid in enumerate(sample_ids)
            }
            for future in as_completed(future_to_idx):
                i = future_to_idx[future]
                sid = sample_ids[i]
                try:
                    ordered[i] = future.result()
                except Exception as exc:
                    log.error("[%s] Unexpected error: %s", sid, exc)
                    in_vcf = config.base_dir / "inputs" / f"{sid}.vcf.gz"
                    ordered[i] = SampleLiftoverResult(sid, in_vcf, None, "error")

    results = [ordered[i] for i in range(len(sample_ids))]
    run_result = LiftoverRunResult(results)
    log.info("==========================================")
    log.info("Total liftover time: %.1fs  (%d samples)", time.monotonic() - t0_total, len(results))

    if config.cleanup:
        if run_result.failed == 0:
            _cleanup_intermediates(liftover_dir, [r.sample_id for r in results])
        else:
            log.warning(
                "Cleanup skipped: %d sample(s) failed. "
                "Intermediates preserved for debugging.",
                run_result.failed,
            )

    return run_result

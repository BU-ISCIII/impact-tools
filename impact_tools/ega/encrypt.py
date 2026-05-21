"""Crypt4GH encryption workflow for LocalEGA/FEGA inbox submissions."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import shutil
import subprocess
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


LOGGER = logging.getLogger(__name__)

BYTES_IN_MIB = 1024**2
BYTES_IN_GIB = 1024**3

METRIC_FIELDS = [
    "run_id",
    "sample_id",
    "input_file",
    "output_file",
    "input_size_bytes",
    "output_size_bytes",
    "size_delta_bytes",
    "input_size_gib",
    "output_size_gib",
    "size_delta_mib",
    "size_ratio",
    "overhead_percent",
    "encryption_seconds",
    "throughput_mib_s",
    "sha256_input",
    "sha256_c4gh",
    "status",
    "error",
]


@dataclass(frozen=True)
class EncryptionConfig:
    """User-facing encryption settings."""

    input_dir: Path
    recipient_pubkey: Path
    output_dir: Path | None = None
    crypt4gh_bin: Path | None = None
    input_list: Path | None = None
    pattern: str = "*.fastq.gz"
    sample_id: str | None = None
    force: bool = False
    dry_run: bool = False
    compute_checksums: bool = True
    generate_plots: bool = True
    fail_fast: bool = False


@dataclass
class FileMetric:
    """Per-file encryption metrics."""

    run_id: str
    sample_id: str
    input_file: str
    output_file: str
    input_size_bytes: int
    output_size_bytes: int
    size_delta_bytes: int
    input_size_gib: float
    output_size_gib: float
    size_delta_mib: float
    size_ratio: float | None
    overhead_percent: float | None
    encryption_seconds: float
    throughput_mib_s: float | None
    sha256_input: str
    sha256_c4gh: str
    status: str
    error: str


@dataclass
class EncryptionResult:
    """Batch-level result."""

    run_id: str
    output_dir: Path
    metrics_file: Path
    summary_file: Path
    manifest_file: Path
    log_file: Path
    plots_dir: Path | None
    discovered: int
    encrypted: int
    skipped: int
    failed: int
    total_input_bytes: int
    total_output_bytes: int
    total_encryption_seconds: float


def run_encryption(config: EncryptionConfig) -> EncryptionResult:
    """Encrypt all matching files and write metrics, summary and optional plots."""
    input_dir = config.input_dir.expanduser().resolve()
    recipient_pubkey = config.recipient_pubkey.expanduser().resolve()
    output_dir = (
        config.output_dir.expanduser().resolve()
        if config.output_dir is not None
        else input_dir / "encrypted_c4gh"
    )
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)

    log_file = output_dir / f"encryption_{run_id}.log"
    file_handler = _attach_run_log(log_file)
    try:
        _validate_config(input_dir, recipient_pubkey)
        crypt4gh_bin = _resolve_crypt4gh(config.crypt4gh_bin)
        files = _collect_input_files(
            input_dir=input_dir,
            output_dir=output_dir,
            pattern=config.pattern,
            input_list=config.input_list,
        )
        if not files:
            raise ValueError(
                f"No input files found under {input_dir} with pattern {config.pattern}"
            )

        LOGGER.info("Starting Crypt4GH batch encryption")
        LOGGER.info("Run ID: %s", run_id)
        LOGGER.info("Input directory: %s", input_dir)
        LOGGER.info("Output directory: %s", output_dir)
        LOGGER.info("Recipient public key: %s", recipient_pubkey)
        LOGGER.info("Pattern: %s", config.pattern)
        if config.input_list is not None:
            LOGGER.info("Input list: %s", config.input_list.expanduser().resolve())
        LOGGER.info("Files discovered: %s", len(files))

        metrics: list[FileMetric] = []
        for input_file in files:
            metric = _process_file(
                run_id=run_id,
                input_file=input_file,
                input_dir=input_dir,
                output_dir=output_dir,
                recipient_pubkey=recipient_pubkey,
                crypt4gh_bin=crypt4gh_bin,
                config=config,
            )
            metrics.append(metric)
            if metric.status == "failed" and config.fail_fast:
                LOGGER.error("Stopping after first failure because --fail-fast is set")
                break

        metrics_file = output_dir / f"encryption_metrics_{run_id}.tsv"
        summary_file = output_dir / f"encryption_summary_{run_id}.txt"
        manifest_file = output_dir / f"encryption_manifest_{run_id}.json"
        plots_dir = output_dir / f"plots_{run_id}" if config.generate_plots else None

        _write_metrics(metrics_file, metrics)
        result = _build_result(
            run_id=run_id,
            output_dir=output_dir,
            metrics_file=metrics_file,
            summary_file=summary_file,
            manifest_file=manifest_file,
            log_file=log_file,
            plots_dir=plots_dir,
            metrics=metrics,
        )
        _write_summary(
            summary_file,
            result,
            config,
            input_dir,
            recipient_pubkey,
            Path(crypt4gh_bin),
        )
        _write_manifest(
            manifest_file,
            result,
            config,
            input_dir,
            recipient_pubkey,
            Path(crypt4gh_bin),
            metrics,
        )
        if config.generate_plots:
            _write_plots(plots_dir, metrics)

        LOGGER.info("Metrics written to %s", metrics_file)
        LOGGER.info("Summary written to %s", summary_file)
        LOGGER.info("Manifest written to %s", manifest_file)
        LOGGER.info("Run log written to %s", log_file)
        if plots_dir is not None and plots_dir.exists():
            LOGGER.info("Plots written to %s", plots_dir)
        return result
    finally:
        _detach_run_log(file_handler)


def _validate_config(input_dir: Path, recipient_pubkey: Path) -> None:
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input directory does not exist: {input_dir}")
    if not recipient_pubkey.is_file():
        raise FileNotFoundError(f"Recipient public key does not exist: {recipient_pubkey}")
    if not recipient_pubkey.stat().st_size:
        raise ValueError(f"Recipient public key is empty: {recipient_pubkey}")


def _resolve_crypt4gh(crypt4gh_bin: Path | None) -> str:
    if crypt4gh_bin is None:
        executable = shutil.which("crypt4gh")
        if executable is None:
            raise RuntimeError("Required executable not found in PATH: crypt4gh")
    else:
        executable = str(crypt4gh_bin.expanduser().resolve())
        if not Path(executable).is_file():
            raise FileNotFoundError(f"crypt4gh executable does not exist: {executable}")
    completed = subprocess.run(
        [executable, "--version"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip()
        raise RuntimeError(
            f"Required executable is present but cannot run: {executable}. {message}"
        )
    return executable


def _collect_input_files(
    input_dir: Path,
    output_dir: Path,
    pattern: str,
    input_list: Path | None,
) -> list[Path]:
    if input_list is not None:
        return _read_input_list(input_list.expanduser().resolve(), input_dir, output_dir)
    return _discover_input_files(input_dir, output_dir, pattern)


def _discover_input_files(input_dir: Path, output_dir: Path, pattern: str) -> list[Path]:
    files: list[Path] = []
    for candidate in input_dir.rglob(pattern):
        if not candidate.is_file():
            continue
        try:
            candidate.relative_to(output_dir)
        except ValueError:
            files.append(candidate)
    return sorted(files)


def _read_input_list(input_list: Path, input_dir: Path, output_dir: Path) -> list[Path]:
    if not input_list.is_file():
        raise FileNotFoundError(f"Input list does not exist: {input_list}")

    files: list[Path] = []
    seen: set[Path] = set()
    with input_list.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            candidate = Path(line).expanduser()
            if not candidate.is_absolute():
                candidate = input_dir / candidate
            candidate = candidate.resolve()
            if not candidate.is_file():
                raise FileNotFoundError(
                    f"Input list entry does not exist or is not a file "
                    f"({input_list}:{line_number}): {line}"
                )
            try:
                candidate.relative_to(output_dir)
                raise ValueError(
                    f"Input list entry points inside the output directory "
                    f"({input_list}:{line_number}): {candidate}"
                )
            except ValueError as exc:
                if "inside the output directory" in str(exc):
                    raise
            if candidate not in seen:
                files.append(candidate)
                seen.add(candidate)
            else:
                LOGGER.warning(
                    "Ignoring duplicate input list entry at %s:%s: %s",
                    input_list,
                    line_number,
                    candidate,
                )
    return files


def _process_file(
    run_id: str,
    input_file: Path,
    input_dir: Path,
    output_dir: Path,
    recipient_pubkey: Path,
    crypt4gh_bin: str,
    config: EncryptionConfig,
) -> FileMetric:
    sample_id = _sample_id_for_file(input_file, input_dir, config.sample_id)
    relative_input = input_file.relative_to(input_dir)
    output_file = output_dir / sample_id / f"{input_file.name}.c4gh"
    output_file.parent.mkdir(parents=True, exist_ok=True)

    input_size = input_file.stat().st_size
    LOGGER.info("Processing %s", relative_input)

    if config.dry_run:
        LOGGER.info("Dry-run: %s -> %s", input_file, output_file)
        return _metric(
            run_id=run_id,
            sample_id=sample_id,
            input_file=input_file,
            output_file=output_file,
            input_size=input_size,
            output_size=0,
            seconds=0,
            throughput=None,
            sha256_input="",
            sha256_c4gh="",
            status="dry_run",
            error="",
        )

    if output_file.exists() and not config.force:
        output_size = output_file.stat().st_size
        sha_input, sha_output = _checksums(input_file, output_file, config.compute_checksums)
        LOGGER.warning("Skipping existing output: %s", output_file)
        return _metric(
            run_id=run_id,
            sample_id=sample_id,
            input_file=input_file,
            output_file=output_file,
            input_size=input_size,
            output_size=output_size,
            seconds=0,
            throughput=None,
            sha256_input=sha_input,
            sha256_c4gh=sha_output,
            status="skipped_existing",
            error="",
        )

    tmp_file = output_file.with_suffix(output_file.suffix + f".tmp.{run_id}")
    start = time.perf_counter()
    try:
        _encrypt_file(crypt4gh_bin, recipient_pubkey, input_file, tmp_file)
        tmp_file.replace(output_file)
    except Exception as exc:  # noqa: BLE001 - logged and converted to metric
        tmp_file.unlink(missing_ok=True)
        seconds = time.perf_counter() - start
        LOGGER.exception("Encryption failed for %s", input_file)
        return _metric(
            run_id=run_id,
            sample_id=sample_id,
            input_file=input_file,
            output_file=output_file,
            input_size=input_size,
            output_size=0,
            seconds=seconds,
            throughput=_throughput(input_size, seconds),
            sha256_input=_sha256(input_file) if config.compute_checksums else "",
            sha256_c4gh="",
            status="failed",
            error=str(exc),
        )

    seconds = time.perf_counter() - start
    output_size = output_file.stat().st_size
    sha_input, sha_output = _checksums(input_file, output_file, config.compute_checksums)
    throughput = _throughput(input_size, seconds)
    LOGGER.info(
        "Encrypted %s in %.3fs at %s MiB/s",
        relative_input,
        seconds,
        "NA" if throughput is None else f"{throughput:.3f}",
    )
    return _metric(
        run_id=run_id,
        sample_id=sample_id,
        input_file=input_file,
        output_file=output_file,
        input_size=input_size,
        output_size=output_size,
        seconds=seconds,
        throughput=throughput,
        sha256_input=sha_input,
        sha256_c4gh=sha_output,
        status="ok",
        error="",
    )


def _sample_id_for_file(input_file: Path, input_dir: Path, explicit_sample_id: str | None) -> str:
    relative_parent = input_file.relative_to(input_dir).parent
    if relative_parent == Path("."):
        return explicit_sample_id or input_dir.name
    return str(relative_parent)


def _encrypt_file(
    crypt4gh_bin: str,
    recipient_pubkey: Path,
    input_file: Path,
    output_file: Path,
) -> None:
    with input_file.open("rb") as input_handle, output_file.open("wb") as output_handle:
        completed = subprocess.run(
            [
                crypt4gh_bin,
                "encrypt",
                "--recipient_pk",
                str(recipient_pubkey),
            ],
            stdin=input_handle,
            stdout=output_handle,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
    if completed.returncode != 0:
        raise RuntimeError(
            f"crypt4gh failed with exit code {completed.returncode}: "
            f"{completed.stderr.strip()}"
        )


def _checksums(input_file: Path, output_file: Path, enabled: bool) -> tuple[str, str]:
    if not enabled:
        return "", ""
    return _sha256(input_file), _sha256(output_file)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * BYTES_IN_MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _metric(
    run_id: str,
    sample_id: str,
    input_file: Path,
    output_file: Path,
    input_size: int,
    output_size: int,
    seconds: float,
    throughput: float | None,
    sha256_input: str,
    sha256_c4gh: str,
    status: str,
    error: str,
) -> FileMetric:
    size_delta = output_size - input_size
    ratio = output_size / input_size if input_size else None
    overhead = (size_delta / input_size) * 100 if input_size else None
    return FileMetric(
        run_id=run_id,
        sample_id=sample_id,
        input_file=str(input_file),
        output_file=str(output_file),
        input_size_bytes=input_size,
        output_size_bytes=output_size,
        size_delta_bytes=size_delta,
        input_size_gib=round(input_size / BYTES_IN_GIB, 6),
        output_size_gib=round(output_size / BYTES_IN_GIB, 6),
        size_delta_mib=round(size_delta / BYTES_IN_MIB, 6),
        size_ratio=round(ratio, 8) if ratio is not None else None,
        overhead_percent=round(overhead, 6) if overhead is not None else None,
        encryption_seconds=round(seconds, 6),
        throughput_mib_s=round(throughput, 6) if throughput is not None else None,
        sha256_input=sha256_input,
        sha256_c4gh=sha256_c4gh,
        status=status,
        error=error,
    )


def _throughput(input_size: int, seconds: float) -> float | None:
    if seconds <= 0:
        return None
    return (input_size / BYTES_IN_MIB) / seconds


def _write_metrics(metrics_file: Path, metrics: list[FileMetric]) -> None:
    with metrics_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=METRIC_FIELDS, delimiter="\t")
        writer.writeheader()
        for metric in metrics:
            writer.writerow(asdict(metric))


def _build_result(
    run_id: str,
    output_dir: Path,
    metrics_file: Path,
    summary_file: Path,
    manifest_file: Path,
    log_file: Path,
    plots_dir: Path | None,
    metrics: list[FileMetric],
) -> EncryptionResult:
    ok_metrics = [metric for metric in metrics if metric.status == "ok"]
    output_metrics = [
        metric for metric in metrics if metric.status in {"ok", "skipped_existing"}
    ]
    return EncryptionResult(
        run_id=run_id,
        output_dir=output_dir,
        metrics_file=metrics_file,
        summary_file=summary_file,
        manifest_file=manifest_file,
        log_file=log_file,
        plots_dir=plots_dir,
        discovered=len(metrics),
        encrypted=len(ok_metrics),
        skipped=sum(metric.status == "skipped_existing" for metric in metrics),
        failed=sum(metric.status == "failed" for metric in metrics),
        total_input_bytes=sum(metric.input_size_bytes for metric in metrics),
        total_output_bytes=sum(metric.output_size_bytes for metric in output_metrics),
        total_encryption_seconds=sum(metric.encryption_seconds for metric in ok_metrics),
    )


def _write_summary(
    summary_file: Path,
    result: EncryptionResult,
    config: EncryptionConfig,
    input_dir: Path,
    recipient_pubkey: Path,
    crypt4gh_bin: Path,
) -> None:
    throughput = _throughput(result.total_input_bytes, result.total_encryption_seconds)
    ratio = (
        result.total_output_bytes / result.total_input_bytes
        if result.total_input_bytes
        else None
    )
    overhead = (
        ((result.total_output_bytes - result.total_input_bytes) / result.total_input_bytes)
        * 100
        if result.total_input_bytes
        else None
    )
    lines = [
        "Go-IMPaCT LocalEGA Crypt4GH encryption report",
        f"Run ID: {result.run_id}",
        f"Input directory: {input_dir}",
        f"Output directory: {result.output_dir}",
        f"Recipient public key: {recipient_pubkey}",
        f"Crypt4GH executable: {crypt4gh_bin}",
        f"Pattern: {config.pattern}",
        f"Input list: {config.input_list.expanduser().resolve() if config.input_list else 'NA'}",
        f"Dry-run: {config.dry_run}",
        f"Checksums: {config.compute_checksums}",
        "",
        f"Files discovered: {result.discovered}",
        f"Encrypted OK: {result.encrypted}",
        f"Skipped existing: {result.skipped}",
        f"Failed: {result.failed}",
        "",
        f"Total input GiB: {result.total_input_bytes / BYTES_IN_GIB:.3f}",
        f"Total output GiB: {result.total_output_bytes / BYTES_IN_GIB:.3f}",
        f"Overall size ratio: {'NA' if ratio is None else f'{ratio:.8f}'}",
        f"Overall overhead percent: {'NA' if overhead is None else f'{overhead:.6f}'}",
        f"Total encryption seconds: {result.total_encryption_seconds:.6f}",
        f"Overall throughput MiB/s: {'NA' if throughput is None else f'{throughput:.3f}'}",
        "",
        f"Metrics TSV: {result.metrics_file}",
        f"Manifest JSON: {result.manifest_file}",
        f"Run log: {result.log_file}",
    ]
    if result.plots_dir is not None:
        lines.append(f"Plots directory: {result.plots_dir}")
    summary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_manifest(
    manifest_file: Path,
    result: EncryptionResult,
    config: EncryptionConfig,
    input_dir: Path,
    recipient_pubkey: Path,
    crypt4gh_bin: Path,
    metrics: list[FileMetric],
) -> None:
    payload = {
        "run": {
            "run_id": result.run_id,
            "input_dir": str(input_dir),
            "output_dir": str(result.output_dir),
            "recipient_pubkey": str(recipient_pubkey),
            "crypt4gh_bin": str(crypt4gh_bin),
            "pattern": config.pattern,
            "input_list": (
                str(config.input_list.expanduser().resolve())
                if config.input_list is not None
                else None
            ),
            "dry_run": config.dry_run,
            "force": config.force,
            "compute_checksums": config.compute_checksums,
        },
        "summary": {
            "files_discovered": result.discovered,
            "encrypted": result.encrypted,
            "skipped": result.skipped,
            "failed": result.failed,
            "total_input_bytes": result.total_input_bytes,
            "total_output_bytes": result.total_output_bytes,
            "total_encryption_seconds": result.total_encryption_seconds,
        },
        "files": [asdict(metric) for metric in metrics],
    }
    manifest_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _write_plots(plots_dir: Path | None, metrics: list[FileMetric]) -> None:
    if plots_dir is None:
        return
    plot_metrics = [
        metric for metric in metrics if metric.status in {"ok", "skipped_existing"}
    ]
    if not plot_metrics:
        LOGGER.warning("No successful metrics available for plot generation")
        return
    try:
        import matplotlib.pyplot as plt
    except ImportError:
        LOGGER.warning("matplotlib is not installed; skipping plot generation")
        return

    plots_dir.mkdir(parents=True, exist_ok=True)
    labels = [_short_label(metric) for metric in plot_metrics]

    _bar_plot(
        plt=plt,
        output=plots_dir / "input_vs_output_size_gib.png",
        title="Input vs encrypted size",
        ylabel="GiB",
        labels=labels,
        series=[
            ("input", [metric.input_size_gib for metric in plot_metrics]),
            ("encrypted", [metric.output_size_gib for metric in plot_metrics]),
        ],
    )
    _bar_plot(
        plt=plt,
        output=plots_dir / "overhead_percent.png",
        title="Crypt4GH size overhead",
        ylabel="Percent",
        labels=labels,
        series=[
            (
                "overhead",
                [
                    metric.overhead_percent
                    if metric.overhead_percent is not None
                    else 0
                    for metric in plot_metrics
                ],
            )
        ],
    )
    encrypted_metrics = [metric for metric in plot_metrics if metric.status == "ok"]
    if encrypted_metrics:
        _bar_plot(
            plt=plt,
            output=plots_dir / "throughput_mib_s.png",
            title="Encryption throughput",
            ylabel="MiB/s",
            labels=[_short_label(metric) for metric in encrypted_metrics],
            series=[
                (
                    "throughput",
                    [
                        metric.throughput_mib_s
                        if metric.throughput_mib_s is not None
                        else 0
                        for metric in encrypted_metrics
                    ],
                )
            ],
        )


def _bar_plot(plt, output: Path, title: str, ylabel: str, labels: list[str], series):
    width = 0.8 / len(series)
    x_values = list(range(len(labels)))
    fig_width = max(8, len(labels) * 1.2)
    _, axis = plt.subplots(figsize=(fig_width, 5))
    for index, (name, values) in enumerate(series):
        offsets = [x + (index - (len(series) - 1) / 2) * width for x in x_values]
        axis.bar(offsets, values, width=width, label=name)
    axis.set_title(title)
    axis.set_ylabel(ylabel)
    axis.set_xticks(x_values)
    axis.set_xticklabels(labels, rotation=30, ha="right")
    axis.grid(axis="y", linestyle="--", linewidth=0.5, alpha=0.5)
    if len(series) > 1:
        axis.legend()
    plt.tight_layout()
    plt.savefig(output, dpi=150)
    plt.close()


def _short_label(metric: FileMetric) -> str:
    name = Path(metric.input_file).name
    return name.replace(".fastq.gz", "").replace(".fq.gz", "")


def _attach_run_log(log_file: Path) -> logging.Handler:
    handler = logging.FileHandler(log_file, encoding="utf-8")
    handler.setLevel(logging.DEBUG)
    handler.setFormatter(
        logging.Formatter("[%(asctime)s] %(name)-28s [%(levelname)-8s] %(message)s")
    )
    logging.getLogger().addHandler(handler)
    return handler


def _detach_run_log(handler: logging.Handler) -> None:
    logging.getLogger().removeHandler(handler)
    handler.close()

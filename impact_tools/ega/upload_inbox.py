"""SFTP upload workflow for LocalEGA/FEGA inbox files."""

from __future__ import annotations

import csv
import getpass
import hashlib
import json
import logging
import posixpath
import socket
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path


LOGGER = logging.getLogger(__name__)

BYTES_IN_MIB = 1024**2
BYTES_IN_GIB = 1024**3

UPLOAD_METRIC_FIELDS = [
    "run_id",
    "sample_id",
    "local_file",
    "remote_path",
    "local_size_bytes",
    "local_size_gib",
    "upload_seconds",
    "throughput_mib_s",
    "sha256_local",
    "status",
    "error",
]


@dataclass(frozen=True)
class InboxUploadConfig:
    """User-facing SFTP inbox upload settings."""

    input_dir: Path
    host: str
    username: str
    port: int = 2222
    output_dir: Path | None = None
    input_list: Path | None = None
    pattern: str = "*.c4gh"
    remote_dir: str = "/"
    remote_layout: str = "flat"
    identity_file: Path | None = None
    password: str | None = None
    ask_password: bool = False
    host_key_policy: str = "auto-add"
    force: bool = False
    dry_run: bool = False
    compute_checksums: bool = True
    fail_fast: bool = False
    connect_timeout: int = 30


@dataclass
class UploadMetric:
    """Per-file upload metrics."""

    run_id: str
    sample_id: str
    local_file: str
    remote_path: str
    local_size_bytes: int
    local_size_gib: float
    upload_seconds: float
    throughput_mib_s: float | None
    sha256_local: str
    status: str
    error: str


@dataclass
class InboxUploadResult:
    """Batch-level SFTP upload result."""

    run_id: str
    output_dir: Path
    metrics_file: Path
    summary_file: Path
    manifest_file: Path
    log_file: Path
    discovered: int
    uploaded: int
    skipped: int
    failed: int
    total_input_bytes: int
    total_upload_seconds: float


def run_inbox_upload(config: InboxUploadConfig) -> InboxUploadResult:
    """Upload selected encrypted files to a LocalEGA inbox over SFTP."""
    input_dir = config.input_dir.expanduser().resolve()
    output_dir = (
        config.output_dir.expanduser().resolve()
        if config.output_dir is not None
        else input_dir / "inbox_upload_reports"
    )
    run_id = datetime.now().strftime("%Y%m%d%H%M%S")
    output_dir.mkdir(parents=True, exist_ok=True)
    log_file = output_dir / f"inbox_upload_{run_id}.log"
    file_handler = _attach_run_log(log_file)
    try:
        _validate_config(config, input_dir)
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

        LOGGER.info("Starting inbox SFTP upload")
        LOGGER.info("Run ID: %s", run_id)
        LOGGER.info("Input directory: %s", input_dir)
        LOGGER.info("Output directory: %s", output_dir)
        LOGGER.info("Inbox: %s@%s:%s", config.username, config.host, config.port)
        LOGGER.info("Remote directory: %s", config.remote_dir)
        LOGGER.info("Remote layout: %s", config.remote_layout)
        if config.input_list is not None:
            LOGGER.info("Input list: %s", config.input_list.expanduser().resolve())
        LOGGER.info("Files discovered: %s", len(files))

        metrics: list[UploadMetric] = []
        if config.dry_run:
            for local_file in files:
                metrics.append(_dry_run_metric(run_id, local_file, input_dir, config))
        else:
            with _open_sftp(config) as sftp:
                for local_file in files:
                    metric = _upload_file(run_id, local_file, input_dir, config, sftp)
                    metrics.append(metric)
                    if metric.status == "failed" and config.fail_fast:
                        LOGGER.error("Stopping after first failure because --fail-fast is set")
                        break

        metrics_file = output_dir / f"inbox_upload_metrics_{run_id}.tsv"
        summary_file = output_dir / f"inbox_upload_summary_{run_id}.txt"
        manifest_file = output_dir / f"inbox_upload_manifest_{run_id}.json"

        _write_metrics(metrics_file, metrics)
        result = _build_result(
            run_id=run_id,
            output_dir=output_dir,
            metrics_file=metrics_file,
            summary_file=summary_file,
            manifest_file=manifest_file,
            log_file=log_file,
            metrics=metrics,
        )
        _write_summary(summary_file, result, config, input_dir)
        _write_manifest(manifest_file, result, config, input_dir, metrics)
        LOGGER.info("Metrics written to %s", metrics_file)
        LOGGER.info("Summary written to %s", summary_file)
        LOGGER.info("Manifest written to %s", manifest_file)
        LOGGER.info("Run log written to %s", log_file)
        return result
    finally:
        _detach_run_log(file_handler)


def _validate_config(config: InboxUploadConfig, input_dir: Path) -> None:
    if not input_dir.is_dir():
        raise NotADirectoryError(f"Input directory does not exist: {input_dir}")
    if config.remote_layout not in {"flat", "relative"}:
        raise ValueError("--remote-layout must be one of: flat, relative")
    if config.port <= 0:
        raise ValueError("--port must be a positive integer")
    if config.host_key_policy not in {"auto-add", "reject"}:
        raise ValueError("--host-key-policy must be one of: auto-add, reject")
    if config.identity_file is not None and not config.identity_file.expanduser().is_file():
        raise FileNotFoundError(f"Identity file does not exist: {config.identity_file}")


def _collect_input_files(
    input_dir: Path,
    output_dir: Path,
    pattern: str,
    input_list: Path | None,
) -> list[Path]:
    if input_list is not None:
        return _read_input_list(input_list.expanduser().resolve(), input_dir, output_dir)

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
            if candidate in seen:
                LOGGER.warning(
                    "Ignoring duplicate input list entry at %s:%s: %s",
                    input_list,
                    line_number,
                    candidate,
                )
                continue
            files.append(candidate)
            seen.add(candidate)
    return files


def _open_sftp(config: InboxUploadConfig):
    try:
        import paramiko
    except ImportError as exc:
        raise RuntimeError(
            "paramiko is required for inbox uploads. Install impact-tools with "
            "the SFTP dependencies or add paramiko to the environment."
        ) from exc

    password = config.password
    if config.ask_password and password is None:
        password = getpass.getpass(f"EGA password for {config.username}@{config.host}: ")

    client = paramiko.SSHClient()
    if config.host_key_policy == "auto-add":
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

    kwargs = {
        "hostname": config.host,
        "port": config.port,
        "username": config.username,
        "timeout": config.connect_timeout,
        "look_for_keys": config.identity_file is None and password is None,
        "allow_agent": config.identity_file is None,
    }
    if config.identity_file is not None:
        kwargs["key_filename"] = str(config.identity_file.expanduser().resolve())
    if password is not None:
        kwargs["password"] = password

    LOGGER.info("Opening SFTP connection to %s:%s", config.host, config.port)
    try:
        client.connect(**kwargs)
    except paramiko.ssh_exception.AuthenticationException:
        if password is None:
            raise
        LOGGER.info("Password auth failed; trying keyboard-interactive auth")
        client.close()
        client = _keyboard_interactive_client(paramiko, config, password)
    return _ManagedSFTPClient(client)


def _keyboard_interactive_client(paramiko, config: InboxUploadConfig, password: str):
    client = paramiko.SSHClient()
    if config.host_key_policy == "auto-add":
        client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    else:
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())

    sock = socket.create_connection((config.host, config.port), timeout=config.connect_timeout)
    transport = paramiko.Transport(sock)
    transport.start_client(timeout=config.connect_timeout)

    if config.host_key_policy == "reject":
        host_key = transport.get_remote_server_key()
        host_keys = client.get_host_keys()
        expected = host_keys.lookup(config.host)
        if expected is None or host_key.get_name() not in expected:
            raise paramiko.ssh_exception.SSHException(
                f"Server host key for {config.host} is not known"
            )

    def handler(_title, _instructions, prompts):
        return [password for _prompt, _echo in prompts]

    transport.auth_interactive(config.username, handler)
    if not transport.is_authenticated():
        raise paramiko.ssh_exception.AuthenticationException(
            f"Could not authenticate {config.username}@{config.host}"
        )
    client._transport = transport  # noqa: SLF001 - Paramiko exposes no public setter.
    return client


class _ManagedSFTPClient:
    def __init__(self, client):
        self.client = client
        self.sftp = None

    def __enter__(self):
        self.sftp = self.client.open_sftp()
        return self.sftp

    def __exit__(self, exc_type, exc, traceback):
        if self.sftp is not None:
            self.sftp.close()
        self.client.close()


def _dry_run_metric(
    run_id: str,
    local_file: Path,
    input_dir: Path,
    config: InboxUploadConfig,
) -> UploadMetric:
    remote_path = _remote_path(local_file, input_dir, config)
    size = local_file.stat().st_size
    LOGGER.info("Dry-run: %s -> %s", local_file, remote_path)
    return _metric(
        run_id=run_id,
        local_file=local_file,
        input_dir=input_dir,
        remote_path=remote_path,
        size=size,
        seconds=0,
        throughput=None,
        sha256_local="",
        status="dry_run",
        error="",
    )


def _upload_file(
    run_id: str,
    local_file: Path,
    input_dir: Path,
    config: InboxUploadConfig,
    sftp,
) -> UploadMetric:
    remote_path = _remote_path(local_file, input_dir, config)
    size = local_file.stat().st_size
    LOGGER.info("Uploading %s -> %s", local_file.relative_to(input_dir), remote_path)
    try:
        if _remote_exists(sftp, remote_path) and not config.force:
            LOGGER.warning("Skipping existing remote file: %s", remote_path)
            return _metric(
                run_id=run_id,
                local_file=local_file,
                input_dir=input_dir,
                remote_path=remote_path,
                size=size,
                seconds=0,
                throughput=None,
                sha256_local=_sha256(local_file) if config.compute_checksums else "",
                status="skipped_existing",
                error="",
            )
        _ensure_remote_parent(sftp, posixpath.dirname(remote_path))
        start = time.perf_counter()
        sftp.put(str(local_file), remote_path)
        seconds = time.perf_counter() - start
        throughput = _throughput(size, seconds)
        LOGGER.info(
            "Uploaded %s in %.3fs at %s MiB/s",
            local_file.relative_to(input_dir),
            seconds,
            "NA" if throughput is None else f"{throughput:.3f}",
        )
        return _metric(
            run_id=run_id,
            local_file=local_file,
            input_dir=input_dir,
            remote_path=remote_path,
            size=size,
            seconds=seconds,
            throughput=throughput,
            sha256_local=_sha256(local_file) if config.compute_checksums else "",
            status="ok",
            error="",
        )
    except Exception as exc:  # noqa: BLE001 - logged and converted to metric
        LOGGER.exception("Upload failed for %s", local_file)
        return _metric(
            run_id=run_id,
            local_file=local_file,
            input_dir=input_dir,
            remote_path=remote_path,
            size=size,
            seconds=0,
            throughput=None,
            sha256_local=_sha256(local_file) if config.compute_checksums else "",
            status="failed",
            error=str(exc),
        )


def _remote_path(local_file: Path, input_dir: Path, config: InboxUploadConfig) -> str:
    remote_dir = _normalize_remote_dir(config.remote_dir)
    if config.remote_layout == "flat":
        return posixpath.join(remote_dir, local_file.name)
    relative_path = local_file.relative_to(input_dir).as_posix()
    return posixpath.join(remote_dir, relative_path)


def _normalize_remote_dir(remote_dir: str) -> str:
    normalized = "/" + remote_dir.strip("/")
    return "/" if normalized == "/" else normalized


def _remote_exists(sftp, remote_path: str) -> bool:
    try:
        sftp.stat(remote_path)
        return True
    except OSError:
        return False


def _ensure_remote_parent(sftp, remote_parent: str) -> None:
    if remote_parent in {"", "/"}:
        return
    parts = [part for part in remote_parent.split("/") if part]
    current = ""
    for part in parts:
        current = f"{current}/{part}"
        try:
            sftp.stat(current)
        except OSError:
            sftp.mkdir(current)


def _metric(
    run_id: str,
    local_file: Path,
    input_dir: Path,
    remote_path: str,
    size: int,
    seconds: float,
    throughput: float | None,
    sha256_local: str,
    status: str,
    error: str,
) -> UploadMetric:
    return UploadMetric(
        run_id=run_id,
        sample_id=_sample_id_for_file(local_file, input_dir),
        local_file=str(local_file),
        remote_path=remote_path,
        local_size_bytes=size,
        local_size_gib=round(size / BYTES_IN_GIB, 6),
        upload_seconds=round(seconds, 6),
        throughput_mib_s=round(throughput, 6) if throughput is not None else None,
        sha256_local=sha256_local,
        status=status,
        error=error,
    )


def _sample_id_for_file(local_file: Path, input_dir: Path) -> str:
    relative_parent = local_file.relative_to(input_dir).parent
    if relative_parent == Path("."):
        return input_dir.name
    return str(relative_parent)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(8 * BYTES_IN_MIB), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _throughput(size: int, seconds: float) -> float | None:
    if seconds <= 0:
        return None
    return (size / BYTES_IN_MIB) / seconds


def _write_metrics(metrics_file: Path, metrics: list[UploadMetric]) -> None:
    with metrics_file.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=UPLOAD_METRIC_FIELDS, delimiter="\t")
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
    metrics: list[UploadMetric],
) -> InboxUploadResult:
    ok_metrics = [metric for metric in metrics if metric.status == "ok"]
    return InboxUploadResult(
        run_id=run_id,
        output_dir=output_dir,
        metrics_file=metrics_file,
        summary_file=summary_file,
        manifest_file=manifest_file,
        log_file=log_file,
        discovered=len(metrics),
        uploaded=len(ok_metrics),
        skipped=sum(metric.status == "skipped_existing" for metric in metrics),
        failed=sum(metric.status == "failed" for metric in metrics),
        total_input_bytes=sum(metric.local_size_bytes for metric in metrics),
        total_upload_seconds=sum(metric.upload_seconds for metric in ok_metrics),
    )


def _write_summary(
    summary_file: Path,
    result: InboxUploadResult,
    config: InboxUploadConfig,
    input_dir: Path,
) -> None:
    throughput = _throughput(result.total_input_bytes, result.total_upload_seconds)
    lines = [
        "Go-IMPaCT LocalEGA inbox upload report",
        f"Run ID: {result.run_id}",
        f"Input directory: {input_dir}",
        f"Output directory: {result.output_dir}",
        f"Inbox host: {config.host}",
        f"Inbox port: {config.port}",
        f"Inbox username: {config.username}",
        f"Remote directory: {config.remote_dir}",
        f"Remote layout: {config.remote_layout}",
        f"Input list: {config.input_list.expanduser().resolve() if config.input_list else 'NA'}",
        f"Pattern: {config.pattern}",
        f"Dry-run: {config.dry_run}",
        f"Checksums: {config.compute_checksums}",
        "",
        f"Files discovered: {result.discovered}",
        f"Uploaded OK: {result.uploaded}",
        f"Skipped existing: {result.skipped}",
        f"Failed: {result.failed}",
        "",
        f"Total input GiB: {result.total_input_bytes / BYTES_IN_GIB:.3f}",
        f"Total upload seconds: {result.total_upload_seconds:.6f}",
        f"Overall throughput MiB/s: {'NA' if throughput is None else f'{throughput:.3f}'}",
        "",
        f"Metrics TSV: {result.metrics_file}",
        f"Manifest JSON: {result.manifest_file}",
        f"Run log: {result.log_file}",
    ]
    summary_file.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _write_manifest(
    manifest_file: Path,
    result: InboxUploadResult,
    config: InboxUploadConfig,
    input_dir: Path,
    metrics: list[UploadMetric],
) -> None:
    payload = {
        "run": {
            "run_id": result.run_id,
            "input_dir": str(input_dir),
            "output_dir": str(result.output_dir),
            "host": config.host,
            "port": config.port,
            "username": config.username,
            "remote_dir": config.remote_dir,
            "remote_layout": config.remote_layout,
            "input_list": (
                str(config.input_list.expanduser().resolve())
                if config.input_list is not None
                else None
            ),
            "pattern": config.pattern,
            "dry_run": config.dry_run,
            "force": config.force,
            "compute_checksums": config.compute_checksums,
            "host_key_policy": config.host_key_policy,
        },
        "summary": {
            "files_discovered": result.discovered,
            "uploaded": result.uploaded,
            "skipped": result.skipped,
            "failed": result.failed,
            "total_input_bytes": result.total_input_bytes,
            "total_upload_seconds": result.total_upload_seconds,
        },
        "files": [asdict(metric) for metric in metrics],
    }
    manifest_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")


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

"""Command line interface for impact-tools."""

from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install as install_rich_traceback

from impact_tools import __version__
from impact_tools.beacon import ingest as beacon_ingest
from impact_tools.beacon import liftover as beacon_liftover
from impact_tools.beacon import pgx as beacon_pgx
from impact_tools.config import (
    EXTRA_CONFIG_PATH,
    get_config_value,
    include_extra_config,
    load_configuration,
    remove_extra_config,
)
from impact_tools.ega.benchmark import compare_runs
from impact_tools.ega.encrypt import EncryptionConfig, run_encryption
from impact_tools.ega.registry import DEFAULT_REGISTRY_PATH, EgaRegistry
from impact_tools.ega.slurm import (
    SlurmEncryptionPlanConfig,
    submit_slurm_job,
    write_slurm_encryption_plan,
)
from impact_tools.ega.upload_inbox import InboxUploadConfig, run_inbox_upload
from impact_tools.ega.workflow import EncryptUploadConfig, run_encrypt_upload

log = logging.getLogger(__name__)


def _configured_path(
    configuration: dict,
    path: str,
) -> Path | None:
    """Return an expanded Path from configuration when a value is set."""
    value = get_config_value(configuration, path)
    return Path(value).expanduser() if value else None


def slurm_config_value(slurm_conf: dict, key: str, override, default=None):
    """Return CLI override when present, otherwise the configured value."""
    if override is not None:
        return override
    return slurm_conf.get(key, default)


def configure_logging(verbose: bool, log_file: Path | None) -> None:
    """Configure root logging for console and optional file output."""
    level = logging.DEBUG if verbose else logging.INFO
    console_handler = RichHandler(
        console=Console(stderr=True),
        rich_tracebacks=True,
        show_path=verbose,
        markup=True,
    )
    console_handler.setFormatter(logging.Formatter("%(message)s"))
    handlers: list[logging.Handler] = [
        console_handler
    ]
    if log_file is not None:
        log_file.parent.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(log_file, encoding="utf-8")
        file_handler.setFormatter(
            logging.Formatter(
                "[%(asctime)s] %(name)-28s [%(levelname)-8s] %(message)s"
            )
        )
        handlers.append(file_handler)

    logging.basicConfig(level=level, handlers=handlers, force=True)


def configure_module_logging(ctx: click.Context, module_name: str) -> Path | None:
    """Add a module-specific log file unless --log-file was provided."""
    if ctx.obj["log_file"] is not None:
        return ctx.obj["log_file"]

    logs_config = get_config_value(ctx.obj["configuration"], "logs", {}) or {}
    modules_outpath = logs_config.get("modules_outpath", {})
    log_directory = modules_outpath.get(module_name)
    if not log_directory:
        default_outpath = logs_config.get("default_outpath")
        if not default_outpath:
            return None
        log_directory = Path(default_outpath).expanduser() / module_name

    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
    log_file = (
        Path(log_directory).expanduser()
        / f"{module_name}_{timestamp}.log"
    )
    log_file.parent.mkdir(parents=True, exist_ok=True)
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setFormatter(
        logging.Formatter(
            "[%(asctime)s] %(name)-28s [%(levelname)-8s] %(message)s"
        )
    )
    logging.getLogger().addHandler(file_handler)
    ctx.obj["log_file"] = log_file
    log.debug("Module log: %s", log_file)
    return log_file


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
@click.option("-v", "--verbose", is_flag=True, help="Show debug log messages.")
@click.option(
    "--log-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write a detailed execution log to this file.",
)
@click.option(
    "--config-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help=(
        "Additional JSON/YAML configuration for this execution. User defaults "
        "from ~/.impact_tools/extra_config.json are loaded automatically."
    ),
)
@click.pass_context
def cli(
    ctx: click.Context,
    verbose: bool,
    log_file: Path | None,
    config_file: Path | None,
) -> None:
    """Utilities for Go-IMPaCT Beacon and EGA workflows."""
    install_rich_traceback(width=200, word_wrap=True, extra_lines=1)
    configuration = load_configuration(config_file)
    configure_logging(verbose=verbose, log_file=log_file)
    ctx.obj = {
        "verbose": verbose,
        "log_file": log_file,
        "configuration": configuration,
    }


@cli.command("add-extra-config")
@click.option(
    "-f",
    "--config-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="JSON or YAML file to save as persistent user configuration.",
)
@click.option(
    "-n",
    "--config-name",
    help="Store the input file under this top-level configuration section.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Replace or merge configuration sections that already exist.",
)
@click.option(
    "--remove-config",
    is_flag=True,
    help="Remove --config-name, or the complete file when used with --force.",
)
def add_extra_config_cmd(
    config_file: Path | None,
    config_name: str | None,
    force: bool,
    remove_config: bool,
) -> None:
    """Create or update ~/.impact_tools/extra_config.json."""
    try:
        if remove_config:
            if config_file is not None:
                raise click.UsageError(
                    "--config-file cannot be used together with --remove-config."
                )
            removed = remove_extra_config(config_name, force=force)
            if not removed:
                raise click.ClickException(
                    f"Configuration not found: {config_name or EXTRA_CONFIG_PATH}"
                )
            click.echo(f"Removed configuration from {EXTRA_CONFIG_PATH}")
            return
        if config_file is None:
            raise click.UsageError("--config-file is required.")
        path = include_extra_config(
            config_file,
            config_name=config_name,
            force=force,
        )
        click.echo(f"Extra configuration written to {path}")
    except (OSError, ValueError) as exc:
        raise click.ClickException(str(exc)) from exc


@cli.group()
def ega() -> None:
    """Tools for Affiliated EGA workflows."""


@ega.command("encrypt")
@click.option(
    "-i",
    "--input-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help=(
        "Directory containing sample folders or files to encrypt. Also used as "
        "the base directory for relative paths in --input-list."
    ),
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory for encrypted files, HTML report, metrics and logs.",
)
@click.option(
    "-k",
    "--recipient-pubkey",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Crypt4GH recipient public key, usually LocalEGA service.key.pub.",
)
@click.option(
    "--crypt4gh-bin",
    type=click.Path(path_type=Path, dir_okay=False),
    help="crypt4gh executable to use. Default: first crypt4gh found in PATH.",
)
@click.option(
    "--input-list",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help=(
        "Text file with one input file path per line. Empty lines and lines "
        "starting with # are ignored. Relative paths are resolved from input-dir."
    ),
)
@click.option(
    "--pattern",
    default="*.fastq.gz",
    show_default=True,
    help="Input file glob pattern.",
)
@click.option(
    "--sample-id",
    help="Sample identifier to use when input files are directly under input-dir.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Re-encrypt files even if the output .c4gh already exists.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Discover files and write a plan without running crypt4gh.",
)
@click.option(
    "--no-checksums",
    is_flag=True,
    help=(
        "Omit checksums from reports. The processing registry still requires "
        "SHA256 when enabled."
    ),
)
@click.option(
    "--no-plots",
    is_flag=True,
    help="Do not generate the legacy directory of PNG plots.",
)
@click.option(
    "--no-charts",
    is_flag=True,
    help="Omit embedded charts from the HTML report.",
)
@click.option(
    "--fail-fast",
    is_flag=True,
    help="Stop at the first failed file instead of continuing with the batch.",
)
@click.option(
    "--registry-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="SQLite registry used to track encrypted content across batches.",
)
@click.option(
    "--no-registry",
    is_flag=True,
    help="Disable persistent content tracking for this encryption run.",
)
@click.option(
    "--run-profile",
    type=click.Choice(["local", "ws", "hpc"]),
    help="Execution environment label recorded in metrics and manifests.",
)
@click.pass_context
def encrypt_cmd(
    ctx: click.Context,
    input_dir: Path | None,
    output_dir: Path | None,
    recipient_pubkey: Path | None,
    crypt4gh_bin: Path | None,
    input_list: Path | None,
    pattern: str,
    sample_id: str | None,
    force: bool,
    dry_run: bool,
    no_checksums: bool,
    no_plots: bool,
    no_charts: bool,
    fail_fast: bool,
    registry_file: Path | None,
    no_registry: bool,
    run_profile: str | None,
) -> None:
    """Encrypt sequencing files with Crypt4GH and generate metrics."""
    configure_module_logging(ctx, "ega_encrypt")
    configuration = ctx.obj["configuration"]
    input_dir = input_dir or _configured_path(configuration, "ega.encryption.input_dir")
    output_dir = output_dir or _configured_path(
        configuration, "ega.encryption.output_dir"
    )
    recipient_pubkey = recipient_pubkey or _configured_path(
        configuration, "ega.encryption.recipient_pubkey"
    )
    crypt4gh_bin = crypt4gh_bin or _configured_path(
        configuration, "ega.encryption.crypt4gh_bin"
    )
    registry_file = registry_file or _configured_path(
        configuration, "ega.registry_file"
    )
    if no_registry:
        registry_file = None
    run_profile = run_profile or get_config_value(
        configuration, "ega.execution.profile", "local"
    )
    if input_dir is None:
        raise click.UsageError(
            "--input-dir is required or must be set as ega.encryption.input_dir."
        )
    if recipient_pubkey is None:
        raise click.UsageError(
            "--recipient-pubkey is required or must be set as "
            "ega.encryption.recipient_pubkey."
        )
    config = EncryptionConfig(
        input_dir=input_dir,
        output_dir=output_dir,
        recipient_pubkey=recipient_pubkey,
        crypt4gh_bin=crypt4gh_bin,
        input_list=input_list,
        pattern=pattern,
        sample_id=sample_id,
        force=force,
        dry_run=dry_run,
        compute_checksums=not no_checksums,
        generate_plots=not no_plots,
        generate_report_charts=not no_charts,
        fail_fast=fail_fast,
        registry_file=registry_file,
        run_profile=run_profile,
    )
    try:
        result = run_encryption(config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts to clean error
        raise click.ClickException(str(exc)) from exc
    click.echo(f"HTML report: {result.report_file}")
    if result.failed > 0:
        raise click.ClickException(
            f"Encryption finished with {result.failed} failed file(s). "
            f"See log: {result.log_file}"
        )


@ega.command("upload-inbox")
@click.option(
    "-i",
    "--input-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help=(
        "Directory containing encrypted files to upload. Also used as the base "
        "directory for relative paths in --input-list."
    ),
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory for the HTML report, upload metrics, logs and manifests.",
)
@click.option("--host", help="Inbox SFTP host.")
@click.option("--port", type=int, help="Inbox SFTP port.")
@click.option("-u", "--username", help="Inbox username.")
@click.option(
    "--remote-dir",
    help="Remote inbox directory where files are uploaded.",
)
@click.option(
    "--remote-layout",
    type=click.Choice(["flat", "relative"]),
    help=(
        "Upload files directly into remote-dir (flat) or preserve paths relative "
        "to input-dir (relative)."
    ),
)
@click.option(
    "--input-list",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help=(
        "Text file with one encrypted file path per line. Empty lines and lines "
        "starting with # are ignored. Relative paths are resolved from input-dir."
    ),
)
@click.option(
    "--pattern",
    default="*.c4gh",
    show_default=True,
    help="Encrypted input file glob pattern.",
)
@click.option(
    "--identity-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="SSH private key for SFTP authentication.",
)
@click.option(
    "--ask-password",
    is_flag=True,
    help="Prompt interactively for the EGA password.",
)
@click.option(
    "--host-key-policy",
    type=click.Choice(["auto-add", "reject"]),
    help="How to handle unknown SFTP host keys.",
)
@click.option("--force", is_flag=True, help="Overwrite remote files if they exist.")
@click.option("--dry-run", is_flag=True, help="Plan uploads without connecting.")
@click.option(
    "--no-checksums",
    is_flag=True,
    help=(
        "Omit checksums from reports. The processing registry still requires "
        "SHA256 when enabled."
    ),
)
@click.option(
    "--no-charts",
    is_flag=True,
    help="Omit embedded charts from the HTML report.",
)
@click.option(
    "--fail-fast",
    is_flag=True,
    help="Stop at the first failed upload instead of continuing with the batch.",
)
@click.option(
    "--connect-timeout",
    type=int,
    help="SFTP connection timeout in seconds.",
)
@click.option(
    "--registry-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help=(
        "SQLite registry used to avoid uploading content already submitted in "
        "previous batches."
    ),
)
@click.option(
    "--no-registry",
    is_flag=True,
    help="Disable persistent content tracking for this upload.",
)
@click.option(
    "--run-profile",
    type=click.Choice(["local", "ws", "hpc"]),
    help="Execution environment label recorded in metrics and manifests.",
)
@click.pass_context
def upload_inbox_cmd(
    ctx: click.Context,
    input_dir: Path | None,
    output_dir: Path | None,
    host: str | None,
    port: int | None,
    username: str | None,
    remote_dir: str | None,
    remote_layout: str | None,
    input_list: Path | None,
    pattern: str,
    identity_file: Path | None,
    ask_password: bool,
    host_key_policy: str | None,
    force: bool,
    dry_run: bool,
    no_checksums: bool,
    no_charts: bool,
    fail_fast: bool,
    connect_timeout: int | None,
    registry_file: Path | None,
    no_registry: bool,
    run_profile: str | None,
) -> None:
    """Upload encrypted .c4gh files to a LocalEGA inbox over SFTP."""
    configure_module_logging(ctx, "ega_upload_inbox")
    configuration = ctx.obj["configuration"]
    input_dir = input_dir or _configured_path(configuration, "ega.inbox.input_dir")
    output_dir = output_dir or _configured_path(configuration, "ega.inbox.output_dir")
    host = host or get_config_value(configuration, "ega.inbox.host")
    port = port or int(get_config_value(configuration, "ega.inbox.port", 2222))
    username = username or get_config_value(configuration, "ega.inbox.username")
    remote_dir = remote_dir or get_config_value(
        configuration, "ega.inbox.remote_dir", "/"
    )
    remote_layout = remote_layout or get_config_value(
        configuration, "ega.inbox.remote_layout", "flat"
    )
    identity_file = identity_file or _configured_path(
        configuration, "ega.inbox.identity_file"
    )
    host_key_policy = host_key_policy or get_config_value(
        configuration, "ega.inbox.host_key_policy", "auto-add"
    )
    connect_timeout = connect_timeout or int(
        get_config_value(configuration, "ega.inbox.connect_timeout", 30)
    )
    registry_file = registry_file or _configured_path(
        configuration, "ega.registry_file"
    )
    if no_registry:
        registry_file = None
    run_profile = run_profile or get_config_value(
        configuration, "ega.execution.profile", "local"
    )
    if input_dir is None:
        raise click.UsageError(
            "--input-dir is required or must be set as ega.inbox.input_dir."
        )
    if not host:
        raise click.UsageError("--host is required or must be set as ega.inbox.host.")
    if not username:
        raise click.UsageError(
            "--username is required or must be set as ega.inbox.username."
        )
    config = InboxUploadConfig(
        input_dir=input_dir,
        output_dir=output_dir,
        input_list=input_list,
        pattern=pattern,
        host=host,
        port=port,
        username=username,
        remote_dir=remote_dir,
        remote_layout=remote_layout,
        identity_file=identity_file,
        ask_password=ask_password,
        host_key_policy=host_key_policy,
        force=force,
        dry_run=dry_run,
        compute_checksums=not no_checksums,
        generate_report_charts=not no_charts,
        fail_fast=fail_fast,
        connect_timeout=connect_timeout,
        registry_file=registry_file,
        run_profile=run_profile,
    )
    try:
        result = run_inbox_upload(config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts to clean error
        raise click.ClickException(str(exc)) from exc
    click.echo(f"HTML report: {result.report_file}")
    if result.failed > 0:
        raise click.ClickException(
            f"Inbox upload finished with {result.failed} failed file(s). "
            f"See log: {result.log_file}"
        )


@ega.command("encrypt-upload")
@click.option(
    "-i",
    "--input-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help="Directory containing the raw files or sample directories.",
)
@click.option(
    "--encrypted-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory where encrypted .c4gh files are written.",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory for the combined workflow report and upload reports.",
)
@click.option(
    "-k",
    "--recipient-pubkey",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="LocalEGA Crypt4GH recipient public key.",
)
@click.option(
    "--crypt4gh-bin",
    type=click.Path(path_type=Path, dir_okay=False),
    help="crypt4gh executable. Defaults to configuration or PATH.",
)
@click.option("--pattern", default="*.fastq.gz", show_default=True)
@click.option("--host", help="Inbox SFTP host.")
@click.option("--port", type=int, help="Inbox SFTP port.")
@click.option("-u", "--username", help="Inbox username.")
@click.option("--remote-dir", help="Remote Inbox directory.")
@click.option(
    "--remote-layout",
    type=click.Choice(["flat", "relative"]),
    help="Upload all files together or preserve their relative directories.",
)
@click.option(
    "--identity-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="SSH private key for Inbox authentication.",
)
@click.option("--ask-password", is_flag=True, help="Prompt for the EGA password.")
@click.option(
    "--run-profile",
    type=click.Choice(["local", "ws", "hpc"]),
    help="Execution environment label stored with the metrics.",
)
@click.option(
    "--registry-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="SQLite registry shared by encryption and upload.",
)
@click.option("--force", is_flag=True, help="Replace existing outputs.")
@click.option("--no-checksums", is_flag=True)
@click.option(
    "--no-charts",
    "--no-plots",
    is_flag=True,
    help="Omit charts from the HTML report.",
)
@click.option("--no-registry", is_flag=True)
@click.pass_context
def encrypt_upload_cmd(
    ctx: click.Context,
    input_dir: Path | None,
    encrypted_dir: Path | None,
    output_dir: Path | None,
    recipient_pubkey: Path | None,
    crypt4gh_bin: Path | None,
    pattern: str,
    host: str | None,
    port: int | None,
    username: str | None,
    remote_dir: str | None,
    remote_layout: str | None,
    identity_file: Path | None,
    ask_password: bool,
    run_profile: str | None,
    registry_file: Path | None,
    force: bool,
    no_checksums: bool,
    no_charts: bool,
    no_registry: bool,
) -> None:
    """Encrypt a batch and upload exactly the resulting files."""
    configure_module_logging(ctx, "ega_encrypt_upload")
    configuration = ctx.obj["configuration"]
    input_dir = input_dir or _configured_path(configuration, "ega.encryption.input_dir")
    encrypted_dir = encrypted_dir or _configured_path(
        configuration, "ega.encryption.output_dir"
    )
    recipient_pubkey = recipient_pubkey or _configured_path(
        configuration, "ega.encryption.recipient_pubkey"
    )
    crypt4gh_bin = crypt4gh_bin or _configured_path(
        configuration, "ega.encryption.crypt4gh_bin"
    )
    output_dir = output_dir or _configured_path(
        configuration, "ega.workflow.output_dir"
    )
    host = host or get_config_value(configuration, "ega.inbox.host")
    port = port or int(get_config_value(configuration, "ega.inbox.port", 2222))
    username = username or get_config_value(configuration, "ega.inbox.username")
    remote_dir = remote_dir or get_config_value(
        configuration, "ega.inbox.remote_dir", "/"
    )
    remote_layout = remote_layout or get_config_value(
        configuration, "ega.inbox.remote_layout", "flat"
    )
    identity_file = identity_file or _configured_path(
        configuration, "ega.inbox.identity_file"
    )
    run_profile = run_profile or get_config_value(
        configuration, "ega.execution.profile", "local"
    )
    if no_registry:
        registry_file = None
    else:
        registry_file = registry_file or _configured_path(
            configuration, "ega.registry_file"
        )

    if input_dir is None:
        raise click.UsageError("--input-dir is required or must be configured.")
    if recipient_pubkey is None:
        raise click.UsageError(
            "--recipient-pubkey is required or must be configured."
        )
    if not host or not username:
        raise click.UsageError("Inbox --host and --username are required.")
    encrypted_dir = encrypted_dir or input_dir / "encrypted_c4gh"
    output_dir = output_dir or encrypted_dir / "workflow_reports"

    workflow_config = EncryptUploadConfig(
        run_profile=run_profile,
        output_dir=output_dir,
        generate_report_charts=not no_charts,
        encryption=EncryptionConfig(
            input_dir=input_dir,
            output_dir=encrypted_dir,
            recipient_pubkey=recipient_pubkey,
            crypt4gh_bin=crypt4gh_bin,
            pattern=pattern,
            force=force,
            compute_checksums=not no_checksums,
            generate_plots=False,
            generate_report_charts=False,
            fail_fast=True,
            registry_file=registry_file,
            run_profile=run_profile,
        ),
        upload=InboxUploadConfig(
            input_dir=encrypted_dir,
            output_dir=output_dir / "upload",
            host=host,
            port=port,
            username=username,
            remote_dir=remote_dir,
            remote_layout=remote_layout,
            identity_file=identity_file,
            ask_password=ask_password,
            host_key_policy=get_config_value(
                configuration, "ega.inbox.host_key_policy", "auto-add"
            ),
            force=force,
            compute_checksums=not no_checksums,
            fail_fast=True,
            connect_timeout=int(
                get_config_value(configuration, "ega.inbox.connect_timeout", 30)
            ),
            registry_file=registry_file,
            run_profile=run_profile,
        ),
    )
    try:
        result = run_encrypt_upload(workflow_config)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Workflow manifest: {result.manifest_file}")
    click.echo(f"HTML report: {result.report_file}")
    if result.upload.failed:
        raise click.ClickException(
            f"Inbox upload finished with {result.upload.failed} failed file(s). "
            f"See log: {result.upload.log_file}"
        )


@ega.command("compare-runs")
@click.argument(
    "manifests",
    nargs=-1,
    required=True,
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("ega_run_comparison"),
    show_default=True,
)
@click.option(
    "--no-charts",
    "--no-plots",
    is_flag=True,
    help="Omit charts from the HTML comparison report.",
)
def compare_runs_cmd(
    manifests: tuple[Path, ...],
    output_dir: Path,
    no_charts: bool,
) -> None:
    """Compare encryption, Inbox upload and end-to-end run manifests."""
    try:
        result = compare_runs(
            manifests,
            output_dir,
            generate_charts=not no_charts,
        )
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Compared runs: {result.runs}")
    click.echo(f"Metrics: {result.metrics_file}")
    click.echo(f"HTML report: {result.report_file}")


@ega.command("processing-history")
@click.option(
    "--registry-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="SQLite EGA processing registry to inspect.",
)
@click.option(
    "--stage",
    type=click.Choice(["all", "encrypted", "uploaded"]),
    default="all",
    show_default=True,
    help="Processing stage to display.",
)
@click.option(
    "--limit",
    type=click.IntRange(min=1),
    default=100,
    show_default=True,
    help="Maximum number of recent successful processing events to show.",
)
@click.pass_context
def processing_history_cmd(
    ctx: click.Context,
    registry_file: Path | None,
    stage: str,
    limit: int,
) -> None:
    """Show successful encryption and upload events across EGA batches."""
    configuration = ctx.obj["configuration"]
    registry_file = registry_file or _configured_path(
        configuration, "ega.registry_file"
    ) or DEFAULT_REGISTRY_PATH
    if not registry_file.expanduser().exists():
        raise click.ClickException(
            f"Processing registry does not exist: {registry_file}"
        )

    with EgaRegistry(registry_file) as registry:
        rows: list[tuple[str, str, str, str, str, str]] = []
        if stage in {"all", "encrypted"}:
            rows.extend(
                (
                    record.completed_at,
                    "encrypted",
                    record.sample_id,
                    record.output_path,
                    record.source_sha256,
                    record.input_path,
                )
                for record in registry.list_encryptions(limit=limit)
            )
        if stage in {"all", "uploaded"}:
            rows.extend(
                (
                    record.completed_at,
                    "uploaded",
                    record.sample_id,
                    f"{record.endpoint}{record.remote_path}",
                    record.sha256,
                    record.local_path,
                )
                for record in registry.list_uploads(limit=limit)
            )

    rows.sort(key=lambda row: row[0], reverse=True)
    click.echo("completed_at\tstage\tsample_id\tdestination\tsha256\tsource")
    for row in rows[:limit]:
        click.echo("\t".join(row))


@cli.group()
def beacon() -> None:
    """Tools for Beacon workflows."""


@beacon.command("liftover")
@click.option(
    "-b",
    "--base-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=Path("."),
    show_default="current working directory",
    help="Base working directory. Defaults to the current directory.",
)
@click.option(
    "--chain",
    type=click.Path(path_type=Path, dir_okay=False),
    help=(
        "Chain file for liftover. "
        "Defaults to <base-dir>/liftover/resources/hg19ToHg38.over.chain.gz."
    ),
)
@click.option(
    "--fasta",
    type=click.Path(path_type=Path, dir_okay=False),
    help=(
        "Reference FASTA for target build. "
        "Defaults to <base-dir>/liftover/resources/GRCh38_full_analysis_set_plus_decoy_hla.fa."
    ),
)
@click.option(
    "--hpc-mount",
    default="/data/ucct/bi",
    show_default=True,
    help="HPC mount point passed as a read-only Docker volume to resolve input symlinks.",
)
@click.option(
    "--bcftools-image",
    default=beacon_liftover.BCFTOOLS_IMAGE,
    show_default=True,
    help="Docker image for bcftools.",
)
@click.option(
    "--crossmap-image",
    default=beacon_liftover.CROSSMAP_IMAGE,
    show_default=True,
    help="Docker image for CrossMap.",
)
@click.option(
    "--cleanup",
    is_flag=True,
    help="Remove intermediate files after the pipeline completes.",
)
@click.option(
    "-w",
    "--workers",
    default=4,
    show_default=True,
    help="Parallel worker threads for processing multiple samples.",
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Only check inputs and detected genome builds; do not run liftover.",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Continue without interactive confirmation.",
)
@click.pass_context
def liftover_cmd(
    ctx: click.Context,
    base_dir: Path,
    chain: Path | None,
    fasta: Path | None,
    hpc_mount: str,
    bcftools_image: str,
    crossmap_image: str,
    cleanup: bool,
    workers: int,
    check_only: bool,
    yes: bool,
) -> None:
    """Run Beacon liftover workflow (GRCh37 -> GRCh38) using CrossMap via Docker.

    The command first validates input VCFs and detects their genome build.
    If all inputs are already GRCh38, liftover is skipped.
    """
    configure_module_logging(ctx, "beacon_liftover")
    base_dir = base_dir.resolve()

    if check_only:
        try:
            beacon_liftover.validate_liftover_layout(base_dir)
        except ValueError as exc:
            raise click.ClickException(
                f"{exc}\n"
                "Please create the required directory layout by running:\n"
                f"  impact-tools beacon liftover --base-dir {base_dir}"
            ) from exc
    else:
        try:
            beacon_liftover.validate_liftover_layout(base_dir)
        except ValueError:
            log.warning("Liftover directories not found. Creating workspace layout...")
            beacon_liftover.create_liftover_layout(base_dir)

    try:
        check_results = beacon_liftover.check_liftover_inputs(base_dir)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts to clean error
        raise click.ClickException(str(exc)) from exc

    log.info("==========================================")
    log.info("Beacon liftover")
    log.info("==========================================")
    log.info("Base directory: %s", base_dir)
    log.info("Target build:   GRCh38")
    log.info("==========================================")

    detected_builds: set[str] = set()

    for vcf, build, contig_style, chr1_length in check_results:
        detected_builds.add(build)
        log.info(
            "  %s: build=%s, contig_style=%s, chr1_length=%s",
            vcf.name,
            build,
            contig_style,
            chr1_length if chr1_length is not None else "N/A",
        )

    log.info("==========================================")

    sample_ids = [
        vcf.name.removesuffix(".vcf.gz").removesuffix(".clean").removesuffix(".GRCh38")
        for vcf, _, _, _ in check_results
    ]

    already_lifted = any(
        (base_dir / "liftover" / f"{sample_id}.GRCh38.clean.vcf.gz").exists()
        for sample_id in sample_ids
    )

    if cleanup and already_lifted:
        intermediates = beacon_liftover.list_intermediates(
            base_dir / "liftover", sample_ids
        )
        if not intermediates:
            log.info("No intermediate files found for cleanup.")
            return
        log.info("Intermediate files found for cleanup: (%d)", len(intermediates))
        for p in intermediates:
            log.info("  %s", p.name)
        if yes or click.confirm("Delete these intermediate files?", default=False):
            beacon_liftover.cleanup_intermediates(
                base_dir / "liftover", sample_ids
            )
        else:
            log.info("Cleanup cancelled.")
        return

    if "unknown" in detected_builds:
        log.warning("At least one VCF build could not be detected.")

    if len(detected_builds) > 1:
        log.warning(
            "Mixed or uncertain builds detected: %s",
            ", ".join(sorted(detected_builds)),
        )

    if detected_builds == {"GRCh38"}:
        log.info("Status: input already matches GRCh38. Liftover not required.")
        if check_only:
            log.info("Check completed.")
        return
    elif detected_builds == {"GRCh37"}:
        log.info("Status: liftover GRCh37 -> GRCh38 is required.")
    else:
        log.warning("Status: manual review recommended before running liftover.")

    if check_only:
        log.info("Check completed. Liftover was not executed because --check was used.")
        return

    if not yes:
        if not click.confirm("Continue with liftover execution?", default=False):
            log.info("Cancelled.")
            return

    config = beacon_liftover.LiftoverConfig(
        base_dir=base_dir,
        chain=chain,
        fasta=fasta,
        hpc_mount=hpc_mount,
        bcftools_image=bcftools_image,
        crossmap_image=crossmap_image,
        workers=workers,
    )

    try:
        result = beacon_liftover.run_liftover(config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts to clean error
        raise click.ClickException(str(exc)) from exc

    log.info("==========================================")
    log.info("Liftover summary")
    log.info("==========================================")
    log.info("  Samples OK:       %d", len(result.results) - result.failed - result.warned)
    log.info("  Samples warnings: %d", result.warned)
    log.info("  Samples failed:   %d", result.failed)

    if result.failed > 0:
        raise click.ClickException(
            f"Liftover finished with {result.failed} failed sample(s). "
            "Check logs in <base-dir>/logs/ for details."
        )

    intermediates = beacon_liftover.list_intermediates(
        base_dir / "liftover", sample_ids
    )
    if intermediates:
        log.info("Intermediate files generated (%d):", len(intermediates))
        for p in intermediates:
            log.info("  %s", p.name)
        if cleanup or yes or click.confirm("Remove intermediate files?", default=True):
            beacon_liftover.cleanup_intermediates(
                base_dir / "liftover", sample_ids
            )

@beacon.command("pgx")
@click.option(
    "-b",
    "--base-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=Path("."),
    show_default="current working directory",
    help="Base working directory. Defaults to the current directory.",
)
@click.option(
    "--country-code",
    default="ES",
    show_default=True,
    help="ISO 3166-1 alpha-2 country code written into each workspace samples.tsv.",
)
@click.option(
    "--sex-ambiguous-min",
    default=beacon_pgx.DEFAULT_SEX_AMBIGUOUS_MIN,
    show_default=True,
    help="Lower bound of the ambiguous sex zone (manual input required).",
)
@click.option(
    "--sex-ambiguous-max",
    default=beacon_pgx.DEFAULT_SEX_AMBIGUOUS_MAX,
    show_default=True,
    help="Upper bound of the ambiguous sex zone (manual input required).",
)
@click.option(
    "--bcftools-image",
    default=beacon_pgx.BCFTOOLS_IMAGE,
    show_default=True,
    help="Docker image for bcftools (used for chrY sex inference).",
)
@click.option(
    "--pgx-image",
    default=beacon_pgx.PGX_IMAGE,
    show_default=True,
    help="Docker image for pgx_pilot.",
)
@click.option(
    "--pgx-repo",
    type=click.Path(path_type=Path, file_okay=False),
    envvar="PGX_REPO",
    help=(
        "Path to the pgx_pilot repository. "
        "scripts/ and resources/ are mounted read-only into each run container. "
        "Can also be set via the PGX_REPO environment variable."
    ),
)
@click.option(
    "--snakemake-jobs",
    default=8,
    show_default=True,
    help="Parallel jobs passed to Snakemake (-j).",
)
@click.option(
    "-w",
    "--workers",
    default=4,
    show_default=True,
    help="Parallel worker threads (sex inference, workspace prep, pgx runs).",
)
@click.option(
    "--prepare",
    is_flag=True,
    help="Only prepare workspaces and samples.tsv; do not run pgx_pilot.",
)
@click.option(
    "--run",
    is_flag=True,
    help="Only run pgx_pilot; skip workspace preparation (workspaces must already exist).",
)
@click.option(
    "-y",
    "--yes",
    is_flag=True,
    help="Continue without interactive confirmation.",
)
@click.pass_context
def pgx_cmd(
    ctx: click.Context,
    base_dir: Path,
    country_code: str,
    sex_ambiguous_min: int,
    sex_ambiguous_max: int,
    bcftools_image: str,
    pgx_image: str,
    pgx_repo: Path | None,
    snakemake_jobs: int,
    workers: int,
    prepare: bool,
    run: bool,
    yes: bool,
) -> None:
    """Prepare pgx_pilot workspaces and run the AF pipeline for each WGS sample.

    Reads lifted VCFs from <base-dir>/liftover/, infers sample sex from chrY
    variant counts (ambiguous cases are asked interactively), creates one
    workspace per sample under <base-dir>/pgx_runs/, then runs the pgx_pilot
    Snakemake pipeline via Docker.

    Use --prepare to stop after workspace creation, or --run to skip
    preparation and go straight to execution.
    """
    configure_module_logging(ctx, "beacon_pgx")
    if prepare and run:
        raise click.UsageError("--prepare and --run are mutually exclusive.")

    base_dir = base_dir.resolve()

    config = beacon_pgx.PgxConfig(
        base_dir=base_dir,
        country_code=country_code,
        sex_ambiguous_min=sex_ambiguous_min,
        sex_ambiguous_max=sex_ambiguous_max,
        bcftools_image=bcftools_image,
        pgx_image=pgx_image,
        pgx_repo=pgx_repo,
        snakemake_jobs=snakemake_jobs,
        workers=workers,
    )

    try:
        beacon_pgx.validate_pgx_layout(config)
        if not prepare:
            beacon_pgx.install_snakefile(config)
            beacon_pgx.validate_pgx_run_prereqs(config)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    # ----------------------------------------------------------------
    # Discover lifted VCFs; compare against existing samples.tsv
    # ----------------------------------------------------------------
    lifted_vcfs = beacon_pgx.discover_lifted_vcfs(base_dir)
    if not lifted_vcfs:
        raise click.ClickException(
            f"No *.GRCh38.clean.vcf.gz files found in {base_dir / 'liftover'}. "
            "Run `impact-tools beacon liftover` first."
        )

    existing = (
        beacon_pgx.read_samples_tsv(config.samples_tsv)
        if config.samples_tsv.exists()
        else []
    )
    existing_basenames = {r.vcf_basename for r in existing if r.vcf_basename}

    new_vcfs = [
        vcf for vcf in lifted_vcfs
        if beacon_pgx.vcf_to_sample_id(vcf) not in existing_basenames
    ]

    log.info("==========================================")
    log.info("Beacon pgx")
    log.info("==========================================")
    log.info("Base directory:      %s", base_dir)
    log.info("Lifted VCFs found:   %d", len(lifted_vcfs))
    log.info("Already in samples.tsv: %d  |  new: %d", len(existing), len(new_vcfs))
    log.info("==========================================")

    # ----------------------------------------------------------------
    # Infer sex for new samples in parallel; prompt for ambiguous cases
    # sequentially; write TSV once after all records are resolved
    # ----------------------------------------------------------------
    new_records: list[beacon_pgx.SampleRecord] = []
    if new_vcfs:
        log.info(
            "Counting non-ref chrY variants for %d new sample(s) (workers=%d)...",
            len(new_vcfs), workers,
        )
        inferences = beacon_pgx.infer_sex_batch(config, [v.name for v in new_vcfs])

        for vcf, inference in zip(new_vcfs, inferences):
            if inference is None:
                log.warning("[%s] chrY count failed — skipping", vcf.name)
                continue

            sex = inference.sex
            if sex is None:
                log.warning(
                    "[%s] %s: %d chrY variants — ambiguous zone (%d–%d), manual input required",
                    vcf.name, inference.sample_id, inference.n_chry,
                    sex_ambiguous_min, sex_ambiguous_max,
                )
                raw = click.prompt(
                    f"  Sex for {inference.sample_id} (M/F)",
                    type=click.Choice(["M", "F"], case_sensitive=False),
                ).upper()
                sex = "M" if raw == "M" else "F"
            else:
                log.info(
                    "[%s] %s: %d chrY -> %s",
                    vcf.name, inference.sample_id, inference.n_chry, sex,
                )

            new_records.append(beacon_pgx.SampleRecord(
                sample_id=inference.sample_id,
                sex=sex,
                country_code=country_code,
                vcf_basename=beacon_pgx.vcf_to_sample_id(vcf),
            ))

        for record in new_records:
            beacon_pgx.append_sample_to_tsv(config.samples_tsv, record)
            log.info("[%s] Added to samples.tsv", record.sample_id)

    all_records = existing + new_records

    if not all_records:
        raise click.ClickException("No samples to process.")

    log.info("==========================================")
    log.info("Samples to process (%d):", len(all_records))
    for r in all_records:
        log.info("  %s  sex=%s  country=%s", r.sample_id, r.sex, r.country_code)
    log.info("==========================================")

    if not yes:
        proceed = click.confirm("Continue?", default=False)
        if not proceed:
            log.info("Cancelled.")
            return

    (base_dir / "logs").mkdir(parents=True, exist_ok=True)
    config.pgx_runs_dir.mkdir(parents=True, exist_ok=True)

    # ----------------------------------------------------------------
    # Prepare workspaces (parallel)
    # ----------------------------------------------------------------
    prepare_results: list[beacon_pgx.WorkspaceResult] = []
    if not run:
        log.info("==========================================")
        log.info("Preparing workspaces  (workers=%d)", workers)
        log.info("==========================================")
        prepare_results = beacon_pgx.prepare_workspaces(config, all_records)

    # ----------------------------------------------------------------
    # Run pgx_pilot (parallel)
    # ----------------------------------------------------------------
    run_results: list[beacon_pgx.PgxRunResult] = []
    if not prepare:
        log.info("==========================================")
        log.info("Running pgx_pilot  (workers=%d)", workers)
        log.info("==========================================")
        run_results = beacon_pgx.run_pgx_pilots(config, all_records)

    pipeline_result = beacon_pgx.PgxPipelineResult(
        prepare_results=prepare_results,
        run_results=run_results,
    )

    total = len(prepare_results) + len(run_results)
    log.info("==========================================")
    log.info("pgx summary")
    log.info("==========================================")
    log.info(
        "  Steps OK:       %d",
        total - pipeline_result.failed - pipeline_result.warned,
    )
    log.info("  Steps warnings: %d", pipeline_result.warned)
    log.info("  Steps failed:   %d", pipeline_result.failed)

    if pipeline_result.failed > 0:
        raise click.ClickException(
            f"pgx pipeline finished with {pipeline_result.failed} failed step(s). "
            "Check logs in <base-dir>/logs/ for details."
        )


@beacon.group("ingest")
def beacon_ingest_group() -> None:
    """Beacon ingestion workflows."""

@beacon_ingest_group.command("dataset")
@click.option("--dataset-id", required=False, help="Beacon dataset identifier.")
@click.option("--name", required=False, help="Dataset display name.")
@click.option(
    "--description",
    default=None,
    show_default=True,
    help="Dataset description. If omitted, you will be prompted.",
)
@click.option(
    "--ref-genome",
    "reference_genome",
    type=click.Choice(["GRCh37", "GRCh38"]),
    default=None,
    show_default=True,
    help="Reference genome used by the dataset. If omitted, you will be prompted.",
)
@click.option(
    "--test/--no-test",
    default=None,
    show_default=True,
    help="Whether this dataset is a test dataset.",
)
@click.option(
    "--synthetic/--no-synthetic",
    default=None,
    show_default=True,
    help="Whether this dataset is synthetic.",
)
@click.option(
    "-b",
    "--base-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default="current working directory",
    help="Base Beacon operational directory. Defaults to the current directory.",
)
@click.option(
    "--granularity",
    type=click.Choice(["boolean", "count", "record"]),
    default="record",
    show_default=True,
    help="Default public entry type granularity.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Discover artifacts without writing files.",
)

@click.pass_context
def ingest_dataset_cmd(
    ctx: click.Context,
    dataset_id: str | None,
    name: str | None,
    description: str | None,
    reference_genome: str | None,
    test: bool | None,
    synthetic: bool | None,
    base_dir: Path,
    granularity: str,
    dry_run: bool,
) -> None:
    """Prepare Beacon dataset registration artifacts."""
    configure_module_logging(ctx, "beacon_ingest_dataset")

    if dataset_id is None: 
        dataset_id = click.prompt("Dataset ID")

    if name is None: 
        name = click.prompt("Dataset name")

    if description is None:
        add_description = click.confirm(
            "Do you want to add any description?",
            default=False,
        )
        if add_description:
            description = click.prompt("Please write a description")
        else:
            description = ""

    if reference_genome is None:
        reference_genome = click.prompt(
            "Please, select your dataset genome build", 
            type=click.Choice(["GRCh37", "GRCh38"]),
            default="GRCh38",
            show_choices=True,
            show_default=True,
        )

    if test is None:
        test = click.confirm("Is this a <test> dataset?", default=False)

    if synthetic is None:
        synthetic = click.confirm("Is this a <synthetic> dataset?", default=False)

    base_dir = base_dir.resolve()

    cfg = beacon_ingest.DatasetIngestConfig(
        dataset_id=dataset_id,
        name=name,
        description=description,
        reference_genome=reference_genome,
        is_test=test,
        is_synthetic=synthetic,
        base_dir=base_dir,
        granularity=granularity,
        dry_run=dry_run,
    )

    try:
        result = beacon_ingest.ingest_dataset(cfg)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    log.info("==========================================")
    log.info("Beacon dataset ingest")
    log.info("==========================================")
    log.info("Dataset ID: %s", result.dataset_id)
    log.info("Base dir:   %s", result.paths.base_dir)
    log.info("Mode:       %s", "dry-run" if dry_run else "write")
    log.info("Generated artifacts:")
    for path in result.generated_files:
        log.info("  - %s", path)

    if result.metrics_file is not None:
        log.info("Metrics file:")
        log.info("  - %s", result.metrics_file)


@beacon_ingest_group.command("variants")
@click.option(
    "--dataset-id",
    required=True,
    help="Beacon dataset identifier (must already exist in MongoDB).",
)
@click.option(
    "--vcf",
    required=True,
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Aggregated VCF (.vcf.gz) to ingest into the dataset.",
)
@click.option(
    "--ref-genome",
    "reference_genome",
    type=click.Choice(["GRCh37", "GRCh38"]),
    default="GRCh38",
    show_default=True,
    help="Reference genome used by the VCF.",
)
@click.option(
    "-b",
    "--base-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=Path("."),
    show_default="current working directory",
    help="Base Beacon operational directory for staging artifacts and logs.",
)
@click.option(
    "--cleanup-old",
    is_flag=True,
    help=(
        "After a successful swap, immediately delete the _old_<run_id> "
        "variants from MongoDB. Off by default for safer rollback."
    ),
)
@click.option(
    "--skip-filtering-terms",
    is_flag=True,
    help=(
        "Skip filtering terms extraction (slow). Useful for batched "
        "ingestions where you run it once at the end."
    ),
)
@click.option(
    "--dry-run",
    is_flag=True,
    help=(
        "Run ri-tools against the staging dataset and STOP before the swap. "
        "Useful for validating that the VCF ingests cleanly."
    ),
)
@click.pass_context
def ingest_variants_cmd(
    ctx: click.Context,
    dataset_id: str,
    vcf: Path,
    reference_genome: str,
    base_dir: Path,
    cleanup_old: bool,
    skip_filtering_terms: bool,
    dry_run: bool,
) -> None:
    """Ingest variants into an existing Beacon dataset (stage→swap→cleanup)."""
    configure_module_logging(ctx, "beacon_ingest_variants")

    base_dir = base_dir.resolve()
    vcf = vcf.resolve()

    cfg = beacon_ingest.VariantsIngestConfig(
        dataset_id=dataset_id,
        vcf=vcf,
        reference_genome=reference_genome,
        base_dir=base_dir,
        cleanup_old=cleanup_old,
        skip_filtering_terms=skip_filtering_terms,
        dry_run=dry_run,
    )

    try:
        result = beacon_ingest.apply_variants_to_remote(cfg)
    except Exception as exc:  # noqa: BLE001
        raise click.ClickException(str(exc)) from exc

    log.info("==========================================")
    log.info("Beacon variant ingest")
    log.info("==========================================")
    log.info("Dataset ID:           %s", result.dataset_id)
    log.info("Staging ID (Mongo):   %s", result.staging_id)
    log.info("Old ID (Mongo):       %s", result.old_id)
    log.info("Mode:                 %s", "dry-run" if dry_run else "swap")
    log.info("VCF variants count:   %s", result.vcf_count)
    if result.mongo_count is not None:
        log.info("Mongo variants count: %s", result.mongo_count)
    if result.api_visible is not None:
        log.info("API visible:          %s", result.api_visible)
    if result.api_count_valid is not None:
        log.info("API count valid:      %s", result.api_count_valid)
    if result.deleted_old_variants is not None:
        log.info("Old variants purged:  %s", result.deleted_old_variants)

    if not cleanup_old and not dry_run:
        try:
            deleted_backups = beacon_ingest.offer_old_variant_backups_cleanup(
                dataset_id=dataset_id,
                older_than_days=7,
            )
        except click.Abort:
            raise
        except Exception as exc:  # noqa: BLE001
            raise click.ClickException(str(exc)) from exc

        if deleted_backups:
            log.info("Old variant backups cleanup summary:")
            for backup_id, deleted_count in deleted_backups.items():
                log.info("  %s: %d variants deleted", backup_id, deleted_count)


@ega.command("encrypt-slurm")
@click.option(
    "--config-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Additional JSON/YAML configuration for this SLURM plan.",
)
@click.option(
    "-i",
    "--input-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help=(
        "Directory containing raw files. Also used as the base directory for "
        "relative paths in --input-list."
    ),
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory where encrypted .c4gh files will be written by SLURM jobs.",
)
@click.option(
    "--plan-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory where SLURM chunks, manifests and sbatch file are written.",
)
@click.option(
    "-k",
    "--recipient-pubkey",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Crypt4GH recipient public key, usually LocalEGA service.key.pub.",
)
@click.option(
    "--crypt4gh-bin",
    type=click.Path(path_type=Path, dir_okay=False),
    help="crypt4gh executable to use inside each SLURM task.",
)
@click.option(
    "--input-list",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help=(
        "Optional text file with one input file path per line. Relative paths "
        "are resolved from input-dir."
    ),
)
@click.option(
    "--pattern",
    help="Input file glob pattern. Defaults to configuration value.",
)
@click.option(
    "--task-layout",
    type=click.Choice(["sample", "file"]),
    help="Create SLURM tasks by sample directory or by individual file.",
)
@click.option(
    "--items-per-task",
    type=int,
    help="Number of samples or files grouped into each SLURM array task.",
)
@click.option("--job-name", help="SLURM job name.")
@click.option(
    "--partition",
    help="SLURM partition to include in the sbatch file.",
)
@click.option("--account", help="SLURM account to include in the sbatch file.")
@click.option(
    "--chdir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Working directory for the SLURM job. Written as #SBATCH --chdir.",
)
@click.option("--ntasks", type=int, help="SLURM ntasks value.")
@click.option("--cpus-per-task", type=int, help="SLURM CPUs per task.")
@click.option("--mem", help="SLURM memory request.")
@click.option(
    "--time-limit",
    help="SLURM time limit.",
)
@click.option(
    "--setup-command",
    multiple=True,
    help=(
        "Command inserted before encryption in the sbatch file. Can be used "
        "multiple times to load modules or activate environments."
    ),
)
@click.option(
    "--no-checksums",
    is_flag=True,
    help="Pass --no-checksums to each encryption task.",
)
@click.option(
    "--with-plots",
    is_flag=True,
    help="Allow each encryption task to generate plots.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Pass --force to each encryption task.",
)
@click.option(
    "--continue-on-error",
    is_flag=True,
    help="Do not pass --fail-fast to each encryption task.",
)
@click.option(
    "--registry-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Shared SQLite processing registry passed to each array task.",
)
@click.option(
    "--no-registry",
    is_flag=True,
    help="Disable persistent content tracking in generated array tasks.",
)
@click.option(
    "--submit",
    is_flag=True,
    help="Submit the generated sbatch file with sbatch.",
)
@click.pass_context
def encrypt_slurm_cmd(
    ctx: click.Context,
    config_file: Path | None,
    input_dir: Path | None,
    output_dir: Path | None,
    plan_dir: Path | None,
    recipient_pubkey: Path | None,
    crypt4gh_bin: Path | None,
    input_list: Path | None,
    pattern: str,
    task_layout: str,
    items_per_task: int,
    job_name: str,
    partition: str | None,
    account: str | None,
    chdir: Path | None,
    ntasks: int,
    cpus_per_task: int,
    mem: str,
    time_limit: str,
    setup_command: tuple[str, ...],
    no_checksums: bool,
    with_plots: bool,
    force: bool,
    continue_on_error: bool,
    registry_file: Path | None,
    no_registry: bool,
    submit: bool,
) -> None:
    """Generate and optionally submit SLURM array jobs for Crypt4GH encryption."""
    configure_module_logging(ctx, "ega_encrypt_slurm")
    configuration = (
        load_configuration(config_file)
        if config_file is not None
        else ctx.obj["configuration"]
    )
    slurm_conf = configuration.get("ega", {}).get("slurm_encryption", {})
    input_dir = input_dir or _configured_path(
        configuration, "ega.slurm_encryption.input_dir"
    ) or _configured_path(configuration, "ega.encryption.input_dir")
    output_dir = output_dir or _configured_path(
        configuration, "ega.slurm_encryption.output_dir"
    ) or _configured_path(configuration, "ega.encryption.output_dir")
    recipient_pubkey = recipient_pubkey or _configured_path(
        configuration, "ega.slurm_encryption.recipient_pubkey"
    ) or _configured_path(configuration, "ega.encryption.recipient_pubkey")
    crypt4gh_bin = crypt4gh_bin or _configured_path(
        configuration, "ega.slurm_encryption.crypt4gh_bin"
    ) or _configured_path(configuration, "ega.encryption.crypt4gh_bin")
    registry_file = registry_file or _configured_path(
        configuration, "ega.registry_file"
    )
    if no_registry:
        registry_file = None
    if input_dir is None:
        raise click.UsageError(
            "--input-dir is required or must be set in the EGA encryption "
            "configuration."
        )
    if recipient_pubkey is None:
        raise click.UsageError(
            "--recipient-pubkey is required or must be set in the EGA "
            "encryption configuration."
        )
    configured_chdir = slurm_config_value(slurm_conf, "chdir", chdir)
    if isinstance(configured_chdir, str):
        configured_chdir = Path(configured_chdir)
    configured_setup_commands = (
        setup_command
        if setup_command
        else tuple(slurm_conf.get("setup_commands") or ())
    )
    configured_fail_fast = bool(slurm_conf.get("fail_fast", True))
    if continue_on_error:
        configured_fail_fast = False

    config = SlurmEncryptionPlanConfig(
        input_dir=input_dir,
        output_dir=output_dir,
        plan_dir=plan_dir,
        recipient_pubkey=recipient_pubkey,
        crypt4gh_bin=crypt4gh_bin,
        input_list=input_list,
        pattern=slurm_config_value(slurm_conf, "pattern", pattern, "*.fastq.gz"),
        task_layout=slurm_config_value(slurm_conf, "task_layout", task_layout, "sample"),
        items_per_task=int(
            slurm_config_value(slurm_conf, "items_per_task", items_per_task, 1)
        ),
        job_name=slurm_config_value(slurm_conf, "job_name", job_name, "localega_encrypt"),
        partition=slurm_config_value(slurm_conf, "partition", partition, "middle_idx"),
        account=slurm_config_value(slurm_conf, "account", account),
        chdir=configured_chdir,
        ntasks=int(slurm_config_value(slurm_conf, "ntasks", ntasks, 1)),
        cpus_per_task=int(slurm_config_value(slurm_conf, "cpus_per_task", cpus_per_task, 2)),
        mem=slurm_config_value(slurm_conf, "mem", mem, "8G"),
        time_limit=slurm_config_value(slurm_conf, "time_limit", time_limit, "24:00:00"),
        setup_commands=configured_setup_commands,
        no_checksums=no_checksums or bool(slurm_conf.get("no_checksums", False)),
        no_plots=not (with_plots or bool(slurm_conf.get("generate_plots", False))),
        force=force or bool(slurm_conf.get("force", False)),
        fail_fast=configured_fail_fast,
        registry_file=registry_file,
    )
    try:
        result = write_slurm_encryption_plan(config)
        if submit:
            result = submit_slurm_job(result)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts to clean error
        raise click.ClickException(str(exc)) from exc

    click.echo(f"Plan directory : {result.plan_dir}")
    click.echo(f"Tasks          : {result.task_count}")
    click.echo(f"Files          : {result.file_count}")
    click.echo(f"Total GiB      : {result.total_size_bytes / (1024**3):.3f}")
    click.echo(f"Task plan      : {result.task_plan_file}")
    click.echo(f"File plan      : {result.file_plan_file}")
    click.echo(f"Chunk index    : {result.chunk_index_file}")
    click.echo(f"SBATCH file    : {result.sbatch_file}")
    click.echo(f"Run helper     : {result.run_file}")
    if submit:
        if result.submitted:
            click.echo(f"Submitted      : {result.submit_stdout}")
        else:
            raise click.ClickException(
                f"sbatch submission failed: {result.submit_stderr or result.submit_stdout}"
            )
    else:
        click.echo(f"Submit with    : bash {result.run_file}")


ega.add_command(encrypt_slurm_cmd, "plan-encryption-slurm")


def main() -> None:
    """Console entry point."""
    cli()


if __name__ == "__main__":
    main()

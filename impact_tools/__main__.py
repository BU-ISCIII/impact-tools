"""Command line interface for impact-tools."""

from __future__ import annotations

import logging
import time
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
from impact_tools.beacon.config import build_beacon_deployment_config

from impact_tools.config import (
    EXTRA_CONFIG_PATH,
    get_config_value,
    include_extra_config,
    install_submission_profile,
    load_configuration,
    remove_extra_config,
)
from impact_tools.ega.benchmark import compare_runs
from impact_tools.ega.encrypt import EncryptionConfig, run_encryption
from impact_tools.ega.registry import DEFAULT_REGISTRY_PATH, EgaRegistry
from impact_tools.ega.slurm_report import aggregate_slurm_encryption
from impact_tools.ega.slurm import (
    SlurmEncryptionPlanConfig,
    submit_slurm_job,
    write_slurm_encryption_plan,
)
from impact_tools.ega.submission import (
    PrepareSubmissionConfig,
    SubmitSubmissionConfig,
    prepare_submission,
    submit_submission,
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
    "--ega-submission-profile",
    help="Install a bundled editable EGA submission profile under ~/.impact_tools.",
)
@click.option(
    "--submission-profile-name",
    help="Install --config-file as this editable EGA submission profile name.",
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
    ega_submission_profile: str | None,
    submission_profile_name: str | None,
    remove_config: bool,
) -> None:
    """Create/update extra config or install editable EGA submission profiles."""
    try:
        if remove_config:
            if config_file is not None or ega_submission_profile or submission_profile_name:
                raise click.UsageError(
                    "--remove-config cannot be combined with config/profile install options."
                )
            removed = remove_extra_config(config_name, force=force)
            if not removed:
                raise click.ClickException(
                    f"Configuration not found: {config_name or EXTRA_CONFIG_PATH}"
                )
            click.echo(f"Removed configuration from {EXTRA_CONFIG_PATH}")
            return

        if ega_submission_profile:
            if config_file is not None or submission_profile_name:
                raise click.UsageError(
                    "--ega-submission-profile cannot be combined with --config-file "
                    "or --submission-profile-name."
                )
            path = install_submission_profile(ega_submission_profile, force=force)
            click.echo(f"EGA submission profile written to {path}")
            return

        if submission_profile_name:
            if config_file is None:
                raise click.UsageError("--config-file is required with --submission-profile-name.")
            path = install_submission_profile(
                submission_profile_name,
                source_file=config_file,
                force=force,
            )
            click.echo(f"EGA submission profile written to {path}")
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


@ega.command("prepare-submission")
@click.option(
    "--provider",
    default="cnio",
    show_default=True,
    help="Provider extraction profile to use.",
)
@click.option(
    "-i",
    "--input-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    required=True,
    help="Directory containing provider delivery files for one sample.",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="Directory where draft submission and evidence files are written.",
)
@click.option(
    "--sample-id",
    help="Sample identifier. Defaults to the input directory name.",
)
@click.option(
    "--metadata-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Optional provider/cohort metadata CSV, TSV or JSON file.",
)
@click.option(
    "--profile-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Optional submission profile YAML/JSON file with official default values.",
)
@click.option(
    "--include-examples",
    is_flag=True,
    help="Fill missing fields with EXAMPLE testing values from the submission profile.",
)
@click.pass_context
def prepare_submission_cmd(
    ctx: click.Context,
    provider: str,
    input_dir: Path,
    output_dir: Path,
    sample_id: str | None,
    metadata_file: Path | None,
    profile_file: Path | None,
    include_examples: bool,
) -> None:
    """Prepare an EGA submitter-portal draft from local provider files."""
    configure_module_logging(ctx, "ega_prepare_submission")
    try:
        result = prepare_submission(
            PrepareSubmissionConfig(
                provider=provider,
                input_dir=input_dir,
                output_dir=output_dir,
                sample_id=sample_id,
                metadata_file=metadata_file,
                profile_file=profile_file,
                include_examples=include_examples,
            )
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Evidence report: {result.evidence_file}")
    click.echo(f"Evidence JSON: {result.evidence_json}")
    click.echo(f"Draft submission: {result.draft_file}")
    click.echo(f"Missing fields: {result.missing_file}")
    click.echo(f"File inventory: {result.inventory_file}")


@ega.command("submit-submission")
@click.option(
    "--draft-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
    help="Prepared draft_submission.yaml/json file to submit.",
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    required=True,
    help="Directory where payloads, responses and state are written.",
)
@click.option(
    "--api-base",
    help="Submitter Portal API base URL. Required with --execute.",
)
@click.option(
    "--token",
    help="Bearer access token. Prefer --token-file to avoid shell history.",
)
@click.option(
    "--token-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="File containing a raw bearer token or JSON with access_token.",
)
@click.option(
    "--resume-state-file",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    help="Previous submission_state.json to continue without recreating completed entities.",
)
@click.option(
    "--submission-id",
    help="Existing submission provisional_id to continue from instead of creating a new submission.",
)
@click.option(
    "--execute/--dry-run",
    default=False,
    show_default=True,
    help="Execute API calls. Default dry-run only writes payloads and plan.",
)
@click.option(
    "--finalise/--no-finalise",
    default=False,
    show_default=True,
    help="Include finalise step. Keep disabled until metadata has been reviewed.",
)
@click.option(
    "--timeout-seconds",
    default=60.0,
    show_default=True,
    type=float,
    help="HTTP timeout in seconds for --execute.",
)
@click.option(
    "--no-verify-tls",
    is_flag=True,
    help="Disable TLS certificate verification for --execute.",
)
@click.pass_context
def submit_submission_cmd(
    ctx: click.Context,
    draft_file: Path,
    output_dir: Path,
    api_base: str | None,
    token: str | None,
    token_file: Path | None,
    resume_state_file: Path | None,
    submission_id: str | None,
    execute: bool,
    finalise: bool,
    timeout_seconds: float,
    no_verify_tls: bool,
) -> None:
    """Prepare payloads and optionally submit EGA metadata to the API."""
    configure_module_logging(ctx, "ega_submit_submission")
    try:
        result = submit_submission(
            SubmitSubmissionConfig(
                draft_file=draft_file,
                output_dir=output_dir,
                api_base=api_base,
                token=token,
                token_file=token_file,
                resume_state_file=resume_state_file,
                submission_id=submission_id,
                execute=execute,
                finalise=finalise,
                timeout_seconds=timeout_seconds,
                verify_tls=not no_verify_tls,
            )
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Submission plan: {result.plan_file}")
    click.echo(f"Payloads: {result.payload_dir}")
    click.echo(f"Responses: {result.response_dir}")
    click.echo(f"State: {result.state_file}")


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


@ega.command("aggregate-encryption-array")
@click.option(
    "--manifest-dir",
    required=True,
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    help="Directory containing per-task encryption manifests.",
)
@click.option(
    "--output-dir",
    required=True,
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory for the aggregate manifest, metrics and HTML report.",
)
@click.option(
    "--array-job-id",
    "array_job_ids",
    multiple=True,
    required=True,
    help="SLURM array job identifier. Repeat when one logical run used several submissions.",
)
@click.option(
    "--expected-tasks",
    required=True,
    type=click.IntRange(min=1),
    help="Number of tasks expected in the array.",
)
@click.option("--no-charts", is_flag=True, help="Omit charts from the aggregate HTML report.")
@click.pass_context
def aggregate_encryption_array_cmd(
    ctx: click.Context,
    manifest_dir: Path,
    output_dir: Path,
    array_job_ids: tuple[str, ...],
    expected_tasks: int,
    no_charts: bool,
) -> None:
    """Aggregate one SLURM encryption array into a single logical run report."""
    configure_module_logging(ctx, "ega_encrypt_slurm")
    try:
        result = aggregate_slurm_encryption(
            manifest_dir=manifest_dir,
            output_dir=output_dir,
            array_job_ids=array_job_ids,
            expected_tasks=expected_tasks,
            include_charts=not no_charts,
        )
    except Exception as exc:  # noqa: BLE001 - CLI boundary
        raise click.ClickException(str(exc)) from exc
    click.echo(f"Array task manifests: {result.task_manifests}/{result.expected_tasks}")
    click.echo(f"Files: {result.files}")
    click.echo(f"Failed or missing: {result.failed}")
    click.echo(f"Manifest: {result.manifest_file}")
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
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Directory for metrics and logs (default: <base-dir>/logs/).",
)
@click.option(
    "--run-profile",
    type=click.Choice(["local", "ws", "hpc"]),
    default=None,
    help="Execution environment label recorded in metrics.",
)
@click.option(
    "--check",
    "check_only",
    is_flag=True,
    help="Only check inputs and detected genome builds; do not run liftover.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Skip interactive confirmation prompts.",
)
@click.option(
    "--no-report",
    is_flag=True,
    help="Skip HTML report generation. Metrics JSON is always written.",
)
@click.pass_context
def liftover_cmd(
    ctx: click.Context,
    base_dir: Path,
    chain: Path | None,
    fasta: Path | None,
    bcftools_image: str,
    crossmap_image: str,
    cleanup: bool,
    workers: int,
    output_dir: Path | None,
    run_profile: str | None,
    check_only: bool,
    force: bool,
    no_report: bool,
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
        if force or click.confirm("Delete these intermediate files?", default=False):
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

    if not force:
        if not click.confirm("Continue with liftover execution?", default=False):
            log.info("Cancelled.")
            return

    configuration = ctx.obj["configuration"]
    effective_run_profile = run_profile or get_config_value(
        configuration, "beacon.execution.profile", "local"
    )

    config = beacon_liftover.LiftoverConfig(
        base_dir=base_dir,
        chain=chain,
        fasta=fasta,
        bcftools_image=bcftools_image,
        crossmap_image=crossmap_image,
        workers=workers,
        output_dir=output_dir.resolve() if output_dir is not None else None,
        run_profile=effective_run_profile,
    )

    started_at = datetime.now().astimezone().isoformat()
    started_perf = time.perf_counter()
    status = "success"
    error_message: str | None = None
    result = None

    try:
        result = beacon_liftover.run_liftover(config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts to clean error
        status = "failed"
        error_message = str(exc)
        raise click.ClickException(str(exc)) from exc
    finally:
        ended_at = datetime.now().astimezone().isoformat()
        duration_seconds = time.perf_counter() - started_perf
        try:
            metrics_file = beacon_liftover.write_liftover_metrics(
                config=config,
                result=result,
                started_at=started_at,
                ended_at=ended_at,
                duration_seconds=duration_seconds,
                status=status,
                error=error_message,
            )
            log.info("Liftover metrics written: %s", metrics_file)
            if not no_report:
                beacon_liftover.write_liftover_html_report(metrics_file=metrics_file)
                log.info(
                    "Liftover HTML report written: %s",
                    metrics_file.with_name(
                        metrics_file.name.removesuffix(".metrics.json") + ".report.html"
                    ),
                )
        except Exception as metrics_exc:  # noqa: BLE001
            log.warning("Could not write liftover metrics: %s", metrics_exc)

    if result is None:
        return

    log.info("==========================================")
    log.info("Liftover summary")
    log.info("==========================================")
    log.info("  Samples OK:       %d", len(result.results) - result.failed - result.warned)
    log.info("  Samples warnings: %d", result.warned)
    log.info("  Samples failed:   %d", result.failed)

    if result.failed > 0:
        raise click.ClickException(
            f"Liftover finished with {result.failed} failed sample(s). "
            f"Check logs in {config.logs_dir} for details."
        )

    intermediates = beacon_liftover.list_intermediates(
        base_dir / "liftover", sample_ids
    )
    if intermediates:
        log.info("Intermediate files generated (%d):", len(intermediates))
        for p in intermediates:
            log.info("  %s", p.name)
        if cleanup or force or click.confirm("Remove intermediate files?", default=True):
            beacon_liftover.cleanup_intermediates(
                base_dir / "liftover", sample_ids
            )

@beacon.command("pgx")
@click.option(
    "--release-id", "release_id",
    default=None,
    help=(
        "Release identifier (alphanumeric, _ and -, max 63 chars). "
        "Auto-derived from --samples-tsv filename if omitted."
    ),
)
@click.option(
    "--samples-tsv",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    required=True,
    help="Tab-separated sample metadata: sample_id, sex, country_code[, batch_id[, ancestry_group]].",
)
@click.option(
    "--gvcf-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Directory to search recursively for *.hard-filtered.gvcf.gz files (DRAGEN output).",
)
@click.option(
    "--gvcf-list",
    type=click.Path(path_type=Path, exists=True, dir_okay=False),
    default=None,
    help="File listing sample_id<TAB>gvcf_path entries (one per line).",
)
@click.option(
    "--executor",
    type=click.Choice(["local", "hpc"]),
    default=None,
    help=(
        "Execution backend. 'local' runs with Docker; "
        "'hpc' generates an sbatch script for SLURM using Singularity. "
        "Defaults to beacon.pgx.executor in config, or 'local'."
    ),
)
@click.option(
    "--prepare",
    is_flag=True,
    help="Prepare the workspace and generate the launcher script; do not execute or submit.",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Validate inputs and print the plan without writing any files.",
)
@click.option(
    "--force",
    is_flag=True,
    help="Remove existing workspace and start fresh.",
)
@click.option(
    "--pypgx",
    is_flag=True,
    help=(
        "Enable the optional PyPGx sub-workflow. "
        "Disabled by default until its offline resources are configured."
    ),
)
@click.option(
    "--output-dir", "-o",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Root output directory (pgx_runs/ is created inside). Overrides beacon.pgx.output_dir in config.",
)
@click.option(
    "--ref-fasta", "ref_fasta",
    type=click.Path(path_type=Path, dir_okay=False),
    default=None,
    help=(
        "Reference genome FASTA. Required, but may instead be set via "
        "beacon.pgx.ref_fasta in config."
    ),
)
@click.option(
    "--pgx-image", "pgx_image",
    default=None,
    help=(
        "pgx_pilot container image: a .sif path for --executor hpc, or a Docker "
        "image reference for --executor local. Required, but may instead be set "
        "via beacon.pgx.pgx_image in config."
    ),
)
@click.option(
    "--no-report",
    is_flag=True,
    help="Skip HTML report generation. Metrics JSON is always written.",
)
@click.option(
    "--cleanup",
    is_flag=True,
    help=(
        "Remove the results/temp scratch intermediates after a successful run. "
        "Prompts for confirmation unless --force is given."
    ),
)
@click.pass_context
def pgx_cmd(
    ctx: click.Context,
    release_id: str | None,
    samples_tsv: Path,
    gvcf_dir: Path | None,
    gvcf_list: Path | None,
    executor: str | None,
    output_dir: Path | None,
    ref_fasta: Path | None,
    pgx_image: str | None,
    prepare: bool,
    dry_run: bool,
    force: bool,
    pypgx: bool,
    no_report: bool,
    cleanup: bool,
) -> None:
    """Run the pgx_pilot AF/QC pipeline for a DRAGEN gVCF batch.

    ref_fasta, output_dir and pgx_image are required: pass them via
    --ref-fasta / --output-dir / --pgx-image, or set them under the beacon.pgx
    namespace in ~/.impact_tools/extra_config.json. Remaining infrastructure
    (glnexus_image, slurm settings, ...) is read from config. See the README
    for the full config schema.
    """
    configure_module_logging(ctx, "beacon_pgx")

    # Exactly one gVCF input source is required (Click cannot express XOR).
    if gvcf_dir is None and gvcf_list is None:
        raise click.UsageError("One of --gvcf-dir or --gvcf-list is required.")
    if gvcf_dir is not None and gvcf_list is not None:
        raise click.UsageError("--gvcf-dir and --gvcf-list are mutually exclusive.")

    # --cleanup bakes a `rm -rf results/temp` into the generated script. Confirm
    # interactively (default No) unless --force is given. Skip in dry-run, which
    # writes no script.
    cleanup_temp = False
    if cleanup and not dry_run:
        cleanup_temp = force or click.confirm(
            "Remove intermediate files (results/temp) after a successful run?",
            default=False,
        )

    resolved_samples_tsv = samples_tsv.resolve()
    if not release_id:
        release_id = beacon_pgx._derive_batch_id(resolved_samples_tsv)
        log.info("release_id auto-derived: %s", release_id)

    configuration = ctx.obj["configuration"]

    # ── Read infrastructure from persistent config ────────────────────────────
    pgx_conf = get_config_value(configuration, "beacon.pgx", {}) or {}

    def _pgx_path(key: str) -> Path | None:
        v = pgx_conf.get(key) or get_config_value(configuration, f"beacon.pgx.{key}", None)
        return Path(v).expanduser().resolve() if v else None

    ref_fasta = (
        ref_fasta.expanduser().resolve() if ref_fasta is not None else _pgx_path("ref_fasta")
    )
    output_dir = (
        output_dir.expanduser().resolve() if output_dir is not None else _pgx_path("output_dir")
    )
    pgx_image_raw = (
        pgx_image
        or pgx_conf.get("pgx_image")
        or get_config_value(configuration, "beacon.pgx.pgx_image", None)
    )
    glnexus_image_raw = pgx_conf.get("glnexus_image") or get_config_value(
        configuration, "beacon.pgx.glnexus_image", None
    )
    resolved_executor: str = (
        executor
        or pgx_conf.get("executor")
        or get_config_value(configuration, "beacon.pgx.executor", "local")
    )
    pgx_image: Path | None = (
        Path(pgx_image_raw).expanduser().resolve()
        if pgx_image_raw is not None and resolved_executor == "hpc"
        else Path(pgx_image_raw)
        if pgx_image_raw is not None
        else None
    )
    glnexus_image: Path | None = (
        Path(glnexus_image_raw).expanduser().resolve()
        if glnexus_image_raw is not None and resolved_executor == "hpc"
        else Path(glnexus_image_raw)
        if glnexus_image_raw is not None
        else None
    )
    glnexus_config: str = (
        pgx_conf.get("glnexus_config")
        or get_config_value(configuration, "beacon.pgx.glnexus_config", "gatk")
    )
    snakemake_jobs: int = int(
        pgx_conf.get("snakemake_jobs")
        or get_config_value(configuration, "beacon.pgx.snakemake_jobs", 8)
    )
    pypgx_snakefile_raw = pgx_conf.get("pypgx_snakefile") or get_config_value(
        configuration, "beacon.pgx.pypgx_snakefile", None
    )
    pypgx_snakefile: Path | None = (
        Path(pypgx_snakefile_raw).expanduser().resolve() if pypgx_snakefile_raw else None
    )

    # Validate required values early with clear guidance. Each may be supplied
    # either via its CLI flag or under the beacon.pgx config namespace.
    missing: list[str] = []
    if ref_fasta is None:
        missing.append("--ref-fasta / beacon.pgx.ref_fasta")
    if output_dir is None:
        missing.append("--output-dir / beacon.pgx.output_dir")
    if pgx_image is None:
        missing.append("--pgx-image / beacon.pgx.pgx_image")
    if missing:
        raise click.UsageError(
            "Required values are missing. Pass them on the command line, or add "
            "them to your extra_config.json under the beacon.pgx namespace:\n"
            + "\n".join(f"  {k}" for k in missing)
        )
    assert ref_fasta is not None and output_dir is not None and pgx_image is not None

    slurm_conf = pgx_conf.get("slurm") or get_config_value(configuration, "beacon.pgx.slurm", {}) or {}
    slurm = beacon_pgx.SlurmConfig(
        time_limit=slurm_conf.get("time_limit", "24:00:00"),
        memory=slurm_conf.get("memory", "32G"),
        cpus=slurm_conf.get("cpus", 8),
        job_name=slurm_conf.get("job_name", f"pgx_{release_id}"),
        extra_args=tuple(slurm_conf.get("extra_args", [])),
    )

    config = beacon_pgx.PgxPipelineConfig(
        release_id=release_id,
        output_dir=output_dir,
        ref_fasta=ref_fasta,
        pgx_image=pgx_image,
        gvcf_dir=gvcf_dir.resolve() if gvcf_dir else None,
        gvcf_list=gvcf_list.resolve() if gvcf_list else None,
        samples_tsv=resolved_samples_tsv,
        executor=resolved_executor,  # type: ignore[arg-type]
        slurm=slurm,
        snakemake_jobs=snakemake_jobs,
        no_pypgx=not pypgx,
        no_report=no_report,
        prepare=prepare,
        dry_run=dry_run,
        force=force,
        cleanup_temp=cleanup_temp,
        pypgx_snakefile=pypgx_snakefile,
        glnexus_image=glnexus_image,
        glnexus_config=glnexus_config,
    )

    started_at = datetime.now().astimezone().isoformat()
    started_perf = time.perf_counter()
    status: beacon_pgx.BatchStatus = "planned"
    error_message: str | None = None
    batch: beacon_pgx.PgxBatch | None = None
    execution_script_path: Path | None = None
    plan_warnings: list[str] = []

    log.info("=" * 46)
    log.info("Beacon pgx — release: %s", release_id)
    log.info("=" * 46)
    log.info("  Input mode:  %s", config.input_mode)
    log.info("  Executor:    %s", resolved_executor)
    log.info("  Output dir:  %s", config.workspace)

    try:
        if dry_run:
            # Validate but do not write anything
            plan_warnings = beacon_pgx.validate_pre_job(config)
            batch = beacon_pgx.build_batch(config)
            log.info("[dry-run] Batch plan for %r: %d sample(s)", release_id, len(batch.samples))
            for s in batch.samples:
                log.info("  %s  sex=%s  country=%s", s.sample_id, s.sex, s.country_code)
            log.info("[dry-run] No files written.")
            return

        batch, execution_script_path = beacon_pgx.plan_batch(config)
        status = "planned"

        log.info("Workspace:        %s", batch.workspace)
        log.info("Execution script: %s", execution_script_path)

        if prepare:
            if resolved_executor == "hpc":
                log.info("--prepare: workspace ready. Submit with: sbatch %s", execution_script_path)
            else:
                log.info("--prepare: workspace ready. Run with: bash %s", execution_script_path)
            return

    except beacon_pgx.ValidationError as exc:
        status = "planned"
        error_message = str(exc)
        raise click.UsageError(str(exc)) from exc

    except Exception as exc:
        status = "planned"
        error_message = f"{type(exc).__name__}: {exc}"
        if isinstance(exc, (click.ClickException, click.UsageError)):
            raise
        raise click.ClickException(str(exc)) from exc

    finally:
        ended_at = datetime.now().astimezone().isoformat()
        duration_seconds = time.perf_counter() - started_perf

        if not dry_run and batch is not None:
            try:
                metrics_file = beacon_pgx.write_pgx_metrics(
                    config=config,
                    batch=batch,
                    execution_script_path=execution_script_path,
                    job_id=None,
                    status=status,
                    started_at=started_at,
                    ended_at=ended_at,
                    duration_seconds=duration_seconds,
                    warnings=plan_warnings,
                    error=error_message,
                )
                log.info("Metrics written: %s", metrics_file)

                if not no_report:
                    try:
                        report_file = beacon_pgx.write_pgx_html_report(
                            metrics_file=metrics_file
                        )
                        log.info("HTML report: %s", report_file)
                    except Exception as report_exc:  # noqa: BLE001
                        log.warning("Could not write HTML report: %s", report_exc)

            except Exception as metrics_exc:  # noqa: BLE001
                log.warning("Could not write metrics: %s", metrics_exc)

@beacon.group("ingest")
@click.option(
    "--run-profile",
    type=click.Choice(["local", "ws", "hpc"]),
    help="Execution environment label recorded in metrics and manifests.",
)
@click.pass_context
def beacon_ingest_group(
    ctx: click.Context,
    run_profile: str | None,
) -> None:
    """Beacon ingestion workflows."""
    configuration = ctx.obj["configuration"]

    ctx.obj["beacon_ingest_run_profile"] = run_profile or get_config_value(
        configuration,
        "beacon.execution.profile",
        "local",
    )

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
    "--set-permissions",
    "permissions_level",
    type=click.Choice(["public", "registered", "controlled"]),
    default=None,
    help="Initial dataset permissions level.",
)
@click.option(
    "--set-email",
    "permissions_email",
    default=None,
    help="Initial controlled user e-mail. Only used with --set-permissions controlled.",
)
@click.option(
    "--is-test",
    type=click.Choice(["y", "n"], case_sensitive=False),
    default=None,
    help="Whether this dataset is a test dataset: y/N.",
)
@click.option(
    "--is-synthetic",
    is_flag=True,
    default=None,
    help="Mark this dataset as synthetic.",
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
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Directory for metrics and logs (default: <base-dir>/logs/).",
)
@click.option(
    "--dry-run",
    is_flag=True,
    help="Discover artifacts without writing files.",
)
@click.option(
    "--no-report",
    is_flag=True,
    help="Skip HTML report generation. Metrics JSON is always written.",
)
@click.pass_context
def ingest_dataset_cmd(
    ctx: click.Context,
    dataset_id: str | None,
    name: str | None,
    description: str | None,
    reference_genome: str | None,
    permissions_level: str | None,
    permissions_email: str | None,
    is_test: str | None,
    is_synthetic: bool | None,
    base_dir: Path,
    granularity: str,
    output_dir: Path | None,
    dry_run: bool,
    no_report: bool,
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

    if permissions_level is None:
        permissions_level = "public"

    if permissions_level == "controlled" and permissions_email is None:
        if click.get_text_stream("stdin").isatty():
            permissions_email = click.prompt("Controlled user e-mail")
        else:
            raise click.UsageError(
                "--set-email is required with --set-permissions controlled "
                "when stdin is not interactive."
            )

    test = (is_test or "n").lower() == "y"
    synthetic = bool(is_synthetic)

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
        permissions_level=permissions_level,
        permissions_email=permissions_email,
        output_dir=output_dir.resolve() if output_dir is not None else None,
        dry_run=dry_run,
        generate_report=not no_report,
    )

    try:
        deployment = (
            None
            if dry_run
            else build_beacon_deployment_config(
                ctx.obj["configuration"],
            )
        )

        result = beacon_ingest.ingest_dataset(
            cfg,
            deployment=deployment,
        )
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
    type=click.Path(
        path_type=Path,
        exists=True,
        dir_okay=False,
    ),
    required=False,
    help="Single VCF or VCF.GZ file to ingest.",
)
@click.option(
    "--vcf-dir",
    type=click.Path(
        path_type=Path,
        exists=True,
        file_okay=False,
    ),
    required=False,
    help="Directory containing VCF or VCF.GZ files for the same dataset.",
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
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    default=None,
    help="Directory for metrics and logs (default: current directory/logs/).",
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
    "--no-report",
    is_flag=True,
    help="Skip HTML report generation. Metrics JSON is always written.",
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
    vcf: Path | None,
    vcf_dir: Path | None,
    reference_genome: str,
    output_dir: Path | None,
    cleanup_old: bool,
    skip_filtering_terms: bool,
    no_report: bool,
    dry_run: bool,
) -> None:
    """Ingest variants into an existing Beacon dataset (stage→swap→cleanup)."""
    configure_module_logging(ctx, "beacon_ingest_variants")

    if vcf is None and vcf_dir is None:
        raise click.UsageError(
            "Provide --vcf (single file) or --vcf-dir (directory)."
        )

    run_profile = ctx.obj["beacon_ingest_run_profile"]

    cfg = beacon_ingest.VariantsIngestConfig(
        dataset_id=dataset_id,
        vcf=vcf.resolve() if vcf is not None else None,
        vcf_dir=vcf_dir.resolve() if vcf_dir is not None else None,
        reference_genome=reference_genome,
        output_dir=output_dir.resolve() if output_dir is not None else None,
        cleanup_old=cleanup_old,
        skip_filtering_terms=skip_filtering_terms,
        dry_run=dry_run,
        run_profile=run_profile,
        generate_report=not no_report,
    )

    try:
        deployment = build_beacon_deployment_config(
            ctx.obj["configuration"],
        )

        result = beacon_ingest.apply_variants_to_remote(
            cfg,
            deployment,
        )
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

    if result.manifest_file is not None:
        log.info("Manifest file:        %s", result.manifest_file)

    if result.report_file is not None:
        log.info("HTML report:          %s", result.report_file)

    if result.process is not None:
        log.info("Stage wall seconds:   %.3f", result.process.wall_seconds)
        log.info("Max RSS MiB:          %.3f", result.process.max_rss_mib)

    if not cleanup_old and not dry_run:
        try:
            deleted_backups = beacon_ingest.offer_old_variant_backups_cleanup(
                dataset_id=dataset_id,
                deployment=deployment,
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
    click.echo(f"Report SBATCH  : {result.report_sbatch_file}")
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

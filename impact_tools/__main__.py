"""Command line interface for impact-tools."""

from __future__ import annotations

import logging
from pathlib import Path

import click
from rich.console import Console
from rich.logging import RichHandler
from rich.traceback import install as install_rich_traceback

from impact_tools import __version__
from impact_tools.ega.encrypt import EncryptionConfig, run_encryption
from impact_tools.ega.upload_inbox import InboxUploadConfig, run_inbox_upload


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


@click.group(context_settings={"help_option_names": ["-h", "--help"]})
@click.version_option(__version__)
@click.option("-v", "--verbose", is_flag=True, help="Show debug log messages.")
@click.option(
    "--log-file",
    type=click.Path(path_type=Path, dir_okay=False),
    help="Write a detailed execution log to this file.",
)
@click.pass_context
def cli(ctx: click.Context, verbose: bool, log_file: Path | None) -> None:
    """Utilities for Go-IMPaCT Beacon and EGA workflows."""
    install_rich_traceback(width=200, word_wrap=True, extra_lines=1)
    configure_logging(verbose=verbose, log_file=log_file)
    ctx.obj = {"verbose": verbose, "log_file": log_file}


@cli.group()
def ega() -> None:
    """Tools for Affiliated EGA workflows."""


@ega.command("encrypt")
@click.option(
    "-i",
    "--input-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    required=True,
    help=(
        "Directory containing sample folders or files to encrypt. Also used as "
        "the base directory for relative paths in --input-list."
    ),
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory for encrypted files, metrics, logs and plots.",
)
@click.option(
    "-k",
    "--recipient-pubkey",
    type=click.Path(path_type=Path, dir_okay=False, exists=True),
    required=True,
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
    help="Skip SHA256 calculation. Faster, but less auditable.",
)
@click.option(
    "--no-plots",
    is_flag=True,
    help="Do not generate PNG plots from metrics.",
)
@click.option(
    "--fail-fast",
    is_flag=True,
    help="Stop at the first failed file instead of continuing with the batch.",
)
def encrypt_cmd(
    input_dir: Path,
    output_dir: Path | None,
    recipient_pubkey: Path,
    crypt4gh_bin: Path | None,
    input_list: Path | None,
    pattern: str,
    sample_id: str | None,
    force: bool,
    dry_run: bool,
    no_checksums: bool,
    no_plots: bool,
    fail_fast: bool,
) -> None:
    """Encrypt sequencing files with Crypt4GH and generate metrics."""
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
        fail_fast=fail_fast,
    )
    try:
        result = run_encryption(config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts to clean error
        raise click.ClickException(str(exc)) from exc
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
    required=True,
    help=(
        "Directory containing encrypted files to upload. Also used as the base "
        "directory for relative paths in --input-list."
    ),
)
@click.option(
    "-o",
    "--output-dir",
    type=click.Path(path_type=Path, file_okay=False),
    help="Directory for upload metrics, logs and manifests.",
)
@click.option("--host", required=True, help="Inbox SFTP host.")
@click.option("--port", default=2222, show_default=True, help="Inbox SFTP port.")
@click.option("-u", "--username", required=True, help="Inbox username.")
@click.option(
    "--remote-dir",
    default="/",
    show_default=True,
    help="Remote inbox directory where files are uploaded.",
)
@click.option(
    "--remote-layout",
    type=click.Choice(["flat", "relative"]),
    default="flat",
    show_default=True,
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
    default="auto-add",
    show_default=True,
    help="How to handle unknown SFTP host keys.",
)
@click.option("--force", is_flag=True, help="Overwrite remote files if they exist.")
@click.option("--dry-run", is_flag=True, help="Plan uploads without connecting.")
@click.option(
    "--no-checksums",
    is_flag=True,
    help="Skip local SHA256 calculation. Faster, but less auditable.",
)
@click.option(
    "--fail-fast",
    is_flag=True,
    help="Stop at the first failed upload instead of continuing with the batch.",
)
@click.option(
    "--connect-timeout",
    default=30,
    show_default=True,
    help="SFTP connection timeout in seconds.",
)
def upload_inbox_cmd(
    input_dir: Path,
    output_dir: Path | None,
    host: str,
    port: int,
    username: str,
    remote_dir: str,
    remote_layout: str,
    input_list: Path | None,
    pattern: str,
    identity_file: Path | None,
    ask_password: bool,
    host_key_policy: str,
    force: bool,
    dry_run: bool,
    no_checksums: bool,
    fail_fast: bool,
    connect_timeout: int,
) -> None:
    """Upload encrypted .c4gh files to a LocalEGA inbox over SFTP."""
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
        fail_fast=fail_fast,
        connect_timeout=connect_timeout,
    )
    try:
        result = run_inbox_upload(config)
    except Exception as exc:  # noqa: BLE001 - CLI boundary converts to clean error
        raise click.ClickException(str(exc)) from exc
    if result.failed > 0:
        raise click.ClickException(
            f"Inbox upload finished with {result.failed} failed file(s). "
            f"See log: {result.log_file}"
        )


def main() -> None:
    """Console entry point."""
    cli()


if __name__ == "__main__":
    main()

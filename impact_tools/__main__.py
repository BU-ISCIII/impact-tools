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
from impact_tools.beacon import liftover as beacon_liftover
from impact_tools.beacon import pgx as beacon_pgx

log = logging.getLogger(__name__)


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
def liftover_cmd(
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
        msg = "Continue with liftover execution?"
        if cleanup:
            msg = "Continue with liftover execution? (intermediate files will be removed on success)"
        if not click.confirm(msg, default=False):
            log.info("Cancelled.")
            return

    config = beacon_liftover.LiftoverConfig(
        base_dir=base_dir,
        chain=chain,
        fasta=fasta,
        hpc_mount=hpc_mount,
        bcftools_image=bcftools_image,
        crossmap_image=crossmap_image,
        cleanup=cleanup,
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


@beacon.command("pgx")
@click.option(
    "-b",
    "--base-dir",
    type=click.Path(path_type=Path, file_okay=False, exists=True),
    default=Path("."),
    show_default="current working directory",
    help="Base working directory.",
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
def pgx_cmd(
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
    existing_ids = {r.sample_id for r in existing}

    new_vcfs = [
        vcf for vcf in lifted_vcfs
        if beacon_pgx.vcf_to_sample_id(vcf) not in existing_ids
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


def main() -> None:
    """Console entry point."""
    cli()


if __name__ == "__main__":
    main()

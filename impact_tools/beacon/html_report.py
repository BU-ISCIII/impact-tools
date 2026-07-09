"""Self-contained HTML reports for Beacon workflows."""

from __future__ import annotations

from importlib.resources import files
from pathlib import Path

from jinja2 import Environment, PackageLoader, select_autoescape


COLORS = ["#2563eb", "#0891b2", "#7c3aed", "#db2777", "#ea580c", "#16a34a"]
RESOURCE_PACKAGE = "impact_tools.beacon.resources"

TEMPLATE_ENVIRONMENT = Environment(
    loader=PackageLoader("impact_tools.beacon", "resources"),
    autoescape=select_autoescape(("html", "xml")),
    trim_blocks=True,
    lstrip_blocks=True,
)


def write_html_report(
    path: Path,
    *,
    template_name: str,
    context: dict,
) -> Path:
    """Render a self-contained Beacon HTML report from a Jinja template."""
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True)

    report_context = dict(context)
    report_context.setdefault(
        "styles",
        files(RESOURCE_PACKAGE)
        .joinpath("report.css")
        .read_text(encoding="utf-8"),
    )

    _render(
        path,
        template_name,
        report_context,
    )

    return path


def write_dataset_ingest_report(
    path: Path,
    payload: dict,
    include_charts: bool = True,
) -> None:
    """Write one visual report for a Beacon dataset ingest run."""
    dataset = payload.get("dataset") or {}
    environment = payload.get("environment") or {}
    paths = payload.get("paths") or {}
    generated_files = payload.get("generated_files") or []

    remote_payload = payload.get("remote")
    remote = remote_payload or {}
    remote_available = remote_payload is not None

    generated_count = sum(
        1 for item in generated_files if item.get("exists") is True
    )
    generated_size = sum(
        item.get("size_bytes") or 0 for item in generated_files
    )

    performance_charts = []

    if include_charts and generated_files:
        performance_charts.append(
            _chart(
                "Generated file sizes",
                "bytes",
                [
                    Path(item.get("path") or "unknown").name
                    for item in generated_files
                ],
                [
                    item.get("size_bytes") or 0
                    for item in generated_files
                ],
            )
        )

    if include_charts and remote_available:
        performance_charts.append(
            _chart(
                "Mongo registration",
                "documents",
                ["Imported", "Dataset count"],
                [
                    remote.get("mongo_imported") or 0,
                    remote.get("mongo_count") or 0,
                ],
            )
        )

    validation_rows = [
        _table_row(
            [
                "Workflow status",
                payload.get("status"),
                (
                    "ok"
                    if payload.get("status") == "success"
                    else "failed"
                ),
            ],
            status_column=2,
        ),
        _table_row(
            [
                "Generated files",
                f"{generated_count}/{len(generated_files)}",
                (
                    "ok"
                    if generated_files
                    and generated_count == len(generated_files)
                    else "failed"
                ),
            ],
            status_column=2,
        ),
    ]

    if remote_available:
        validation_rows.extend(
            [
                _table_row(
                    [
                        "Dataset present in MongoDB",
                        remote.get("mongo_count"),
                        (
                            "ok"
                            if (remote.get("mongo_count") or 0) > 0
                            else "failed"
                        ),
                    ],
                    status_column=2,
                ),
                _table_row(
                    [
                        "Dataset visible through API",
                        remote.get("api_visible"),
                        (
                            "ok"
                            if remote.get("api_visible") is True
                            else "failed"
                        ),
                    ],
                    status_column=2,
                ),
            ]
        )

    context = _base_context(
        page_title="Beacon ingest dataset report",
        report_title=(
            f"Beacon dataset ingest: "
            f"{dataset.get('dataset_id', 'unknown')}"
        ),
        badge=payload.get("status", "unknown"),
        summary_cards=[
            _card(
                "Status",
                payload.get("status"),
                "workflow result",
            ),
            _card(
                "Mode",
                "dry-run" if dataset.get("dry_run") else "apply",
                "execution mode",
            ),
            _card(
                "Dataset",
                dataset.get("dataset_id"),
                dataset.get("name") or "dataset ID",
            ),
            _card(
                "Generated",
                generated_count,
                f"{len(generated_files)} expected files",
            ),
            _card(
                "Artifacts size",
                _number(generated_size),
                "combined bytes",
            ),
            _card(
                "Mongo imported",
                (
                    remote.get("mongo_imported")
                    if remote_available
                    else "NA"
                ),
                "documents imported",
            ),
            _card(
                "Mongo count",
                (
                    remote.get("mongo_count")
                    if remote_available
                    else "NA"
                ),
                "registered datasets",
            ),
            _card(
                "API visible",
                (
                    remote.get("api_visible")
                    if remote_available
                    else "NA"
                ),
                "Beacon API verification",
            ),
            _card(
                "Runtime",
                _duration(payload.get("duration_seconds")),
                "wall time",
            ),
        ],
        performance_charts=performance_charts,
    )

    context.update(
        run_details=_items(
            [
                ("Dataset ID", dataset.get("dataset_id")),
                ("Name", dataset.get("name")),
                ("Description", dataset.get("description")),
                ("Reference genome", dataset.get("reference_genome")),
                ("Granularity", dataset.get("granularity")),
                ("Test dataset", dataset.get("is_test")),
                ("Synthetic dataset", dataset.get("is_synthetic")),
                ("Dry run", dataset.get("dry_run")),
                ("Started at", payload.get("started_at")),
                ("Ended at", payload.get("ended_at")),
                ("Command", payload.get("command")),
                ("Error", payload.get("error")),
                ("Base directory", paths.get("base_dir")),
                (
                    "Dataset config directory",
                    paths.get("dataset_config_dir"),
                ),
                (
                    "Dataset work directory",
                    paths.get("dataset_work_dir"),
                ),
                (
                    "Dataset input directory",
                    paths.get("dataset_input_dir"),
                ),
            ]
        ),
        environment=_items(
            [
                ("Host", environment.get("hostname")),
                ("Platform", environment.get("platform")),
                ("Python", environment.get("python")),
                ("Working directory", environment.get("cwd")),
            ]
        ),
        generated_headers=[
            "File",
            "Path",
            "Status",
            "Size (bytes)",
        ],
        generated_rows=[
            _table_row(
                [
                    Path(item.get("path") or "unknown").name,
                    item.get("path"),
                    "ok" if item.get("exists") else "failed",
                    item.get("size_bytes"),
                ],
                path_columns={1},
                status_column=2,
            )
            for item in generated_files
        ],
        validation_headers=[
            "Check",
            "Value",
            "Status",
        ],
        validation_rows=validation_rows,
        artifacts=[
            {"label": label, "path": target}
            for label, target in [
                ("Metrics JSON", payload.get("metrics_file")),
                ("HTML report", payload.get("report_file")),
            ]
            if target
        ],
    )

    write_html_report(
        path,
        template_name="dataset_ingest_report.html",
        context=context,
    )


def write_liftover_report(
    path: Path,
    payload: dict,
    include_charts: bool = True,
) -> None:
    """Write one visual report for a Beacon liftover run."""
    config = payload.get("config") or {}
    environment = payload.get("environment") or {}
    summary = payload.get("summary") or {}
    samples = payload.get("samples") or []

    performance_charts = []

    if include_charts:
        performance_charts.append(
            _chart(
                "Sample status",
                "samples",
                ["Successful", "Warnings", "Failed"],
                [
                    summary.get("succeeded") or 0,
                    summary.get("warned") or 0,
                    summary.get("failed") or 0,
                ],
            )
        )

    if include_charts and samples:
        performance_charts.append(
            _chart(
                "Variants per sample",
                "variants",
                [
                    sample.get("sample_id") or "unknown"
                    for sample in samples
                ],
                [
                    sample.get("n_variants") or 0
                    for sample in samples
                ],
            )
        )

        performance_charts.append(
            _chart(
                "Runtime per sample",
                "seconds",
                [
                    sample.get("sample_id") or "unknown"
                    for sample in samples
                ],
                [
                    sample.get("duration_seconds") or 0
                    for sample in samples
                ],
            )
        )

    context = _base_context(
        page_title="Beacon liftover report",
        report_title="Beacon GRCh37 to GRCh38 liftover",
        badge=payload.get("status", "unknown"),
        summary_cards=[
            _card(
                "Status",
                payload.get("status"),
                "workflow result",
            ),
            _card(
                "Samples",
                summary.get("samples"),
                "input VCF files",
            ),
            _card(
                "Successful",
                summary.get("succeeded"),
                "validated outputs",
            ),
            _card(
                "Warnings",
                summary.get("warned"),
                "outputs requiring review",
            ),
            _card(
                "Failed",
                summary.get("failed"),
                "samples with errors",
            ),
            _card(
                "Variants",
                summary.get("variants"),
                "output records",
            ),
            _card(
                "Runtime",
                _duration(payload.get("duration_seconds")),
                "total wall time",
            ),
        ],
        performance_charts=performance_charts,
    )

    context.update(
        run_details=_items(
            [
                ("Base directory", config.get("base_dir")),
                ("Chain file", config.get("chain")),
                ("Reference FASTA", config.get("fasta")),
                ("bcftools image", config.get("bcftools_image")),
                ("CrossMap image", config.get("crossmap_image")),
                ("Workers", config.get("workers")),
                ("Started at", payload.get("started_at")),
                ("Ended at", payload.get("ended_at")),
                ("Command", payload.get("command")),
                ("Workflow error", payload.get("error")),
            ]
        ),
        environment=_items(
            [
                ("Host", environment.get("hostname")),
                ("Platform", environment.get("platform")),
                ("Python", environment.get("python")),
                ("Working directory", environment.get("cwd")),
            ]
        ),
        sample_headers=[
            "Sample",
            "Status",
            "Variants",
            "Runtime",
            "Failed step",
            "Input VCF",
            "Output VCF",
            "Error",
        ],
        sample_rows=[
            _table_row(
                [
                    sample.get("sample_id"),
                    sample.get("status"),
                    sample.get("n_variants"),
                    _duration(sample.get("duration_seconds")),
                    sample.get("failed_step"),
                    (sample.get("input_vcf") or {}).get("path"),
                    (sample.get("output_vcf") or {}).get("path"),
                    sample.get("error"),
                ],
                path_columns={5, 6},
                status_column=1,
            )
            for sample in samples
        ],
        artifacts=[
            {"label": label, "path": target}
            for label, target in [
                ("Metrics JSON", payload.get("metrics_file")),
                ("HTML report", payload.get("report_file")),
            ]
            if target
        ],
    )

    write_html_report(
        path,
        template_name="liftover_report.html",
        context=context,
    )


def write_pgx_report(
    path: Path,
    payload: dict,
    include_charts: bool = True,
) -> None:
    """Write one visual report for a Beacon PGx run."""
    config = payload.get("config") or {}
    environment = payload.get("environment") or {}
    summary = payload.get("summary") or {}
    samples = payload.get("samples") or []

    performance_charts = []

    if include_charts:
        performance_charts.append(
            _chart(
                "Pipeline step status",
                "steps",
                ["Successful", "Warnings", "Failed"],
                [
                    summary.get("succeeded") or 0,
                    summary.get("warned") or 0,
                    summary.get("failed") or 0,
                ],
            )
        )

    run_samples = [
        sample
        for sample in samples
        if sample.get("pgx_run") is not None
    ]

    if include_charts and run_samples:
        performance_charts.append(
            _chart(
                "PGx runtime per sample",
                "seconds",
                [
                    sample.get("sample_id") or "unknown"
                    for sample in run_samples
                ],
                [
                    (sample.get("pgx_run") or {}).get(
                        "duration_seconds"
                    )
                    or 0
                    for sample in run_samples
                ],
            )
        )

    context = _base_context(
        page_title="Beacon PGx report",
        report_title="Beacon pharmacogenomics workflow",
        badge=payload.get("status", "unknown"),
        summary_cards=[
            _card(
                "Status",
                payload.get("status"),
                "workflow result",
            ),
            _card(
                "Execution mode",
                config.get("executor"),
                "execution mode",
            ),
            _card(
                "Lifted VCFs",
                summary.get("lifted_vcfs"),
                "source files",
            ),
            _card(
                "Samples",
                summary.get("samples_discovered"),
                "discovered samples",
            ),
            _card(
                "New samples",
                summary.get("new_samples"),
                "added to samples.tsv",
            ),
            _card(
                "Sex failures",
                summary.get("sex_inference_failed"),
                "unresolved samples",
            ),
            _card(
                "Successful",
                summary.get("succeeded"),
                "completed steps",
            ),
            _card(
                "Warnings",
                summary.get("warned"),
                "steps requiring review",
            ),
            _card(
                "Failed",
                summary.get("failed"),
                "failed steps",
            ),
            _card(
                "Runtime",
                _duration(payload.get("duration_seconds")),
                "total wall time",
            ),
        ],
        performance_charts=performance_charts,
    )

    context.update(
        run_details=_items(
            [
                ("Base directory", config.get("base_dir")),
                ("Executor", config.get("executor")),
                ("Container runtime", config.get("container_runtime")),
                ("Country code", config.get("country_code")),
                (
                    "Sex ambiguous minimum",
                    config.get("sex_ambiguous_min"),
                ),
                (
                    "Sex ambiguous maximum",
                    config.get("sex_ambiguous_max"),
                ),
                ("bcftools image", config.get("bcftools_image")),
                ("PGx image", config.get("pgx_image")),
                ("PGx repository", config.get("pgx_repo")),
                ("Snakemake jobs", config.get("snakemake_jobs")),
                ("Workers", config.get("workers")),
                ("Started at", payload.get("started_at")),
                ("Ended at", payload.get("ended_at")),
                ("Command", payload.get("command")),
                ("Workflow error", payload.get("error")),
            ]
        ),
        environment=_items(
            [
                ("Host", environment.get("hostname")),
                ("Platform", environment.get("platform")),
                ("Python", environment.get("python")),
                ("Working directory", environment.get("cwd")),
            ]
        ),
        sample_headers=[
            "Sample",
            "Source VCF",
            "Sex",
            "Sex resolution",
            "chrY variants",
            "Workspace",
            "Prepare status",
            "PGx status",
            "Runtime",
            "PASS output",
            "Error",
        ],
        sample_rows=[
            _table_row(
                [
                    sample.get("sample_id"),
                    sample.get("vcf_basename"),
                    sample.get("sex"),
                    (
                        sample.get("sex_inference") or {}
                    ).get("status"),
                    (
                        sample.get("sex_inference") or {}
                    ).get("n_chry"),
                    (
                        sample.get("workspace") or {}
                    ).get("path"),
                    (
                        sample.get("workspace") or {}
                    ).get("status"),
                    (
                        sample.get("pgx_run") or {}
                    ).get("status"),
                    _duration(
                        (
                            sample.get("pgx_run") or {}
                        ).get("duration_seconds")
                    ),
                    (
                        (
                            sample.get("pgx_run") or {}
                        ).get("output_pass")
                        or {}
                    ).get("path"),
                    (
                        (sample.get("pgx_run") or {}).get("error")
                        or (sample.get("workspace") or {}).get("error")
                    ),
                ],
                path_columns={1, 5, 9},
                status_column=7,
            )
            for sample in samples
        ],
        artifacts=[
            {"label": label, "path": target}
            for label, target in [
                ("Metrics JSON", payload.get("metrics_file")),
                ("HTML report", payload.get("report_file")),
            ]
            if target
        ],
    )

    write_html_report(
        path,
        template_name="pgx_report.html",
        context=context,
    )


def write_variant_ingest_report(
    path: Path,
    payload: dict,
    include_charts: bool = True,
) -> None:
    """Write one visual report for a Beacon variant ingest run."""
    execution = payload["execution"]
    run = payload["run"]
    summary = payload["summary"]
    process = summary.get("process") or {}

    validation_status = "ok" if summary.get("api_count_valid") is True else "pending"

    context = _base_context(
        page_title="Beacon ingest variants report",
        report_title="Beacon variant ingest run",
        badge=execution.get("profile", "unknown"),
        summary_cards=[
            _card("Profile", execution.get("profile", "NA"), execution.get("hostname", "NA")),
            _card("Mode", "dry-run" if run.get("dry_run") else "swap", "execution mode"),
            _card("VCF files", run.get("vcf_files"), "input files"),
            _card("VCF records", summary.get("vcf_count"), "raw input records"),
            _card("Processed", summary.get("processed_count"), "RI-tools processed"),
            _card("Inserted", summary.get("inserted_count"), "RI-tools inserted"),
            _card("Skipped", summary.get("skipped_count"), "RI-tools skipped"),
            _card("Mongo", summary.get("mongo_count"), "documents"),
            _card("API valid", summary.get("api_count_valid"), "count verification"),
            _card("Runtime", _duration(process.get("wall_seconds")), "wall time"),
            _card("Peak memory", _number(process.get("max_rss_mib")), "MiB observed RSS"),
        ],
        performance_charts=[
            _chart(
                "Variant counts",
                "records",
                ["VCF", "Processed", "Inserted", "Skipped", "Mongo"],
                [
                    summary.get("vcf_count") or 0,
                    summary.get("processed_count") or 0,
                    summary.get("inserted_count") or 0,
                    summary.get("skipped_count") or 0,
                    summary.get("mongo_count") or 0,
                ],
            ),
            _chart(
                "Runtime",
                "seconds",
                ["Wall", "User", "System"],
                [
                    process.get("wall_seconds") or 0,
                    process.get("user_seconds") or 0,
                    process.get("system_seconds") or 0,
                ],
            ),
        ]
        if include_charts
        else [],
    )

    context.update(
        run_details=_items(
            [
                ("Dataset ID", run.get("dataset_id")),
                ("Staging ID", run.get("staging_id")),
                ("Old ID", run.get("old_id")),
                ("Reference genome", run.get("reference_genome")),
                ("Run profile", run.get("run_profile")),
                ("Dry run", run.get("dry_run")),
                ("Cleanup old", run.get("cleanup_old")),
                ("Input mode", run.get("input_mode")),
                ("VCF directory", run.get("vcf_dir")),
                ("VCF files", run.get("vcf_files")),
                (
                    "Local VCFs",
                    "; ".join(run.get("local_vcfs") or []),
                ),
                (
                    "Remote VCFs",
                    "; ".join(run.get("remote_vcfs") or []),
                ),
                ("Base directory", run.get("base_dir")),
            ]
        ),
        environment=_items(
            [
                ("Host", execution.get("hostname")),
                ("CPU", execution.get("cpu_model")),
                ("Logical CPUs", execution.get("cpu_count")),
                ("System memory", _gib(execution.get("memory_bytes")) + " GiB"),
                ("SLURM job", execution.get("slurm_job_id")),
                ("SLURM array task", execution.get("slurm_array_task_id")),
                ("SLURM partition", execution.get("slurm_partition")),
                ("CPUs per task", execution.get("slurm_cpus_per_task")),
                ("SLURM memory", execution.get("slurm_mem_per_node")),
                ("Python", execution.get("python_version")),
            ]
        ),
        validation_headers=[
            "Check",
            "Value",
            "Status",
        ],
        validation_rows=[
            _table_row(
                [
                    "Raw VCF records",
                    summary.get("vcf_count"),
                    "ok"
                    if summary.get("vcf_count") is not None
                    else "pending",
                ],
                status_column=2,
            ),
            _table_row(
                [
                    "RI-tools processed",
                    summary.get("processed_count"),
                    "ok"
                    if summary.get("processed_count") is not None
                    else "pending",
                ],
                status_column=2,
            ),
            _table_row(
                [
                    "RI-tools inserted",
                    summary.get("inserted_count"),
                    "ok"
                    if summary.get("inserted_count") is not None
                    else "pending",
                ],
                status_column=2,
            ),
            _table_row(
                [
                    "RI-tools skipped",
                    summary.get("skipped_count"),
                    "ok"
                    if summary.get("skipped_count") is not None
                    else "pending",
                ],
                status_column=2,
            ),
            _table_row(
                [
                    "Mongo variants",
                    summary.get("mongo_count"),
                    (
                        "ok"
                        if summary.get("mongo_count")
                        == summary.get("inserted_count")
                        and summary.get("mongo_count") is not None
                        else "pending"
                    ),
                ],
                status_column=2,
            ),
            _table_row(
                [
                    "API visible",
                    summary.get("api_visible"),
                    "ok"
                    if summary.get("api_visible") is True
                    else "pending",
                ],
                status_column=2,
            ),
            _table_row(
                [
                    "API count valid",
                    summary.get("api_count_valid"),
                    validation_status,
                ],
                status_column=2,
            ),
            _table_row(
                [
                    "Deleted old variants",
                    summary.get("deleted_old_variants"),
                    (
                        "ok"
                        if summary.get("deleted_old_variants") is not None
                        else "pending"
                    ),
                ],
                status_column=2,
            ),
        ],
        artifacts=[
            {"label": label, "path": target}
            for label, target in [
                ("Manifest", payload.get("manifest_file")),
                ("HTML report", payload.get("report_file")),
            ]
            if target
        ],
    )

    write_html_report(
        path,
        template_name="variant_ingest_report.html",
        context=context,
    )


def _base_context(**values) -> dict:
    styles = files(RESOURCE_PACKAGE).joinpath("report.css").read_text(encoding="utf-8")
    return {"styles": styles, **values}


def _render(path: Path, template_name: str, context: dict) -> None:
    rendered = TEMPLATE_ENVIRONMENT.get_template(template_name).render(**context)
    path.write_text(rendered, encoding="utf-8")


def _card(label: str, value: object, note: str) -> dict:
    return {"label": label, "value": _display(value), "note": note}


def _items(values: list[tuple[str, object]]) -> list[dict]:
    return [
        {"label": label, "value": _display(value)}
        for label, value in values
        if value not in (None, "")
    ]


def _chart(
    title: str,
    unit: str,
    labels: list[str],
    values: list[float],
) -> dict:
    maximum = max(values, default=0) or 1
    return {
        "title": title,
        "unit": unit,
        "rows": [
            {
                "label": label,
                "value": value,
                "display": _number(value),
                "width": f"{max(0, min(100, (value / maximum) * 100)):.2f}",
                "color": COLORS[index % len(COLORS)],
            }
            for index, (label, value) in enumerate(zip(labels, values))
        ],
    }


def _table_row(
    values: list,
    path_columns: set[int] | None = None,
    status_column: int | None = None,
) -> list[dict]:
    path_columns = path_columns or set()
    cells = []

    for index, value in enumerate(values):
        classes = []

        if index in path_columns:
            classes.append("path")

        if index == status_column:
            classes.extend(["status", f"status-{_status_class(value)}"])

        cells.append(
            {
                "value": _display(value),
                "muted": value is None or value == "",
                "css_class": " ".join(classes),
                "title": value if index in path_columns else None,
            }
        )

    return cells


def _gib(size: int | None) -> str:
    return f"{size / (1024**3):.2f}" if size is not None else "NA"


def _duration(seconds: float | None) -> str:
    if seconds is None:
        return "NA"

    if seconds < 60:
        return f"{seconds:.2f}s"

    minutes, remainder = divmod(seconds, 60)

    if minutes < 60:
        return f"{int(minutes)}m {remainder:.0f}s"

    hours, minutes = divmod(minutes, 60)

    return f"{int(hours)}h {int(minutes)}m"


def _display(value: object) -> object:
    if isinstance(value, float):
        return f"{value:.3f}"

    if isinstance(value, bool):
        return str(value)

    return value


def _number(value: float | int | None) -> str:
    if value is None:
        return "NA"

    if value == 0:
        return "0"

    if abs(value) < 0.01:
        return f"{value:.4f}"

    if abs(value) < 1:
        return f"{value:.3f}"

    return f"{value:.2f}"


def _status_class(value: object) -> str:
    normalized = str(value).strip().lower()

    if normalized in {"ok", "success", "true"}:
        return "ok"

    if normalized in {"warn", "warning", "pending"}:
        return "pending"

    return "failed"
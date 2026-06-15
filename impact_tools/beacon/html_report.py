"""Self-contained HTML reports for Beacon ingest metrics."""

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
                ("Skip filtering terms", run.get("skip_filtering_terms")),
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

    _render(path, "variant_ingest_report.html", context)


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
    return "ok" if value == "ok" else "failed"
"""Build Beacon RI-tools metadata CSVs from DRAGEN germline output folders.

For every sample in a sample list, the DRAGEN output folder
``<input_dir>/<sample_id>/`` is read and one row is written to each of the
RI-tools ``individuals``, ``biosamples``, ``runs`` and ``analyses`` CSVs, using
the column names of ``beacon2-ri-tools-v2/csv/templates``. ``datasets.csv``
(summary for ``datasets.info``) and ``cohorts.csv`` (one cohort for the whole
sample list) are completed by ``ingest dataset`` with the dataset id and name
given on its command line.

Values that DRAGEN cannot provide (biosample status and origin) are left
empty; nothing is invented. The sample identifier is used as the id of the
individual, biosample, run and analysis.
"""

from __future__ import annotations

import csv
import gzip
import json
import logging
import re
import statistics
from collections import Counter
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from impact_tools.beacon.ritools import ritools_csv_headers
from impact_tools.ega.sample_files import read_sample_ids
from impact_tools.ega.submission.prepare import (
    _coverage_metric,
    _library_layout,
    _metric_value,
    _read_csv_rows,
)

LOGGER = logging.getLogger(__name__)

# Sex is inferred from DRAGEN's estimated karyotype (team decision).
SEX_BY_KARYOTYPE = {
    "XY": ("NCIT:C20197", "male"),
    "XX": ("NCIT:C16576", "female"),
}
UNKNOWN_SEX = ("NCIT:C17998", "unknown")

WGS_DATA_TYPE = ("OBI:0002117", "whole genome sequencing assay")

# Time zones found in DRAGEN ``##DRAGENCommandLine`` Date values.
UTC_OFFSET_HOURS = {"UTC": 0, "GMT": 0, "CET": 1, "CEST": 2}

LIBRARY_SELECTION = "RANDOM"
LIBRARY_SOURCE = ("GENEPIO:0001966", "genomic source")
PLATFORM = "Illumina"


@dataclass(frozen=True)
class PrepareMetadataConfig:
    """Configuration for building Beacon metadata CSVs from DRAGEN output."""

    input_dir: Path
    sample_list: Path
    output_dir: Path


@dataclass(frozen=True)
class PrepareMetadataResult:
    """CSV files written by prepare_metadata."""

    datasets: Path
    cohorts: Path
    individuals: Path
    biosamples: Path
    runs: Path
    analyses: Path
    sample_count: int


@dataclass(frozen=True)
class DragenSample:
    """Values extracted from one DRAGEN output folder."""

    sample_id: str
    karyotype: str
    library_layout: str
    dragen_version: str
    analysis_date: str
    analysis_timestamp: str
    enabled_modules: list[str]
    qc: dict[str, float | int]


def prepare_metadata(config: PrepareMetadataConfig) -> PrepareMetadataResult:
    """Read every listed DRAGEN folder and write the four RI-tools CSVs."""
    input_dir = config.input_dir.expanduser().resolve()
    sample_ids = read_sample_ids(config.sample_list)

    samples: list[DragenSample] = []
    errors: list[str] = []
    for sample_id in sample_ids:
        try:
            samples.append(read_dragen_sample(input_dir / sample_id, sample_id))
        except (FileNotFoundError, ValueError) as exc:
            errors.append(f"Sample {sample_id!r}: {exc}")

    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise ValueError("DRAGEN metadata extraction failed:\n" + details)

    output_dir = config.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    result = PrepareMetadataResult(
        datasets=output_dir / "datasets.csv",
        cohorts=output_dir / "cohorts.csv",
        individuals=output_dir / "individuals.csv",
        biosamples=output_dir / "biosamples.csv",
        runs=output_dir / "runs.csv",
        analyses=output_dir / "analyses.csv",
        sample_count=len(samples),
    )

    # Full RI-tools template columns for every entity; the _row helpers fill the
    # DRAGEN-derived values and leave the remaining template columns empty, to be
    # completed by hand before ingest. ingest dataset fills id/name/description.
    _write_csv(result.datasets, ritools_csv_headers("datasets"), [_dataset_row(samples)])
    _write_csv(result.cohorts, ritools_csv_headers("cohorts"), [_cohort_row(samples)])
    _write_csv(result.individuals, ritools_csv_headers("individuals"), [_individual_row(s) for s in samples])
    _write_csv(result.biosamples, ritools_csv_headers("biosamples"), [_biosample_row(s) for s in samples])
    _write_csv(result.runs, ritools_csv_headers("runs"), [_run_row(s) for s in samples])
    _write_csv(result.analyses, ritools_csv_headers("analyses"), [_analysis_row(s) for s in samples])
    return result


def read_dragen_sample(sample_dir: Path, sample_id: str) -> DragenSample:
    """Extract Beacon metadata values from one DRAGEN output folder."""
    if not sample_dir.is_dir():
        raise FileNotFoundError(f"sample directory not found: {sample_dir}")

    mapping_file = _require_file(sample_dir / f"{sample_id}.mapping_metrics.csv")
    coverage_file = _require_file(sample_dir / f"{sample_id}.wgs_coverage_metrics.csv")
    ploidy_file = _require_file(sample_dir / f"{sample_id}.ploidy_estimation_metrics.csv")
    replay_file = _require_file(sample_dir / f"{sample_id}-replay.json")
    vcf_file = _require_file(sample_dir / f"{sample_id}.hard-filtered.vcf.gz")

    mapping_rows = _read_csv_rows(mapping_file)
    coverage_rows = _read_csv_rows(coverage_file)

    layout = _library_layout(mapping_rows, mapping_file).value
    if layout is None:
        raise ValueError(f"paired-read metrics not found in {mapping_file.name}")

    karyotype = _metric_value(_read_csv_rows(ploidy_file), "Ploidy estimation")
    if not karyotype:
        raise ValueError(f"'Ploidy estimation' not found in {ploidy_file.name}")

    dragen_version, analysis_timestamp = _dragen_version_and_timestamp(vcf_file)

    command_line = json.loads(replay_file.read_text(encoding="utf-8")).get("command_line", "")
    enabled_modules = re.findall(r"--enable-([\w-]+) true", command_line)

    # libraryStrategy is hardcoded to "WGS" in _run_row. DRAGEN restricts variant
    # calling to a capture target only with --vc-target-bed, so its presence means
    # the sample is not WGS (exome/panel). Warn instead of mislabeling it silently.
    if re.search(r"--vc-target-bed[ =]", command_line):
        LOGGER.warning(
            "Sample %s ran with --vc-target-bed (targeted/exome calling), but "
            "libraryStrategy is hardcoded to 'WGS'. Review runs.csv before ingest.",
            sample_id,
        )

    qc = {
        "totalReads": _metric_value(mapping_rows, "Total input reads"),
        "duplicatePct": _metric_percent(mapping_rows, "Number of duplicate marked reads"),
        "mappedPct": _metric_percent(mapping_rows, "Mapped reads"),
        "meanCoverage": _coverage_metric(coverage_rows, "Average alignment coverage over genome"),
        "pct20x": _coverage_metric(coverage_rows, "PCT of genome with coverage [  20x: inf)"),
        "pct10x": _coverage_metric(coverage_rows, "PCT of genome with coverage [  10x: inf)"),
        "uniformity02xMeanPct": _coverage_metric(
            coverage_rows, "Uniformity of coverage (PCT > 0.2*mean) over genome"
        ),
    }
    missing = [key for key, value in qc.items() if value in (None, "", "NA")]
    if missing:
        raise ValueError(f"QC metrics not found: {', '.join(missing)}")

    return DragenSample(
        sample_id=sample_id,
        karyotype=karyotype,
        library_layout=layout,
        dragen_version=dragen_version,
        analysis_date=analysis_timestamp[:10],
        analysis_timestamp=analysis_timestamp,
        enabled_modules=enabled_modules,
        qc={key: _number(value) for key, value in qc.items()},
    )


def _dragen_version_and_timestamp(vcf_file: Path) -> tuple[str, str]:
    """Return (DRAGEN version, UTC ISO 8601 analysis timestamp) from the VCF header.

    Reads ``##DRAGENCommandLine=<ID=dragen,Version="SW: 07.031.779.4.4.6, ...",
    Date="Thu Apr 23 12:37:13 CEST 2026",...>``.
    """
    with gzip.open(vcf_file, "rt", encoding="utf-8") as handle:
        for line in handle:
            if not line.startswith("##"):
                break
            if not line.startswith("##DRAGENCommandLine=<ID=dragen,"):
                continue
            version = re.search(r'Version="SW: ([^,"]+)', line)
            date = re.search(r'Date="([^"]+)"', line)
            if not version or not date:
                break
            # "07.031.779.4.4.6" -> "4.4.6"
            dragen_version = ".".join(version.group(1).split(".")[-3:])
            # "Thu Apr 23 12:37:13 CEST 2026" -> "2026-04-23T10:37:13Z"
            _, month, day, clock, zone, year = date.group(1).split()
            if zone not in UTC_OFFSET_HOURS:
                raise ValueError(
                    f"Unknown time zone {zone!r} in DRAGEN Date of {vcf_file.name}"
                )
            local = datetime.strptime(f"{month} {day} {year} {clock}", "%b %d %Y %H:%M:%S")
            utc = local - timedelta(hours=UTC_OFFSET_HOURS[zone])
            return dragen_version, utc.strftime("%Y-%m-%dT%H:%M:%SZ")
    raise ValueError(
        f"'##DRAGENCommandLine=<ID=dragen,...>' with Version and Date "
        f"not found in {vcf_file.name}"
    )


def _metric_percent(rows: list[list[str]], metric: str) -> str | None:
    for row in rows:
        if len(row) >= 5 and row[2] == metric:
            return row[4]
    return None


def _number(value: str) -> float | int:
    return int(value) if value.isdigit() else float(value)


def _require_file(path: Path) -> Path:
    if not path.is_file():
        raise FileNotFoundError(f"required DRAGEN file not found: {path}")
    return path


def dataset_document(row: dict[str, str]) -> dict:
    """Turn one datasets.csv row into a Beacon dataset document.

    Empty cells are left out. ``info`` is parsed as JSON. The
    ``dataUseConditions|duoDataUse|<field>`` columns become the
    ``dataUseConditions.duoDataUse`` list: several DUO terms are written as
    ``|``-separated values, aligned across those columns.
    """
    document: dict = {}
    duo: dict[str, list[str]] = {}

    for column, value in row.items():
        if value in ("", None):
            continue
        if column == "info":
            document["info"] = json.loads(value)
        elif column.startswith("dataUseConditions|duoDataUse|"):
            duo[column.rsplit("|", 1)[1]] = value.split("|")
        elif "|" in column:
            raise ValueError(f"Unsupported datasets.csv column: {column}")
        else:
            document[column] = value

    if duo:
        if len({len(values) for values in duo.values()}) != 1:
            raise ValueError(
                "dataUseConditions|duoDataUse columns must have the same "
                f"number of '|'-separated values: {duo}"
            )
        document["dataUseConditions"] = {
            "duoDataUse": [dict(zip(duo, values)) for values in zip(*duo.values())]
        }

    return document


def _dataset_row(samples: list[DragenSample]) -> dict[str, str]:
    summary = {
        "dragenVersions": sorted({s.dragen_version for s in samples}),
        "meanCoverage": round(statistics.mean(s.qc["meanCoverage"] for s in samples), 2),
    }
    timestamps = sorted(sample.analysis_timestamp for sample in samples)
    return {
        "createDateTime": timestamps[0],
        "dataUseConditions|duoDataUse|id": "",
        "dataUseConditions|duoDataUse|label": "",
        "dataUseConditions|duoDataUse|modifiers": "",
        "dataUseConditions|duoDataUse|version": "",
        "description": "",
        "externalUrl": "",
        "id": "",
        "info": json.dumps(summary),
        "name": "",
        "updateDateTime": timestamps[-1],
        "version": "",
    }


def _cohort_row(samples: list[DragenSample]) -> dict[str, str]:
    size = len(samples)
    genders = Counter(
        SEX_BY_KARYOTYPE.get(s.karyotype, UNKNOWN_SEX)[1] for s in samples
    )
    return {
        "cohortType": "study-defined",
        "cohortSize": str(size),
        "cohortDataTypes|id": WGS_DATA_TYPE[0],
        "cohortDataTypes|label": WGS_DATA_TYPE[1],
        "collectionEvents|eventSize": str(size),
        "collectionEvents|eventGenders|availability": "TRUE",
        "collectionEvents|eventGenders|availabilityCount": str(size),
        "collectionEvents|eventGenders|distribution": json.dumps(
            {"genders": dict(sorted(genders.items()))}
        ),
        "collectionEvents|eventDataTypes|availability": "TRUE",
        "collectionEvents|eventDataTypes|availabilityCount": str(size),
        "collectionEvents|eventDataTypes|distribution": json.dumps(
            {"datatypes": {WGS_DATA_TYPE[0]: size}}
        ),
    }


def _individual_row(sample: DragenSample) -> dict[str, str]:
    sex_id, sex_label = SEX_BY_KARYOTYPE.get(sample.karyotype, UNKNOWN_SEX)
    return {
        "id": sample.sample_id,
        "sex|id": sex_id,
        "sex|label": sex_label,
        "karyotypicSex": sample.karyotype,
    }


def _biosample_row(sample: DragenSample) -> dict[str, str]:
    # biosampleStatus and sampleOriginType are not available from DRAGEN.
    return {"id": sample.sample_id, "individualId": sample.sample_id}


def _run_row(sample: DragenSample) -> dict[str, str]:
    return {
        "id": sample.sample_id,
        "biosampleId": sample.sample_id,
        "individualId": sample.sample_id,
        "libraryLayout": sample.library_layout,
        "libraryStrategy": "WGS",
        "librarySelection": LIBRARY_SELECTION,
        "librarySource|id": LIBRARY_SOURCE[0],
        "librarySource|label": LIBRARY_SOURCE[1],
        "platform": PLATFORM,
        "runDate": sample.analysis_date,
        "info": json.dumps(sample.qc),
    }


def _analysis_row(sample: DragenSample) -> dict[str, str]:
    dragen = f"DRAGEN {sample.dragen_version}"
    return {
        "id": sample.sample_id,
        "runId": sample.sample_id,
        "biosampleId": sample.sample_id,
        "individualId": sample.sample_id,
        "analysisDate": sample.analysis_date,
        "pipelineName": dragen,
        "aligner": dragen,
        "variantCaller": dragen,
        "info": json.dumps({"enabledModules": sample.enabled_modules}),
    }


def _write_csv(path: Path, columns: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=columns)
        writer.writeheader()
        writer.writerows(rows)
    LOGGER.info("Wrote %d rows to %s", len(rows), path)

"""Integration helpers for Beacon RI-tools.

After the switch from subprocess to direct import of beacon2-ri-tools-v2,
the heavy lifting (argparse, schema loading, CLI entry point) is bypassed.
We reuse only `get_hash` to keep `_id` byte-compatible with `csv_to_bff.py`.
"""

from __future__ import annotations

import csv
import dataclasses
import glob
import hashlib
import json
import logging
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import quote_plus

from impact_tools.beacon.config import BeaconMongoConfig


def get_hash(string: str) -> str:
    """Return the same SHA256 hash used by beacon2-ri-tools csv_to_bff.py."""
    return hashlib.sha256(string.encode("utf-8")).hexdigest()


@dataclasses.dataclass
class CsvToBffConfig:
    """Configuration for generating a BFF JSON from a CSV."""

    entity: str
    input_csv: Path
    output_dir: Path
    dataset_id: str
    # Kept for reference; no longer used after the switch to direct import.
    # ri_tools_dir: Path | None = None
    # python_bin: str = "python"


LOGGER = logging.getLogger(__name__)


@dataclasses.dataclass
class GenomicVariationsResult:
    """Result returned by one local RI-tools VCF execution."""

    input_vcf: Path
    processed: int
    inserted: int
    skipped: int
    stdout: str
    stderr: str


def _build_ritools_mongo_uri(config: BeaconMongoConfig) -> str:
    """Build the MongoDB URI consumed by RI-tools."""

    credentials = ""

    if config.user or config.password:
        credentials = (
            f"{quote_plus(str(config.user))}:"
            f"{quote_plus(str(config.password or ''))}@"
        )

    query = [
        f"authSource={quote_plus(str(config.auth_source))}",
        f"serverSelectionTimeoutMS={config.server_timeout_ms}",
        f"connectTimeoutMS={config.connect_timeout_ms}",
        f"socketTimeoutMS={config.socket_timeout_ms}",
    ]

    if config.direct:
        query.append("directConnection=true")

    return (
        f"mongodb://{credentials}"
        f"{config.host}:{config.port}/"
        f"{quote_plus(str(config.database))}?"
        + "&".join(query)
    )


def run_genomic_variations_vcf(
    *,
    mongo: BeaconMongoConfig,
    dataset_id: str,
    input_vcf: Path,
    reference_genome: str,
) -> GenomicVariationsResult:
    """Run genomicVariations_vcf locally against the configured MongoDB."""

    input_vcf = input_vcf.expanduser().resolve()

    if not input_vcf.is_file():
        raise FileNotFoundError(f"VCF file not found: {input_vcf}")

    if reference_genome not in {"GRCh37", "GRCh38"}:
        raise ValueError(
            "reference_genome must be one of: GRCh37, GRCh38"
        )

    environment = os.environ.copy()

    # Prevent external variables from overriding the explicit deployment config.
    for name in (
        "DATABASE_URI",
        "MONGODB_URI",
        "BEACON_MONGO_TLS_CA",
        "BEACON_MONGO_TLS_CERT",
    ):
        environment.pop(name, None)

    environment["DATABASE_URI"] = _build_ritools_mongo_uri(mongo)
    environment["DATABASE_NAME"] = mongo.database

    if mongo.tls:
        if not mongo.tls_ca or not mongo.tls_cert:
            raise ValueError(
                "MongoDB TLS requires both tls_ca and tls_cert."
            )

        environment["BEACON_MONGO_TLS_CA"] = str(mongo.tls_ca)
        environment["BEACON_MONGO_TLS_CERT"] = str(mongo.tls_cert)

    command = [
        sys.executable,
        "-m",
        "genomicVariations_vcf",
        "--datasetId",
        dataset_id,
        "--input",
        glob.escape(str(input_vcf)),
        "--refGenome",
        reference_genome,
    ]

    LOGGER.info(
        "Running local RI-tools: dataset=%s input=%s reference=%s",
        dataset_id,
        input_vcf,
        reference_genome,
    )

    completed = subprocess.run(
        command,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    if completed.stdout:
        LOGGER.info("RI-tools stdout:\n%s", completed.stdout)

    if completed.stderr:
        LOGGER.info("RI-tools stderr:\n%s", completed.stderr)

    if completed.returncode != 0:
        raise RuntimeError(
            "Local RI-tools execution failed.\n"
            f"Dataset ID: {dataset_id}\n"
            f"Input VCF: {input_vcf}\n"
            f"Reference genome: {reference_genome}\n"
            f"Exit code: {completed.returncode}\n"
            f"STDOUT: {completed.stdout}\n"
            f"STDERR: {completed.stderr}"
        )

    inserted_match = re.search(
        r"Successfully inserted\s+(\d+)\s+records into beacon",
        completed.stdout,
    )
    processed_match = re.search(
        r"A total of\s+(\d+)\s+variants were processed",
        completed.stdout,
    )
    skipped_match = re.search(
        r"A total of\s+(\d+)\s+variants were skipped",
        completed.stdout,
    )

    if not inserted_match or not processed_match or not skipped_match:
        raise RuntimeError(
            "Could not parse RI-tools variant counts.\n"
            f"STDOUT: {completed.stdout}\n"
            f"STDERR: {completed.stderr}"
        )

    return GenomicVariationsResult(
        input_vcf=input_vcf,
        inserted=int(inserted_match.group(1)),
        processed=int(processed_match.group(1)),
        skipped=int(skipped_match.group(1)),
        stdout=completed.stdout,
        stderr=completed.stderr,
    )


def run_csv_to_bff(config: CsvToBffConfig) -> Path:
    """Generate the BFF JSON for the configured entity.

    Currently only `entity == 'datasets'` is supported. Replicates the `_id`
    hashing logic of ri-tools' csv_to_bff.py so the output is interchangeable
    with what the upstream CLI would produce.

    Returns
    -------
    Path
        Path to the generated JSON file.
    """
    if config.entity != "datasets":
        raise NotImplementedError(
            f"Entity '{config.entity}' is not supported yet by this helper."
        )

    if not config.input_csv.exists():
        raise FileNotFoundError(f"Input CSV does not exist: {config.input_csv}")

    config.output_dir.mkdir(parents=True, exist_ok=True)
    output_path = config.output_dir / f"{config.entity}.json"

    with config.input_csv.open(newline="") as f:
        rows = list(csv.DictReader(f))

    docs = []
    for row in rows:
        row["_id"] = get_hash(config.dataset_id + row["id"])
        docs.append(row)

    output_path.write_text(json.dumps(docs, indent=2))
    return output_path


def validate_datasets_json(path: Path, dataset_id: str) -> None:
    """Validate that datasets.json exists and contains the expected dataset id."""
    if not path.exists():
        raise FileNotFoundError(f"datasets.json was not created: {path}")

    data = json.loads(path.read_text(encoding="utf-8"))

    if not isinstance(data, list):
        raise ValueError(f"Expected datasets.json to contain a JSON array: {path}")

    ids = {item.get("id") for item in data if isinstance(item, dict)}

    if dataset_id not in ids:
        raise ValueError(
            f"Dataset {dataset_id} was not found in generated datasets.json: {path}"
        )
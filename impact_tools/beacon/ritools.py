"""Integration helpers for Beacon RI-tools.

After the switch from subprocess to direct import of beacon2-ri-tools-v2,
the heavy lifting (argparse, schema loading, CLI entry point) is bypassed.
We reuse only `get_hash` to keep `_id` byte-compatible with `csv_to_bff.py`.
"""

from __future__ import annotations

import csv
import dataclasses
import json
import hashlib
from pathlib import Path

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
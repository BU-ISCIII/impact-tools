"""Remote MongoDB operations used by Beacon ingestion workflows."""

from __future__ import annotations

import json
import logging
import re
import shlex
from pathlib import Path
from urllib.parse import quote_plus

from impact_tools.beacon.remote import (
    BeaconContainersConfig,
    BeaconMongoConfig,
    exec_remote,
    sftp_upload,
)

LOGGER = logging.getLogger(__name__)


def _run_mongosh(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    javascript: str,
) -> str:
    """Execute JavaScript with mongosh inside the remote Mongo container."""
    command_parts = [
        f"podman exec {shlex.quote(containers_cfg.mongo)}",
        "mongosh",
        "--quiet",
        f"-u {shlex.quote(mongo_cfg.user)}",
        f"-p {shlex.quote(mongo_cfg.password)}",
        (
            "--authenticationDatabase "
            f"{shlex.quote(mongo_cfg.auth_source)}"
        ),
    ]

    if mongo_cfg.tls:
        command_parts.extend(
            [
                "--tls",
                f"--tlsCAFile {shlex.quote(mongo_cfg.tls_ca)}",
                (
                    "--tlsCertificateKeyFile "
                    f"{shlex.quote(mongo_cfg.tls_cert)}"
                ),
            ]
        )

        if mongo_cfg.tls_allow_invalid:
            command_parts.append("--tlsAllowInvalidCertificates")

    command_parts.append(
        f"--eval {shlex.quote(javascript)}"
    )

    result = exec_remote(
        client,
        " ".join(command_parts),
    )

    if not result.ok:
        raise RuntimeError(
            "MongoDB command failed.\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )

    return result.stdout.strip()


def mongo_count_dataset(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    dataset_id: str,
) -> int:
    """Count records in MongoDB.datasets with the requested dataset ID."""
    javascript = (
        f"db.getSiblingDB({json.dumps(mongo_cfg.database)})"
        ".datasets"
        f".countDocuments({{id: {json.dumps(dataset_id)}}})"
    )

    output = _run_mongosh(
        client,
        mongo_cfg,
        containers_cfg,
        javascript,
    )

    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(
            "Unexpected output while counting dataset "
            f"{dataset_id!r}: {output!r}"
        ) from exc


def mongo_list_datasets(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
) -> list[dict]:
    """Return the registered dataset IDs and names."""
    javascript = f"""
const database = db.getSiblingDB({json.dumps(mongo_cfg.database)});
const datasets = database.datasets
    .find({{}}, {{_id: 0, id: 1, name: 1}})
    .sort({{id: 1}})
    .toArray();

print(JSON.stringify(datasets));
"""

    output = _run_mongosh(
        client,
        mongo_cfg,
        containers_cfg,
        javascript,
    )

    try:
        datasets = json.loads(output or "[]")
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Unexpected output while listing datasets: "
            f"{output!r}"
        ) from exc

    if not isinstance(datasets, list):
        raise RuntimeError(
            "Unexpected MongoDB response: expected a list."
        )

    return datasets


def mongo_import_datasets(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    local_json: Path,
    remote_tmp: str = "/tmp/datasets.json",
) -> int:
    """Import datasets.json into MongoDB.datasets."""
    local_json = local_json.expanduser().resolve()

    if not local_json.is_file():
        raise FileNotFoundError(
            f"Dataset JSON file not found: {local_json}"
        )

    sftp_upload(
        client,
        local_json,
        remote_tmp,
    )

    copy_command = (
        f"podman cp {shlex.quote(remote_tmp)} "
        f"{shlex.quote(containers_cfg.mongo)}:"
        f"{shlex.quote(remote_tmp)}"
    )

    copy_result = exec_remote(
        client,
        copy_command,
    )

    if not copy_result.ok:
        raise RuntimeError(
            "Could not copy datasets JSON into Mongo container.\n"
            f"STDOUT: {copy_result.stdout}\n"
            f"STDERR: {copy_result.stderr}"
        )

    uri_parameters = [
        f"authSource={quote_plus(mongo_cfg.auth_source)}",
    ]

    if mongo_cfg.tls:
        uri_parameters.extend(
            [
                "tls=true",
                f"tlsCAFile={quote_plus(mongo_cfg.tls_ca)}",
                (
                    "tlsCertificateKeyFile="
                    f"{quote_plus(mongo_cfg.tls_cert)}"
                ),
            ]
        )

    mongo_uri = (
        f"mongodb://{quote_plus(mongo_cfg.user)}:"
        f"{quote_plus(mongo_cfg.password)}"
        f"@127.0.0.1:27017/"
        f"{quote_plus(mongo_cfg.database)}"
        f"?{'&'.join(uri_parameters)}"
    )

    command_parts = [
        f"podman exec {shlex.quote(containers_cfg.mongo)}",
        "mongoimport",
        "--jsonArray",
        f"--uri {shlex.quote(mongo_uri)}",
        f"--file {shlex.quote(remote_tmp)}",
        "--collection datasets",
    ]

    if mongo_cfg.tls and mongo_cfg.tls_allow_invalid:
        command_parts.append("--tlsInsecure")

    result = exec_remote(
        client,
        " ".join(command_parts),
    )

    if not result.ok:
        raise RuntimeError(
            "mongoimport failed.\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )

    match = re.search(
        r"(\d+) document\(s\) imported successfully",
        result.stderr + result.stdout,
    )

    if not match:
        raise RuntimeError(
            "Could not parse mongoimport output.\n"
            f"STDOUT: {result.stdout}\n"
            f"STDERR: {result.stderr}"
        )

    return int(match.group(1))


def mongo_count_variants(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    dataset_id: str,
) -> int:
    """Count genomic variants assigned to a dataset ID."""
    javascript = (
        f"db.getSiblingDB({json.dumps(mongo_cfg.database)})"
        ".genomicVariations"
        f".countDocuments({{datasetId: {json.dumps(dataset_id)}}})"
    )

    output = _run_mongosh(
        client,
        mongo_cfg,
        containers_cfg,
        javascript,
    )

    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(
            "Unexpected output while counting variants for "
            f"{dataset_id!r}: {output!r}"
        ) from exc


def mongo_rename_dataset_id(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    *,
    old_dataset_id: str,
    new_dataset_id: str,
) -> int:
    """Replace datasetId in all matching genomic variation documents."""
    javascript = f"""
const database = db.getSiblingDB({json.dumps(mongo_cfg.database)});
const result = database.genomicVariations.updateMany(
    {{datasetId: {json.dumps(old_dataset_id)}}},
    {{$set: {{datasetId: {json.dumps(new_dataset_id)}}}}}
);

print(result.modifiedCount);
"""

    output = _run_mongosh(
        client,
        mongo_cfg,
        containers_cfg,
        javascript,
    )

    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(
            "Unexpected output while renaming datasetId "
            f"{old_dataset_id!r} to {new_dataset_id!r}: "
            f"{output!r}"
        ) from exc


def mongo_delete_dataset_variants(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    dataset_id: str,
) -> int:
    """Delete genomic variation documents assigned to a dataset ID."""
    javascript = f"""
const database = db.getSiblingDB({json.dumps(mongo_cfg.database)});
const result = database.genomicVariations.deleteMany(
    {{datasetId: {json.dumps(dataset_id)}}}
);

print(result.deletedCount);
"""

    output = _run_mongosh(
        client,
        mongo_cfg,
        containers_cfg,
        javascript,
    )

    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(
            "Unexpected output while deleting variants for "
            f"{dataset_id!r}: {output!r}"
        ) from exc
"""MongoDB helpers for Beacon ingest workflows. """

from __future__ import annotations

import logging
import re
from pathlib import Path
from urllib.parse import quote_plus

from impact_tools.beacon.remote import (
    BeaconContainersConfig,
    BeaconMongoConfig,
    exec_remote,
    sftp_upload,
)

LOGGER = logging.getLogger(__name__)


def mongo_count_dataset(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    dataset_id: str,
) -> int:
    """Count documents in MongoDB.datasets with the given id.

    Returns the number of matching documents (0 if not present, 1 if present).
    Useful for verifying that an ingest succeeded before declaring the
    dataset registered.
    """
    eval_js = (
        f'db.getSiblingDB("{mongo_cfg.database}").datasets'
        f'.countDocuments({{id: "{dataset_id}"}})'
    )

    command_parts = [
        f"podman exec {containers_cfg.mongo}",
        "mongosh",
        "--quiet",
        f"-u {mongo_cfg.user}",
        f"-p {mongo_cfg.password}",
        f"--authenticationDatabase {mongo_cfg.auth_source}",
    ]

    if mongo_cfg.tls:
        command_parts.extend([
            "--tls",
            f"--tlsCAFile {mongo_cfg.tls_ca}",
            f"--tlsCertificateKeyFile {mongo_cfg.tls_cert}",
        ])
        if mongo_cfg.tls_allow_invalid:
            command_parts.append("--tlsAllowInvalidCertificates")

    command_parts.append(f"--eval '{eval_js}'")
    command = " ".join(command_parts)

    result = exec_remote(client, command)

    if not result.ok:
        raise RuntimeError(
            f"mongo_count_dataset failed for {dataset_id}.\n"
            f"Command: {result.command}\n"
            f"STDERR: {result.stderr}"
        )

    output = result.stdout.strip()
    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(
            f"Unexpected output from mongosh (not an integer): {output!r}"
        ) from exc


def mongo_count_variants(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    dataset_id: str,
) -> int:
    """Count documents in MongoDB.genomicVariations with the given datasetId.

    Used in the verify-and-swap flow:
    - After ingesting variants under a staging_id, check the count matches
      what we expect from the VCF.
    - After the swap, confirm the count for the active dataset_id is correct.
    """
    eval_js = (
        f'db.getSiblingDB("{mongo_cfg.database}").genomicVariations'
        f'.countDocuments({{datasetId: "{dataset_id}"}})'
    )

    command_parts = [
        f"podman exec {containers_cfg.mongo}",
        "mongosh",
        "--quiet",
        f"-u {mongo_cfg.user}",
        f"-p {mongo_cfg.password}",
        f"--authenticationDatabase {mongo_cfg.auth_source}",
    ]

    if mongo_cfg.tls:
        command_parts.extend([
            "--tls",
            f"--tlsCAFile {mongo_cfg.tls_ca}",
            f"--tlsCertificateKeyFile {mongo_cfg.tls_cert}",
        ])
        if mongo_cfg.tls_allow_invalid:
            command_parts.append("--tlsAllowInvalidCertificates")

    command_parts.append(f"--eval '{eval_js}'")
    command = " ".join(command_parts)

    result = exec_remote(client, command)

    if not result.ok:
        raise RuntimeError(
            f"mongo_count_variants failed for {dataset_id}.\n"
            f"Command: {result.command}\n"
            f"STDERR: {result.stderr}"
        )

    output = result.stdout.strip()
    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(
            f"Unexpected output from mongosh (not an integer): {output!r}"
        ) from exc
    

def mongo_rename_dataset_id(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    *,
    old_dataset_id: str,
    new_dataset_id: str,
) -> int:
    """Rename datasetId in MongoDB.genomicVariations.

    Used during the verify-and-swap flow:
    - active dataset_id -> old_id
    - staging_id -> active dataset_id

    Returns the number of modified documents.
    """
    eval_js = (
        f'db.getSiblingDB("{mongo_cfg.database}").genomicVariations'
        ".updateMany("
        f'{{datasetId: "{old_dataset_id}"}}, '
        f'{{$set: {{datasetId: "{new_dataset_id}"}}}}'
        ").modifiedCount"
    )

    command_parts = [
        f"podman exec {containers_cfg.mongo}",
        "mongosh",
        "--quiet",
        f"-u {mongo_cfg.user}",
        f"-p {mongo_cfg.password}",
        f"--authenticationDatabase {mongo_cfg.auth_source}",
    ]

    if mongo_cfg.tls:
        command_parts.extend([
            "--tls",
            f"--tlsCAFile {mongo_cfg.tls_ca}",
            f"--tlsCertificateKeyFile {mongo_cfg.tls_cert}",
        ])
        if mongo_cfg.tls_allow_invalid:
            command_parts.append("--tlsAllowInvalidCertificates")

    command_parts.append(f"--eval '{eval_js}'")
    command = " ".join(command_parts)

    result = exec_remote(client, command)

    if not result.ok:
        raise RuntimeError(
            "mongo_rename_dataset_id failed.\n"
            f"Old dataset ID: {old_dataset_id}\n"
            f"New dataset ID: {new_dataset_id}\n"
            f"Command: {result.command}\n"
            f"STDERR: {result.stderr}"
        )

    output = result.stdout.strip()

    try:
        modified_count = int(output)
    except ValueError as exc:
        raise RuntimeError(
            f"Unexpected output from mongosh (not an integer): {output!r}"
        ) from exc

    LOGGER.info(
        "Renamed MongoDB genomicVariations datasetId: %s -> %s (%d documents)",
        old_dataset_id,
        new_dataset_id,
        modified_count,
    )

    return modified_count


def mongo_delete_dataset_variants(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    dataset_id: str,
) -> int:
    """Delete genomicVariations documents for a datasetId.

    Used as optional cleanup after a successful verify-and-swap flow,
    typically to remove the old_id backup dataset.

    Returns the number of deleted documents.
    """
    eval_js = (
        f'db.getSiblingDB("{mongo_cfg.database}").genomicVariations'
        f'.deleteMany({{datasetId: "{dataset_id}"}}).deletedCount'
    )

    command_parts = [
        f"podman exec {containers_cfg.mongo}",
        "mongosh",
        "--quiet",
        f"-u {mongo_cfg.user}",
        f"-p {mongo_cfg.password}",
        f"--authenticationDatabase {mongo_cfg.auth_source}",
    ]

    if mongo_cfg.tls:
        command_parts.extend([
            "--tls",
            f"--tlsCAFile {mongo_cfg.tls_ca}",
            f"--tlsCertificateKeyFile {mongo_cfg.tls_cert}",
        ])
        if mongo_cfg.tls_allow_invalid:
            command_parts.append("--tlsAllowInvalidCertificates")

    command_parts.append(f"--eval '{eval_js}'")
    command = " ".join(command_parts)

    result = exec_remote(client, command)

    if not result.ok:
        raise RuntimeError(
            f"mongo_delete_dataset_variants failed for {dataset_id}.\n"
            f"Command: {result.command}\n"
            f"STDERR: {result.stderr}"
        )

    output = result.stdout.strip()

    try:
        deleted_count = int(output)
    except ValueError as exc:
        raise RuntimeError(
            f"Unexpected output from mongosh (not an integer): {output!r}"
        ) from exc

    LOGGER.info(
        "Deleted MongoDB genomicVariations for datasetId %s: %d documents",
        dataset_id,
        deleted_count,
    )

    return deleted_count


def mongo_import_datasets(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    local_json: Path,
    remote_tmp: str = "/tmp/datasets.json",
) -> int:
    """Import a datasets.json into MongoDB.<database>.datasets.

    The JSON is uploaded to the VM, copied into the MongoDB container,
    and ingested with `mongoimport --jsonArray`. Connection uses the URI
    form (TLS options embedded) because mongoimport does not accept TLS
    flags as separate arguments.

    Returns the number of documents reported as imported by mongoimport.
    """
    # 1. SFTP upload to the VM host filesystem
    sftp_upload(client, local_json, remote_tmp)

    # 2. Copy from VM host into the Mongo container
    cp_cmd = (
        f"podman cp {remote_tmp} "
        f"{containers_cfg.mongo}:{remote_tmp}"
    )
    cp_result = exec_remote(client, cp_cmd)
    if not cp_result.ok:
        raise RuntimeError(
            f"podman cp failed: {cp_result.stderr or cp_result.stdout}"
        )

    # 3. Build the mongoimport URI with TLS embedded
    uri_params = [f"authSource={mongo_cfg.auth_source}"]
    if mongo_cfg.tls:
        uri_params.extend([
            "tls=true",
            f"tlsCAFile={mongo_cfg.tls_ca}",
            f"tlsCertificateKeyFile={mongo_cfg.tls_cert}",
        ])
    uri = (
        f"mongodb://{quote_plus(mongo_cfg.user)}:{quote_plus(mongo_cfg.password)}"
        f"@127.0.0.1:27017/{mongo_cfg.database}?{'&'.join(uri_params)}"
    )

    # 4. Run mongoimport
    import_parts = [
        f"podman exec {containers_cfg.mongo}",
        "mongoimport",
        "--jsonArray",
        f'--uri "{uri}"',
    ]
    if mongo_cfg.tls and mongo_cfg.tls_allow_invalid:
        import_parts.append("--tlsInsecure")
    import_parts.extend([
        f"--file {remote_tmp}",
        "--collection datasets",
    ])
    import_cmd = " ".join(import_parts)

    result = exec_remote(client, import_cmd)

    if not result.ok:
        raise RuntimeError(
            f"mongoimport failed: {result.stderr or result.stdout}"
        )

    # 5. Parse the mongoimport summary line, e.g.:
    #    "N document(s) imported successfully. M document(s) failed to import."
    match = re.search(
        r"(\d+) document\(s\) imported successfully",
        result.stderr + result.stdout,
    )
    if not match:
        raise RuntimeError(
            f"Could not parse mongoimport output:\n{result.stdout}\n{result.stderr}"
        )

    imported = int(match.group(1))
    return imported
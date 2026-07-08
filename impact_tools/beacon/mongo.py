"""Direct MongoDB operations used by Beacon ingestion workflows."""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator, Literal

from pymongo import MongoClient, UpdateOne
from pymongo.database import Database

from impact_tools.beacon.config import BeaconMongoConfig


LOGGER = logging.getLogger(__name__)
MongoApplyStatus = Literal["created", "updated", "unchanged"]

@contextmanager
def managed_mongo(
    config: BeaconMongoConfig,
) -> Iterator[Database]:
    """Open, validate and close a direct MongoDB connection."""

    options: dict[str, Any] = {
        "username": config.user,
        "password": config.password,
        "authSource": config.auth_source,
        "directConnection": config.direct,
        "serverSelectionTimeoutMS": config.server_timeout_ms,
        "connectTimeoutMS": config.connect_timeout_ms,
        "socketTimeoutMS": config.socket_timeout_ms,
        "appname": "impact-tools",
        "tls": config.tls,
    }

    if config.tls:
        if config.tls_ca:
            options["tlsCAFile"] = config.tls_ca

        if config.tls_cert:
            options["tlsCertificateKeyFile"] = config.tls_cert

        if config.tls_allow_invalid:
            options["tlsAllowInvalidCertificates"] = True

    client = MongoClient(
        host=config.host,
        port=config.port,
        **options,
    )

    try:
        client.admin.command("ping")

        LOGGER.debug(
            "Connected to MongoDB at %s:%d/%s",
            config.host,
            config.port,
            config.database,
        )

        yield client[config.database]

    finally:
        client.close()


def mongo_ping(
    database: Database,
) -> dict[str, Any]:
    """Run a MongoDB ping command."""

    return database.command("ping")


def mongo_count_dataset(
    database: Database,
    dataset_id: str,
) -> int:
    """Count dataset documents matching the requested dataset ID."""

    return database.datasets.count_documents(
        {"id": dataset_id}
    )


def mongo_list_datasets(
    database: Database,
) -> list[dict[str, Any]]:
    """Return registered dataset IDs and names."""

    cursor = database.datasets.find(
        {},
        {
            "_id": 0,
            "id": 1,
            "name": 1,
        },
    ).sort("id", 1)

    return list(cursor)


def mongo_import_datasets(
    database: Database,
    local_json: Path,
) -> int:
    """Insert missing documents from datasets.json.

    Documents already present with the same ``_id`` are preserved. The
    returned value is the number of newly inserted documents.
    """

    local_json = local_json.expanduser().resolve()

    if not local_json.is_file():
        raise FileNotFoundError(
            f"Dataset JSON file not found: {local_json}"
        )

    try:
        documents = json.loads(
            local_json.read_text(encoding="utf-8")
        )
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON file: {local_json}"
        ) from exc

    if not isinstance(documents, list):
        raise ValueError(
            f"Expected a JSON array in {local_json}."
        )

    operations = []

    for index, document in enumerate(documents, start=1):
        if not isinstance(document, dict):
            raise ValueError(
                f"Dataset entry {index} is not a JSON object."
            )

        document_id = document.get("_id")

        if not isinstance(document_id, str) or not document_id:
            raise ValueError(
                f"Dataset entry {index} does not contain a valid _id."
            )

        dataset_id = document.get("id")

        if not isinstance(dataset_id, str) or not dataset_id:
            raise ValueError(
                f"Dataset entry {index} does not contain a valid id."
            )

        operations.append(
            UpdateOne(
                {"_id": document_id},
                {"$setOnInsert": document},
                upsert=True,
            )
        )

    if not operations:
        return 0

    result = database.datasets.bulk_write(
        operations,
        ordered=True,
    )

    return result.upserted_count


def mongo_count_variants(
    database: Database,
    dataset_id: str,
) -> int:
    """Count genomic variants assigned to a dataset ID."""

    return database.genomicVariations.count_documents(
        {"datasetId": dataset_id}
    )


def mongo_rename_dataset_id(
    database: Database,
    *,
    old_dataset_id: str,
    new_dataset_id: str,
) -> int:
    """Replace datasetId in all matching genomic variation documents."""

    result = database.genomicVariations.update_many(
        {
            "datasetId": old_dataset_id,
        },
        {
            "$set": {
                "datasetId": new_dataset_id,
            }
        },
    )

    return result.modified_count


def mongo_reindex(database: Database) -> None:
    """Recreate Beacon indexes and ensure required collections exist.

    Mirrors beacon.connections.mongo.reindex but runs locally via PyMongo
    so no SSH or container exec is needed.
    """
    existing = set(database.list_collection_names())

    for name in ("synonyms", "targets", "caseLevelData", "similarities"):
        if name not in existing:
            database.create_collection(name)
            LOGGER.debug("Created collection: %s", name)

    # counts is always dropped and recreated (matches upstream reindex.py).
    database.drop_collection("counts")
    database.create_collection("counts")

    gv = database.genomicVariations
    gv.create_index([
        ("variation.location.interval.start.value", 1),
        ("variation.location.interval.end.value", 1),
    ])
    gv.create_index([("length", 1)])
    gv.create_index([
        ("variation.alternateBases", 1),
        ("variation.referenceBases", 1),
        ("variation.location.interval.start.value", 1),
        ("variation.location.interval.end.value", 1),
    ])
    gv.create_index([("datasetId", 1)])
    gv.create_index([("variation.location.interval.end.value", 1)])
    gv.create_index([("identifiers.genomicHGVSId", 1)])
    gv.create_index([
        ("molecularAttributes.geneIds", 1),
        ("variation.variantType", 1),
    ])

    cld = database.caseLevelData
    cld.create_index([("id", 1), ("datasetId", 1)])
    cld.create_index([("datasetId", 1)])

    LOGGER.info("Beacon reindex completed.")


def mongo_delete_dataset_variants(
    database: Database,
    dataset_id: str,
) -> int:
    """Delete genomic variation documents assigned to a dataset ID."""

    result = database.genomicVariations.delete_many(
        {
            "datasetId": dataset_id,
        }
    )

    return result.deleted_count


def mongo_list_old_backups(
    database: Database,
    dataset_id: str,
) -> list[dict[str, Any]]:
    """List backups matching ``<dataset_id>_old_<timestamp>``."""

    pattern = (
        rf"^{re.escape(dataset_id)}"
        rf"_old_[0-9]{{8}}_[0-9]{{6}}$"
    )

    backup_ids = database.genomicVariations.distinct(
        "datasetId",
        {
            "datasetId": {
                "$regex": pattern,
            }
        },
    )

    rows = []

    for backup_id in sorted(backup_ids):
        if not isinstance(backup_id, str):
            continue

        rows.append(
            {
                "datasetId": backup_id,
                "variants": (
                    database.genomicVariations.count_documents(
                        {"datasetId": backup_id}
                    )
                ),
            }
        )

    return rows


def _validate_dataset_permission_level(level: str) -> None:
    """Validate supported Beacon dataset permission levels."""

    allowed = {"public", "registered", "controlled"}

    if level not in allowed:
        raise ValueError(
            f"permissions level must be one of: {', '.join(sorted(allowed))}"
        )


def _validate_dataset_granularity(granularity: str) -> None:
    """Validate supported Beacon response granularities."""

    allowed = {"boolean", "count", "record"}

    if granularity not in allowed:
        raise ValueError(
            f"granularity must be one of: {', '.join(sorted(allowed))}"
        )


def _normalise_controlled_user_list(
    user_list: list[dict[str, Any]] | None,
    *,
    default_granularity: str,
) -> list[dict[str, Any]]:
    """Validate and normalise controlled user-list entries.

    The output preserves the Beacon permissions YAML-compatible shape:

    controlled:
      user-list:
        - user_e-mail: jane.smith@beacon.ga4gh
          default_entry_types_granularity: record
    """

    _validate_dataset_granularity(default_granularity)

    if user_list is None:
        return []

    normalised: list[dict[str, Any]] = []

    for index, user in enumerate(user_list, start=1):
        if not isinstance(user, dict):
            raise ValueError(
                f"controlled user-list entry {index} must be an object."
            )

        email = user.get("user_e-mail")

        if not isinstance(email, str) or not email.strip():
            raise ValueError(
                f"controlled user-list entry {index} requires user_e-mail."
            )

        granularity = user.get(
            "default_entry_types_granularity",
            default_granularity,
        )

        if not isinstance(granularity, str):
            raise ValueError(
                f"controlled user-list entry {index} has invalid "
                "default_entry_types_granularity."
            )

        _validate_dataset_granularity(granularity)

        normalised.append(
            {
                "user_e-mail": email.strip(),
                "default_entry_types_granularity": granularity,
            }
        )

    return normalised


def mongo_set_dataset_flags(
    database: Database,
    dataset_id: str,
    *,
    is_test: bool,
    is_synthetic: bool | None = None,
) -> MongoApplyStatus:
    """Set dataset conf flags in db.datasetsConf.

    Returns "created" if the conf document did not exist, "updated" if it
    existed and changed, "unchanged" if it was already identical.
    """

    document: dict[str, Any] = {
        "_id": dataset_id,
        "isTest": bool(is_test),
    }

    if is_synthetic is not None:
        document["isSynthetic"] = bool(is_synthetic)

    result = database.datasetsConf.replace_one(
        {"_id": dataset_id},
        document,
        upsert=True,
    )

    if result.upserted_id is not None:
        return "created"

    if result.modified_count > 0:
        return "updated"

    if result.matched_count > 0:
        return "unchanged"

    raise RuntimeError(
        f"Unexpected MongoDB result while setting flags for {dataset_id}"
    )


def mongo_set_dataset_permissions(
    database: Database,
    dataset_id: str,
    *,
    level: str,
    granularity: str = "record",
    user_list: list[dict[str, Any]] | None = None,
) -> MongoApplyStatus:
    """Set dataset permissions in db.datasetsPermissions."""

    _validate_dataset_permission_level(level)
    _validate_dataset_granularity(granularity)

    if level == "controlled":
        permission_block: dict[str, Any] = {
            "user-list": _normalise_controlled_user_list(
                user_list,
                default_granularity=granularity,
            )
        }
    else:
        if user_list:
            raise ValueError(
                "user_list is only valid for controlled permissions."
            )

        permission_block = {
            "default_entry_types_granularity": granularity,
        }

    document = {
        "_id": dataset_id,
        "permissions": {
            level: permission_block,
        },
    }

    result = database.datasetsPermissions.replace_one(
        {"_id": dataset_id},
        document,
        upsert=True,
    )

    if result.upserted_id is not None:
        return "created"

    if result.modified_count > 0:
        return "updated"

    if result.matched_count > 0:
        return "unchanged"

    raise RuntimeError(
        f"Unexpected MongoDB result while setting permissions for {dataset_id}"
    )


def mongo_get_dataset_permissions(
    database: Database,
    dataset_id: str,
) -> dict[str, Any] | None:
    """Return the dataset permission document from db.datasetsPermissions."""

    return database.datasetsPermissions.find_one(
        {"_id": dataset_id},
        {"_id": 0},
    )


def mongo_delete_dataset_permissions(
    database: Database,
    dataset_id: str,
) -> int:
    """Delete one dataset permission document from db.datasetsPermissions."""

    result = database.datasetsPermissions.delete_one(
        {"_id": dataset_id}
    )

    return result.deleted_count
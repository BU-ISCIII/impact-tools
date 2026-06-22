"""Direct MongoDB operations used by Beacon ingestion workflows."""

from __future__ import annotations

import json
import logging
import re
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from pymongo import MongoClient, UpdateOne
from pymongo.database import Database

from impact_tools.beacon.config import BeaconMongoConfig


LOGGER = logging.getLogger(__name__)


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
"""Shared helpers for the Beacon tests.

Tests that use MongoDB run against the real MongoDB configured for
impact-tools (beacon.mongo.* and IMPACT_TOOLS_BEACON_MONGO_PASSWORD), in a
separate throw-away database named by IMPACT_TOOLS_TEST_MONGO_DB. That
database is dropped before and after every test, and the production
database name is refused. Without the variable those tests are skipped.

The Beacon API, RI-tools and the local registry are stubbed.
"""

from __future__ import annotations

import dataclasses
import logging
import os
import tempfile
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from impact_tools.beacon import ingest, mongo
from impact_tools.beacon.config import build_beacon_deployment_config
from impact_tools.beacon.registry import BeaconRegistry


VCF_HEADER = "##fileformat=VCFv4.2\n#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"


def write_vcf(path: Path, records: int) -> Path:
    path.write_text(
        VCF_HEADER + "chr1\t100\t.\tA\tG\t.\tPASS\tAF=0.1\n" * records,
        encoding="utf-8",
    )
    return path


TEST_DB_ENV = "IMPACT_TOOLS_TEST_MONGO_DB"

requires_mongo = unittest.skipUnless(
    os.environ.get(TEST_DB_ENV),
    f"set {TEST_DB_ENV} to a throw-away database name to run the MongoDB tests",
)


def mongo_test_config():
    """impact-tools MongoDB config pointed at the throw-away test database."""

    name = os.environ[TEST_DB_ENV]
    production = build_beacon_deployment_config().mongo

    if name == production.database:
        raise RuntimeError(
            f"{TEST_DB_ENV}={name!r} is the production Beacon database; "
            "use a different, throw-away name."
        )

    return dataclasses.replace(production, database=name)


@requires_mongo
class MongoTestCase(unittest.TestCase):
    """Gives each test an empty test database on the real MongoDB."""

    def setUp(self) -> None:
        config = mongo_test_config()
        self.deployment = SimpleNamespace(mongo=config, api=None)

        connection = mongo.managed_mongo(config)
        self.db = connection.__enter__()
        self.addCleanup(connection.__exit__, None, None, None)

        self.db.client.drop_database(config.database)
        self.addCleanup(self.db.client.drop_database, config.database)


class BeaconTestCase(MongoTestCase):
    """Test database plus a stub Beacon API and a temporary registry.

    The real API reads the production database, not the test one, so its
    answers are stubbed.
    """

    api_lists_dataset = True

    def setUp(self) -> None:
        super().setUp()
        self.tmp = Path(tempfile.mkdtemp())
        self.registry_path = self.tmp / "registry.sqlite3"

        patches = [
            patch("impact_tools.beacon.api.managed_beacon_api", lambda cfg: nullcontext(None)),
            patch(
                "impact_tools.beacon.api.verify_dataset_via_api",
                lambda client, dataset_id: self.api_lists_dataset,
            ),
            patch(
                "impact_tools.beacon.api.is_dataset_listed",
                lambda client, dataset_id: self.api_lists_dataset,
            ),
            patch.object(ingest, "BeaconRegistry", lambda: BeaconRegistry(self.registry_path)),
        ]
        for p in patches:
            p.start()
            self.addCleanup(p.stop)

    def tearDown(self) -> None:
        logging.shutdown()

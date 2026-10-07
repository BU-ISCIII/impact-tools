"""Tests for `beacon delete dataset`."""

from __future__ import annotations

import io
import unittest
from unittest.mock import patch

from click.testing import CliRunner

from beacon_testutils import BeaconTestCase
from impact_tools.__main__ import cli
from impact_tools.beacon import ingest, mongo
from impact_tools.beacon.registry import BeaconRegistry

class DeleteDatasetTests(BeaconTestCase):
    api_lists_dataset = False

    def setUp(self) -> None:
        super().setUp()
        self.db.datasets.insert_many([{"_id": "h1", "id": "DS1"}, {"_id": "h2", "id": "DS10"}])
        self.db.datasetsConf.insert_many([{"_id": "DS1"}, {"_id": "DS10"}])
        self.db.datasetsPermissions.insert_many([{"_id": "DS1"}, {"_id": "DS10"}])
        self.db.filtering_terms.insert_one({"id": "DUO:0000042"})
        for dataset_id, count in [
            ("DS1", 5),
            ("DS1_old_20261007_120000", 4),
            ("DS1_staging_20261007_130000", 3),
            ("DS10", 6),
            ("DS1x", 1),
            ("DS1_old_bad", 2),
        ]:
            self.db.genomicVariations.insert_many([{"datasetId": dataset_id} for _ in range(count)])

        with BeaconRegistry(self.registry_path) as registry:
            registry.record_dataset_registration(
                dataset_id="DS1", name="n", description="", reference_genome="GRCh38",
                is_test=False, is_synthetic=False, granularity="record", base_dir=".",
            )

    def remaining_variant_ids(self):
        return sorted(self.db.genomicVariations.distinct("datasetId"))

    def run_cli(self, *args, stdin=None):
        with patch("impact_tools.__main__.build_beacon_deployment_config", lambda cfg: self.deployment):
            return CliRunner().invoke(cli, ["beacon", "delete", "dataset", *args], input=stdin)

    def test_footprint_counts_dataset_and_its_backups_only(self) -> None:
        footprint = mongo.mongo_dataset_footprint(self.db, "DS1")
        self.assertEqual(footprint["genomicVariations"], 12)
        self.assertEqual(footprint["datasets"], 1)

    def test_delete_leaves_other_datasets_and_shared_terms(self) -> None:
        ingest.delete_dataset("DS1", self.deployment)
        self.assertEqual(self.remaining_variant_ids(), ["DS10", "DS1_old_bad", "DS1x"])
        self.assertEqual(self.db.datasets.distinct("id"), ["DS10"])
        self.assertIsNone(self.db.datasetsConf.find_one({"_id": "DS1"}))
        self.assertIsNone(self.db.datasetsPermissions.find_one({"_id": "DS1"}))
        self.assertEqual(self.db.filtering_terms.count_documents({}), 1)

    def test_delete_removes_registry_entry(self) -> None:
        ingest.delete_dataset("DS1", self.deployment)
        with BeaconRegistry(self.registry_path) as registry:
            self.assertIsNone(registry.find_dataset_registration("DS1"))

    def test_cli_dry_run_deletes_nothing(self) -> None:
        result = self.run_cli("--dataset-id", "DS1", "--dry-run")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.db.genomicVariations.count_documents({}), 21)

    def test_cli_unknown_dataset_fails(self) -> None:
        result = self.run_cli("--dataset-id", "NOPE", "--yes")
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("Nothing found", result.output)

    def test_cli_requires_yes_without_terminal(self) -> None:
        result = self.run_cli("--dataset-id", "DS1")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.db.genomicVariations.count_documents({}), 21)

    def test_cli_wrong_confirmation_deletes_nothing(self) -> None:
        tty = type("TTY", (io.StringIO,), {"isatty": lambda self: True})
        with patch("impact_tools.__main__.click.get_text_stream", lambda name: tty()):
            result = self.run_cli("--dataset-id", "DS1", stdin="DS10\n")
        self.assertNotEqual(result.exit_code, 0)
        self.assertEqual(self.db.genomicVariations.count_documents({}), 21)

    def test_cli_with_yes_deletes(self) -> None:
        result = self.run_cli("--dataset-id", "DS1", "--yes")
        self.assertEqual(result.exit_code, 0, result.output)
        self.assertEqual(self.remaining_variant_ids(), ["DS10", "DS1_old_bad", "DS1x"])


if __name__ == "__main__":
    unittest.main()

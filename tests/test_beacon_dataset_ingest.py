"""Tests for `beacon ingest dataset`: flags, metadata updates and permissions."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from click.testing import CliRunner

from beacon_testutils import BeaconTestCase, MongoTestCase
from impact_tools.__main__ import cli
from impact_tools.beacon import ingest, mongo
from impact_tools.beacon.registry import BeaconRegistry

class DatasetFlagsTests(MongoTestCase):
    def flags(self):
        return self.db.datasetsConf.find_one({"_id": "DS1"}, {"_id": 0})

    def test_new_dataset_defaults_to_false(self) -> None:
        status = mongo.mongo_set_dataset_flags(self.db, "DS1", is_test=None, is_synthetic=None)
        self.assertEqual(status, "created")
        self.assertEqual(self.flags(), {"isTest": False, "isSynthetic": False})

    def test_flags_not_given_are_kept(self) -> None:
        mongo.mongo_set_dataset_flags(self.db, "DS1", is_test=True, is_synthetic=True)
        status = mongo.mongo_set_dataset_flags(self.db, "DS1", is_test=None, is_synthetic=None)
        self.assertEqual(status, "unchanged")
        self.assertEqual(self.flags(), {"isTest": True, "isSynthetic": True})

    def test_only_given_flag_changes(self) -> None:
        mongo.mongo_set_dataset_flags(self.db, "DS1", is_test=True, is_synthetic=True)
        status = mongo.mongo_set_dataset_flags(self.db, "DS1", is_test=False, is_synthetic=None)
        self.assertEqual(status, "updated")
        self.assertEqual(self.flags(), {"isTest": False, "isSynthetic": True})

    def test_values_are_stored_as_booleans(self) -> None:
        mongo.mongo_set_dataset_flags(self.db, "DS1", is_test=1, is_synthetic=None)
        self.assertIs(self.flags()["isTest"], True)


class DatasetImportTests(MongoTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.json_path = Path(tempfile.mkdtemp()) / "datasets.json"

    def import_doc(self, **fields) -> int:
        document = {"_id": "hash-DS1", "id": "DS1", "name": "Name", **fields}
        self.json_path.write_text(json.dumps([document]), encoding="utf-8")
        return mongo.mongo_import_datasets(self.db, self.json_path)

    def stored(self):
        return self.db.datasets.find_one({"id": "DS1"}, {"_id": 0})

    def test_new_dataset_is_inserted(self) -> None:
        self.assertEqual(self.import_doc(description="first"), 1)
        self.assertEqual(self.stored()["description"], "first")

    def test_rerun_updates_name_and_description(self) -> None:
        self.import_doc(description="first")
        self.assertEqual(self.import_doc(name="New name", description="second"), 0)
        self.assertEqual(self.stored()["name"], "New name")
        self.assertEqual(self.stored()["description"], "second")
        self.assertEqual(self.db.datasets.count_documents({}), 1)

    def test_empty_description_keeps_stored_value(self) -> None:
        self.import_doc(description="first")
        self.import_doc(description="")
        self.assertEqual(self.stored()["description"], "first")

    def test_duo_is_replaced(self) -> None:
        old = {"duoDataUse": [{"id": "DUO:0000042", "label": "a", "version": "v"}]}
        new = {"duoDataUse": [{"id": "DUO:0000019", "label": "b", "version": "v"}]}
        self.import_doc(dataUseConditions=old)
        self.import_doc(dataUseConditions=new)
        self.assertEqual(self.stored()["dataUseConditions"], new)

    def test_missing_id_is_rejected(self) -> None:
        self.json_path.write_text(json.dumps([{"_id": "x", "name": "n"}]), encoding="utf-8")
        with self.assertRaises(ValueError):
            mongo.mongo_import_datasets(self.db, self.json_path)


class ApplyDatasetTests(BeaconTestCase):
    def apply(self, **kwargs):
        kwargs.setdefault("name", "Test dataset")
        kwargs.setdefault("description", "")
        config = ingest.DatasetIngestConfig(dataset_id="DS1", base_dir=self.tmp, **kwargs)
        prepared = ingest.prepare_dataset_artifacts(config)
        return ingest.apply_dataset_to_remote(config, prepared.paths, self.deployment)

    def permissions(self):
        return self.db.datasetsPermissions.find_one({"_id": "DS1"})["permissions"]

    def users(self):
        return {
            user["user_e-mail"]: user["default_entry_types_granularity"]
            for user in self.permissions()["controlled"]["user-list"]
        }

    def test_new_dataset_is_public(self) -> None:
        self.apply()
        self.assertEqual(
            self.permissions(),
            {"public": {"default_entry_types_granularity": "record"}},
        )

    def test_rerun_without_set_permissions_keeps_controlled(self) -> None:
        self.apply(permissions_level="controlled", permissions_email="a@x")
        self.apply()
        self.assertEqual(self.users(), {"a@x": "record"})

    def test_set_email_keeps_existing_users(self) -> None:
        self.apply(permissions_level="controlled", permissions_email="a@x")
        self.db.datasetsPermissions.update_one(
            {"_id": "DS1"},
            {"$push": {"permissions.controlled.user-list": {
                "user_e-mail": "adminui@x",
                "default_entry_types_granularity": "count",
            }}},
        )
        self.apply(permissions_level="controlled", permissions_email="b@x")
        self.assertEqual(
            self.users(),
            {"a@x": "record", "adminui@x": "count", "b@x": "record"},
        )

    def test_set_email_again_updates_granularity_without_duplicates(self) -> None:
        self.apply(permissions_level="controlled", permissions_email="a@x")
        self.apply(permissions_level="controlled", permissions_email="a@x", granularity="count")
        users = self.permissions()["controlled"]["user-list"]
        self.assertEqual(len(users), 1)
        self.assertEqual(self.users(), {"a@x": "count"})

    def test_explicit_level_change_replaces_permissions(self) -> None:
        self.apply(permissions_level="controlled", permissions_email="a@x")
        self.apply(permissions_level="registered")
        self.assertEqual(list(self.permissions()), ["registered"])

    def test_rerun_keeps_is_test(self) -> None:
        self.apply(is_test=True)
        self.apply()
        conf = self.db.datasetsConf.find_one({"_id": "DS1"})
        self.assertIs(conf["isTest"], True)

    def test_registry_records_stored_flags(self) -> None:
        self.apply(is_test=True)
        self.apply()
        with BeaconRegistry(self.registry_path) as registry:
            self.assertTrue(registry.find_dataset_registration("DS1").is_test)

    def test_rerun_updates_name_and_keeps_description(self) -> None:
        self.apply(description="first")
        self.apply(name="Renamed", description="")
        doc = self.db.datasets.find_one({"id": "DS1"})
        self.assertEqual((doc["name"], doc["description"]), ("Renamed", "first"))

    def test_dataset_not_visible_in_api_fails(self) -> None:
        self.api_lists_dataset = False
        with self.assertRaises(RuntimeError):
            self.apply()


class IngestDatasetCliTests(unittest.TestCase):
    def test_set_email_requires_controlled(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            result = CliRunner().invoke(cli, [
                "beacon", "ingest", "dataset",
                "--dataset-id", "DS1", "--name", "n", "--description", "d",
                "--ref-genome", "GRCh38", "--set-email", "a@x",
                "--dry-run", "-b", tmpdir,
            ])
        self.assertNotEqual(result.exit_code, 0)
        self.assertIn("--set-email is only valid", result.output)


if __name__ == "__main__":
    unittest.main()

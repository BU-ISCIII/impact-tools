"""Tests for `beacon ingest variants`: RI-tools guards, staging cleanup and swap."""

from __future__ import annotations

import datetime as dt
import subprocess
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from beacon_testutils import BeaconTestCase, MongoTestCase, write_vcf
from impact_tools.beacon import ingest, ritools

class RitoolsOutputTests(unittest.TestCase):
    OK_STDOUT = (
        "Successfully inserted 9 records into beacon\n"
        "A total of 10 variants were processed\n"
        "A total of 1 variants were skipped\n"
    )

    def run_with_stdout(self, stdout: str, returncode: int = 0):
        vcf = write_vcf(Path(tempfile.mkdtemp()) / "x.vcf", 10)
        completed = subprocess.CompletedProcess([], returncode, stdout=stdout, stderr="")
        mongo_config = SimpleNamespace(
            user="u", password="p", auth_source="admin", server_timeout_ms=1,
            connect_timeout_ms=1, socket_timeout_ms=1, direct=True, host="h",
            port=1, database="beacon", tls=False,
        )
        with patch.object(ritools.subprocess, "run", return_value=completed):
            return ritools.run_genomic_variations_vcf(
                mongo=mongo_config, dataset_id="DS1", input_vcf=vcf,
                reference_genome="GRCh38",
            )

    def test_counts_are_parsed(self) -> None:
        result = self.run_with_stdout(self.OK_STDOUT)
        self.assertEqual((result.processed, result.inserted, result.skipped), (10, 9, 1))

    def test_missing_populations_file_fails(self) -> None:
        stdout = (
            "pipelines/default/templates/populations.json not found, "
            "VCF is being processed without AF reads\n" + self.OK_STDOUT
        )
        with self.assertRaises(RuntimeError):
            self.run_with_stdout(stdout)

    def test_unparsable_output_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            self.run_with_stdout("something else\n")

    def test_non_zero_exit_fails(self) -> None:
        with self.assertRaises(RuntimeError):
            self.run_with_stdout(self.OK_STDOUT, returncode=1)


class RitoolsResultGuardTests(unittest.TestCase):
    def result(self, processed: int, inserted: int):
        return ritools.GenomicVariationsResult(Path("x.vcf"), processed, inserted, processed - inserted, "", "")

    def test_truncated_run_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            ingest.check_ritools_result(self.result(9, 9), vcf_count=10)

    def test_zero_inserted_is_refused(self) -> None:
        with self.assertRaises(RuntimeError):
            ingest.check_ritools_result(self.result(10, 0), vcf_count=10)

    def test_valid_run_passes(self) -> None:
        ingest.check_ritools_result(self.result(10, 8), vcf_count=10)


class ApplyVariantsTests(BeaconTestCase):
    def setUp(self) -> None:
        super().setUp()
        self.db.datasets.insert_one({"id": "DS1"})
        self.db.genomicVariations.insert_many([{"datasetId": "DS1"} for _ in range(5)])
        self.vcf_dir = self.tmp / "vcfs"
        self.vcf_dir.mkdir()
        write_vcf(self.vcf_dir / "a.vcf", 3)
        write_vcf(self.vcf_dir / "b.vcf", 3)

        p = patch(
            "impact_tools.beacon.api.verify_variant_count_via_api",
            lambda client, dataset_id, expected: True,
        )
        p.start()
        self.addCleanup(p.stop)

    def fake_ritools(self, *behaviours):
        """Each call inserts 3 staging variants or fails as requested."""

        calls = iter(behaviours)

        def run(*, mongo, dataset_id, input_vcf, reference_genome):
            behaviour = next(calls)
            if behaviour == "ok":
                self.db.genomicVariations.insert_many([{"datasetId": dataset_id} for _ in range(3)])
                return ritools.GenomicVariationsResult(input_vcf, 3, 3, 0, "", "")
            if behaviour == "zero":
                return ritools.GenomicVariationsResult(input_vcf, 3, 0, 3, "", "")
            self.db.genomicVariations.insert_one({"datasetId": dataset_id})
            raise behaviour

        return patch.object(ingest.ritools, "run_genomic_variations_vcf", run)

    def apply(self, dry_run: bool = False):
        config = ingest.VariantsIngestConfig(
            dataset_id="DS1", vcf_dir=self.vcf_dir, dry_run=dry_run,
            output_dir=self.tmp, generate_report=False,
        )
        return ingest.apply_variants_to_remote(config, self.deployment)

    def count(self, pattern: str) -> int:
        return self.db.genomicVariations.count_documents({"datasetId": {"$regex": pattern}})

    def test_successful_swap(self) -> None:
        with self.fake_ritools("ok", "ok"):
            result = self.apply()
        self.assertEqual(self.db.genomicVariations.count_documents({"datasetId": "DS1"}), 6)
        self.assertEqual(self.count(f"^{result.old_id}$"), 5)
        self.assertEqual(self.count("_staging_"), 0)

    def test_failure_discards_staging_and_keeps_active(self) -> None:
        for failure in (RuntimeError("ri-tools failed"), KeyboardInterrupt()):
            with self.subTest(failure=type(failure).__name__):
                with self.fake_ritools("ok", failure), self.assertRaises(type(failure)):
                    self.apply()
                self.assertEqual(self.count("_staging_"), 0)
                self.assertEqual(self.db.genomicVariations.count_documents({"datasetId": "DS1"}), 5)

    def test_zero_inserted_never_reaches_swap(self) -> None:
        with self.fake_ritools("ok", "zero"), self.assertRaises(RuntimeError):
            self.apply()
        self.assertEqual(self.db.genomicVariations.count_documents({"datasetId": "DS1"}), 5)
        self.assertEqual(self.count("_old_|_staging_"), 0)

    def test_dry_run_leaves_database_unchanged(self) -> None:
        with self.fake_ritools("ok", "ok"):
            self.apply(dry_run=True)
        self.assertEqual(self.db.genomicVariations.count_documents({}), 5)

    def test_unregistered_dataset_is_refused(self) -> None:
        self.db.datasets.delete_many({})
        with self.fake_ritools(), self.assertRaises(RuntimeError):
            self.apply()


class OldBackupListingTests(MongoTestCase):
    def test_lists_old_and_leftover_staging_of_the_dataset(self) -> None:
        db = self.db
        ten_days_ago = (dt.datetime.now() - dt.timedelta(days=10)).strftime("%Y%m%d_%H%M%S")
        today = dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        for dataset_id in [
            "GOE",
            f"GOE_old_{ten_days_ago}",
            f"GOE_staging_{ten_days_ago}",
            f"GOE_old_{today}",
            f"GOE2_old_{ten_days_ago}",
        ]:
            db.genomicVariations.insert_one({"datasetId": dataset_id})

        backups = {b.dataset_id: b.default_delete for b in ingest.list_old_variant_backups(db, "GOE")}

        self.assertEqual(backups, {
            f"GOE_old_{ten_days_ago}": True,
            f"GOE_staging_{ten_days_ago}": True,
            f"GOE_old_{today}": False,
        })


if __name__ == "__main__":
    unittest.main()

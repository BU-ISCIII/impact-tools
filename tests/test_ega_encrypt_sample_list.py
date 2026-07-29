"""Encryption integration tests for sample-list selection."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from impact_tools.ega.encrypt import (
    EncryptionConfig,
    _validate_selection_options,
    run_encryption,
)


class EncryptionSampleListTests(unittest.TestCase):
    def test_dry_run_manifest_keeps_sample_and_file_roles(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            raw = root / "raw"
            for sample_id in ("S1", "S2"):
                sample_dir = raw / "delivery" / sample_id
                sample_dir.mkdir(parents=True)
                (sample_dir / f"{sample_id}.cram").write_bytes(b"cram\n")
                (sample_dir / f"{sample_id}.vcf.gz").write_bytes(b"vcf\n")
            sample_list = root / "samples.txt"
            sample_list.write_text("S1\nS2\n", encoding="utf-8")
            public_key = root / "service.key.pub"
            public_key.write_text("public-key\n", encoding="utf-8")

            with patch(
                "impact_tools.ega.encrypt._resolve_crypt4gh",
                return_value="/bin/true",
            ):
                result = run_encryption(
                    EncryptionConfig(
                        input_dir=raw,
                        output_dir=root / "encrypted",
                        recipient_pubkey=public_key,
                        sample_list=sample_list,
                        dry_run=True,
                        generate_plots=False,
                        generate_report_charts=False,
                        registry_file=None,
                    )
                )

            manifest = json.loads(
                result.manifest_file.read_text(encoding="utf-8")
            )
            self.assertEqual(result.discovered, 4)
            self.assertEqual(
                [(item["sample_id"], item["input_role"]) for item in manifest["files"]],
                [
                    ("S1", "cram"),
                    ("S1", "vcf"),
                    ("S2", "cram"),
                    ("S2", "vcf"),
                ],
            )
            self.assertEqual(
                manifest["run"]["sample_list"],
                str(sample_list.resolve()),
            )
            self.assertEqual(
                {Path(item["output_file"]).parent.name for item in manifest["files"]},
                {"S1", "S2"},
            )

    def test_rejects_conflicting_selection_options(self) -> None:
        config = EncryptionConfig(
            input_dir=Path("/input"),
            recipient_pubkey=Path("/key"),
            input_list=Path("/files.txt"),
            sample_list=Path("/samples.txt"),
        )

        with self.assertRaisesRegex(ValueError, "mutually exclusive"):
            _validate_selection_options(config)


if __name__ == "__main__":
    unittest.main()

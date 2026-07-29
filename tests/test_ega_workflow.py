"""Tests for the combined EGA encryption and upload workflow."""

from __future__ import annotations

import unittest
from pathlib import Path

from impact_tools.ega.workflow import _validate_upload_layout


class EncryptUploadWorkflowTests(unittest.TestCase):
    def test_flat_layout_rejects_duplicate_remote_basenames(self) -> None:
        files = [
            Path("/encrypted/S1/variants.vcf.gz.c4gh"),
            Path("/encrypted/S2/variants.vcf.gz.c4gh"),
        ]

        with self.assertRaisesRegex(ValueError, "Flat Inbox layout"):
            _validate_upload_layout(files, "flat")

    def test_relative_layout_accepts_duplicate_basenames(self) -> None:
        files = [
            Path("/encrypted/S1/variants.vcf.gz.c4gh"),
            Path("/encrypted/S2/variants.vcf.gz.c4gh"),
        ]

        _validate_upload_layout(files, "relative")


if __name__ == "__main__":
    unittest.main()

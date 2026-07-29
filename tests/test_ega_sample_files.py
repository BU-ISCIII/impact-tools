"""Tests for strict EGA sample file discovery."""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from impact_tools.ega.sample_files import discover_sample_files, read_sample_ids


class SampleFileDiscoveryTests(unittest.TestCase):
    def test_discovers_one_cram_and_vcf_for_each_sample(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            for sample_id in ("S1", "S2"):
                sample_dir = root / sample_id
                sample_dir.mkdir()
                _write(sample_dir / f"{sample_id}.cram")
                _write(sample_dir / f"{sample_id}.vcf.gz")
                _write(sample_dir / f"{sample_id}.vcf.gz.tbi")
            sample_list = _sample_list(root, "S2\nS1\n")

            selected = discover_sample_files(
                input_dir=root,
                output_dir=root / "encrypted_c4gh",
                sample_list=sample_list,
            )

            self.assertEqual(
                [(item.sample_id, item.role, item.path.name) for item in selected],
                [
                    ("S2", "cram", "S2.cram"),
                    ("S2", "vcf", "S2.vcf.gz"),
                    ("S1", "cram", "S1.cram"),
                    ("S1", "vcf", "S1.vcf.gz"),
                ],
            )

    def test_supports_nested_sample_directory_and_uncompressed_vcf(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_dir = root / "delivery" / "S1"
            sample_dir.mkdir(parents=True)
            _write(sample_dir / "alignment.cram")
            _write(sample_dir / "variants.vcf")

            selected = discover_sample_files(
                input_dir=root,
                output_dir=root / "encrypted_c4gh",
                sample_list=_sample_list(root, "S1\n"),
            )

            self.assertEqual([item.sample_id for item in selected], ["S1", "S1"])
            self.assertEqual([item.role for item in selected], ["cram", "vcf"])

    def test_supports_flat_directory_with_exact_sample_filenames(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            _write(root / "S1.cram")
            _write(root / "S1.vcf.gz")
            _write(root / "S2.cram")
            _write(root / "S2.vcf.gz")

            selected = discover_sample_files(
                input_dir=root,
                output_dir=root / "encrypted_c4gh",
                sample_list=_sample_list(root, "S2\n"),
            )

            self.assertEqual(
                [item.path.name for item in selected],
                ["S2.cram", "S2.vcf.gz"],
            )

    def test_reports_all_incomplete_samples(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            (root / "S1").mkdir()
            _write(root / "S1" / "S1.cram")
            (root / "S2").mkdir()
            _write(root / "S2" / "S2.vcf.gz")

            with self.assertRaises(ValueError) as raised:
                discover_sample_files(
                    input_dir=root,
                    output_dir=root / "encrypted_c4gh",
                    sample_list=_sample_list(root, "S1\nS2\n"),
                )

            message = str(raised.exception)
            self.assertIn("Sample 'S1': no VCF file found", message)
            self.assertIn("Sample 'S2': no CRAM file found", message)

    def test_rejects_ambiguous_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_dir = root / "S1"
            sample_dir.mkdir()
            _write(sample_dir / "S1.cram")
            _write(sample_dir / "filtered.vcf.gz")
            _write(sample_dir / "unfiltered.vcf.gz")

            with self.assertRaisesRegex(ValueError, "multiple VCF files"):
                discover_sample_files(
                    input_dir=root,
                    output_dir=root / "encrypted_c4gh",
                    sample_list=_sample_list(root, "S1\n"),
                )

    def test_rejects_empty_files(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_dir = root / "S1"
            sample_dir.mkdir()
            (sample_dir / "S1.cram").touch()
            _write(sample_dir / "S1.vcf.gz")

            with self.assertRaisesRegex(ValueError, "CRAM file is empty"):
                discover_sample_files(
                    input_dir=root,
                    output_dir=root / "encrypted_c4gh",
                    sample_list=_sample_list(root, "S1\n"),
                )

    def test_rejects_same_file_selected_through_different_symlinks(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shared_cram = root / "shared.cram"
            _write(shared_cram)
            for sample_id in ("S1", "S2"):
                sample_dir = root / sample_id
                sample_dir.mkdir()
                (sample_dir / f"{sample_id}.cram").symlink_to(shared_cram)
                _write(sample_dir / f"{sample_id}.vcf.gz")

            with self.assertRaisesRegex(ValueError, "was also selected"):
                discover_sample_files(
                    input_dir=root,
                    output_dir=root / "encrypted_c4gh",
                    sample_list=_sample_list(root, "S1\nS2\n"),
                )

    def test_rejects_duplicate_and_unsafe_sample_ids(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_list = _sample_list(root, "S1\nS1\n../S2\n")

            with self.assertRaises(ValueError) as raised:
                read_sample_ids(sample_list)

            message = str(raised.exception)
            self.assertIn("duplicate sample identifier 'S1'", message)
            self.assertIn("unsafe sample identifier '../S2'", message)


def _write(path: Path) -> None:
    path.write_bytes(b"content\n")


def _sample_list(root: Path, content: str) -> Path:
    path = root / "samples.txt"
    path.write_text(content, encoding="utf-8")
    return path


if __name__ == "__main__":
    unittest.main()

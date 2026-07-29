"""CLI integration tests for configured defaults."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import logging
from click.testing import CliRunner

from impact_tools.__main__ import cli, configure_logging, configure_module_logging


class CliConfigTests(unittest.TestCase):
    def tearDown(self) -> None:
        logging.shutdown()
        for handler in logging.getLogger().handlers[:]:
            logging.getLogger().removeHandler(handler)

    def test_module_log_uses_specific_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            module_dir = Path(tmpdir) / "specific"
            context = SimpleNamespace(
                obj={
                    "log_file": None,
                    "configuration": {
                        "logs": {
                            "default_outpath": str(Path(tmpdir) / "default"),
                            "modules_outpath": {
                                "ega_encrypt": str(module_dir),
                            },
                        }
                    },
                }
            )
            configure_logging(verbose=False, log_file=None)

            log_file = configure_module_logging(context, "ega_encrypt")

            self.assertIsNotNone(log_file)
            self.assertEqual(log_file.parent, module_dir)
            self.assertTrue(log_file.exists())

    def test_module_log_falls_back_to_named_subdirectory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            default_dir = Path(tmpdir) / "logs"
            context = SimpleNamespace(
                obj={
                    "log_file": None,
                    "configuration": {
                        "logs": {
                            "default_outpath": str(default_dir),
                            "modules_outpath": {},
                        }
                    },
                }
            )
            configure_logging(verbose=False, log_file=None)

            log_file = configure_module_logging(context, "beacon_pgx")

            self.assertIsNotNone(log_file)
            self.assertEqual(log_file.parent, default_dir / "beacon_pgx")
            self.assertTrue(log_file.exists())

    def test_encrypt_uses_configured_paths(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            input_dir = base / "raw"
            input_dir.mkdir()
            public_key = base / "service.key.pub"
            public_key.write_text("test-key\n", encoding="utf-8")
            config_file = base / "config.json"
            config_file.write_text(
                json.dumps(
                    {
                        "ega": {
                            "encryption": {
                                "input_dir": str(input_dir),
                                "recipient_pubkey": str(public_key),
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch(
                "impact_tools.__main__.run_encryption",
                return_value=SimpleNamespace(
                    failed=0,
                    report_file=base / "report.html",
                ),
            ) as run_encryption:
                result = CliRunner().invoke(
                    cli,
                    [
                        "--config-file",
                        str(config_file),
                        "ega",
                        "encrypt",
                        "--dry-run",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            encryption_config = run_encryption.call_args.args[0]
            self.assertEqual(encryption_config.input_dir, input_dir)
            self.assertEqual(encryption_config.recipient_pubkey, public_key)

    def test_encrypt_passes_sample_list_to_encryption(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            input_dir = base / "raw"
            input_dir.mkdir()
            public_key = base / "service.key.pub"
            public_key.write_text("test-key\n", encoding="utf-8")
            sample_list = base / "samples.txt"
            sample_list.write_text("S1\nS2\n", encoding="utf-8")

            with patch(
                "impact_tools.__main__.run_encryption",
                return_value=SimpleNamespace(
                    failed=0,
                    report_file=base / "report.html",
                ),
            ) as run_encryption:
                result = CliRunner().invoke(
                    cli,
                    [
                        "ega",
                        "encrypt",
                        "--input-dir",
                        str(input_dir),
                        "--recipient-pubkey",
                        str(public_key),
                        "--sample-list",
                        str(sample_list),
                        "--dry-run",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            encryption_config = run_encryption.call_args.args[0]
            self.assertEqual(encryption_config.sample_list, sample_list)

    def test_encrypt_upload_passes_sample_list_to_encryption_stage(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            input_dir = base / "raw"
            input_dir.mkdir()
            public_key = base / "service.key.pub"
            public_key.write_text("test-key\n", encoding="utf-8")
            sample_list = base / "samples.txt"
            sample_list.write_text("S1\nS2\n", encoding="utf-8")

            with patch(
                "impact_tools.__main__.run_encrypt_upload",
                return_value=SimpleNamespace(
                    manifest_file=base / "manifest.json",
                    report_file=base / "report.html",
                    upload=SimpleNamespace(failed=0),
                ),
            ) as run_encrypt_upload:
                result = CliRunner().invoke(
                    cli,
                    [
                        "ega",
                        "encrypt-upload",
                        "--input-dir",
                        str(input_dir),
                        "--recipient-pubkey",
                        str(public_key),
                        "--sample-list",
                        str(sample_list),
                        "--host",
                        "inbox.example.org",
                        "--username",
                        "user@example.org",
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            workflow_config = run_encrypt_upload.call_args.args[0]
            self.assertEqual(workflow_config.encryption.sample_list, sample_list)

    def test_prepare_submission_passes_sample_list(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            base = Path(tmpdir)
            input_dir = base / "delivery"
            input_dir.mkdir()
            sample_list = base / "samples.txt"
            sample_list.write_text("S1\nS2\n", encoding="utf-8")

            result_payload = SimpleNamespace(
                evidence_file=base / "evidence.md",
                evidence_json=base / "evidence.json",
                draft_file=base / "draft.yaml",
                missing_file=base / "missing.yaml",
                inventory_file=base / "inventory.tsv",
            )
            with patch(
                "impact_tools.__main__.prepare_submission",
                return_value=result_payload,
            ) as prepare:
                result = CliRunner().invoke(
                    cli,
                    [
                        "ega",
                        "prepare-submission",
                        "--input-dir",
                        str(input_dir),
                        "--output-dir",
                        str(base / "prepared"),
                        "--sample-list",
                        str(sample_list),
                    ],
                )

            self.assertEqual(result.exit_code, 0, result.output)
            self.assertEqual(prepare.call_args.args[0].sample_list, sample_list)


if __name__ == "__main__":
    unittest.main()

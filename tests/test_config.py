"""Tests for persistent impact-tools configuration."""

from __future__ import annotations

import json
import tempfile
import unittest
from importlib import resources
from pathlib import Path
from unittest.mock import patch

from impact_tools import config


class ConfigTests(unittest.TestCase):
    def test_commented_initial_config_is_valid_yaml(self) -> None:
        template = resources.files("impact_tools").joinpath(
            "conf/initial_config.yaml"
        )
        configuration = config._read_config(Path(str(template)))

        self.assertEqual(
            config.get_config_value(configuration, "ega.inbox.port"),
            2222,
        )
        self.assertEqual(
            config.get_config_value(
                configuration,
                "ega.slurm_encryption.task_layout",
            ),
            "sample",
        )
        self.assertIn(
            "ega_encrypt",
            config.get_config_value(
                configuration,
                "logs.modules_outpath",
            ),
        )

    def test_extra_config_overrides_defaults_recursively(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            extra_path = Path(tmpdir) / "extra_config.json"
            extra_path.write_text(
                json.dumps(
                    {
                        "ega": {
                            "inbox": {
                                "host": "inbox.example.org",
                                "identity_file": "~/.ssh/ega",
                            }
                        }
                    }
                ),
                encoding="utf-8",
            )

            with patch.object(config, "EXTRA_CONFIG_PATH", extra_path):
                configuration = config.load_configuration()

            self.assertEqual(
                config.get_config_value(configuration, "ega.inbox.host"),
                "inbox.example.org",
            )
            self.assertEqual(
                config.get_config_value(configuration, "ega.inbox.port"),
                2222,
            )

    def test_include_yaml_and_remove_section(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            extra_path = tmp_path / ".impact_tools" / "extra_config.json"
            source = tmp_path / "settings.yaml"
            source.write_text("directory: /tmp/impact-logs\n", encoding="utf-8")

            with patch.object(config, "EXTRA_CONFIG_PATH", extra_path):
                result = config.include_extra_config(
                    source,
                    config_name="logs",
                )
                stored = json.loads(result.read_text(encoding="utf-8"))
                removed = config.remove_extra_config("logs")

            self.assertEqual(stored, {"logs": {"directory": "/tmp/impact-logs"}})
            self.assertTrue(removed)
            self.assertFalse(extra_path.exists())

    def test_existing_section_requires_force(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            tmp_path = Path(tmpdir)
            extra_path = tmp_path / "extra_config.json"
            extra_path.write_text('{"logs": {"directory": "/old"}}\n')
            source = tmp_path / "settings.json"
            source.write_text('{"logs": {"directory": "/new"}}\n')

            with patch.object(config, "EXTRA_CONFIG_PATH", extra_path):
                with self.assertRaisesRegex(ValueError, "already exist"):
                    config.include_extra_config(source)
                config.include_extra_config(source, force=True)

            stored = json.loads(extra_path.read_text(encoding="utf-8"))
            self.assertEqual(stored["logs"]["directory"], "/new")


if __name__ == "__main__":
    unittest.main()

"""Configuration loading and user overrides for impact-tools."""

from __future__ import annotations

import json
import os
from copy import deepcopy
from importlib import resources
from pathlib import Path
from typing import Any

import yaml


EXTRA_CONFIG_PATH = Path(
    os.environ.get(
        "IMPACT_TOOLS_CONFIG",
        Path.home() / ".impact_tools" / "extra_config.json",
    )
).expanduser()


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Merge nested dictionaries, with override values taking precedence."""
    merged = deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = deepcopy(value)
    return merged


def _read_config(path: Path) -> dict[str, Any]:
    """Read a JSON or YAML configuration file."""
    suffix = path.suffix.lower()
    with path.expanduser().open("r", encoding="utf-8") as handle:
        if suffix == ".json":
            content = json.load(handle)
        elif suffix in {".yaml", ".yml"}:
            content = yaml.safe_load(handle)
        else:
            raise ValueError(
                f"Unsupported configuration format for {path}. "
                "Use .json, .yaml or .yml."
            )
    if content is None:
        return {}
    if not isinstance(content, dict):
        raise ValueError(f"Configuration root must be an object: {path}")
    return content


def load_configuration(
    config_file: Path | None = None,
    *,
    include_extra: bool = True,
) -> dict[str, Any]:
    """Load package defaults, user overrides and an optional explicit file."""
    with resources.files("impact_tools").joinpath("conf/configuration.json").open(
        "r",
        encoding="utf-8",
    ) as handle:
        configuration = json.load(handle)

    if include_extra and EXTRA_CONFIG_PATH.is_file():
        configuration = _deep_merge(configuration, _read_config(EXTRA_CONFIG_PATH))
    if config_file is not None:
        configuration = _deep_merge(
            configuration,
            _read_config(config_file.expanduser()),
        )
    return configuration


def get_config_value(
    configuration: dict[str, Any],
    path: str,
    default: Any = None,
) -> Any:
    """Return a value using a dot-separated configuration path."""
    value: Any = configuration
    for key in path.split("."):
        if not isinstance(value, dict) or key not in value:
            return default
        value = value[key]
    return value


def include_extra_config(
    config_file: Path,
    *,
    config_name: str | None = None,
    force: bool = False,
) -> Path:
    """Add a JSON/YAML file to the persistent user configuration."""
    incoming = _read_config(config_file.expanduser())
    current = _read_config(EXTRA_CONFIG_PATH) if EXTRA_CONFIG_PATH.is_file() else {}

    if config_name is not None:
        if config_name in current and not force:
            raise ValueError(
                f"Configuration section already exists: {config_name}. "
                "Use --force to replace it."
            )
        current[config_name] = incoming
    elif force:
        current = _deep_merge(current, incoming)
    else:
        collisions = sorted(set(current).intersection(incoming))
        if collisions:
            raise ValueError(
                "Configuration section(s) already exist: "
                f"{', '.join(collisions)}. Use --force to merge them."
            )
        current.update(incoming)

    _write_extra_config(current)
    return EXTRA_CONFIG_PATH


def remove_extra_config(
    config_name: str | None = None,
    *,
    force: bool = False,
) -> bool:
    """Remove one top-level section or the complete user configuration."""
    if not EXTRA_CONFIG_PATH.exists():
        return False
    if config_name is None:
        if not force:
            raise ValueError("Use --force to remove the complete extra configuration.")
        EXTRA_CONFIG_PATH.unlink()
        return True

    current = _read_config(EXTRA_CONFIG_PATH)
    if config_name not in current:
        return False
    del current[config_name]
    if current:
        _write_extra_config(current)
    else:
        EXTRA_CONFIG_PATH.unlink()
    return True


def _write_extra_config(configuration: dict[str, Any]) -> None:
    """Write user configuration atomically with private permissions."""
    EXTRA_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    temporary = EXTRA_CONFIG_PATH.with_suffix(".json.tmp")
    temporary.write_text(
        json.dumps(configuration, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.chmod(0o600)
    temporary.replace(EXTRA_CONFIG_PATH)

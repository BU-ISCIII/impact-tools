"""Typed configuration for Beacon workflows."""

from __future__ import annotations

import dataclasses
import os
from typing import Any

from impact_tools.config import get_config_value, load_configuration


MONGO_PASSWORD_ENV = "IMPACT_TOOLS_BEACON_MONGO_PASSWORD"


@dataclasses.dataclass(frozen=True)
class BeaconMongoConfig:
    """Direct MongoDB connection configuration."""

    host: str
    port: int = 27017
    user: str | None = None
    password: str | None = None
    auth_source: str = "admin"
    database: str = "beacon"
    direct: bool = True

    tls: bool = False
    tls_ca: str | None = None
    tls_cert: str | None = None
    tls_allow_invalid: bool = False

    server_timeout_ms: int = 10_000
    connect_timeout_ms: int = 10_000
    socket_timeout_ms: int = 30_000


@dataclasses.dataclass(frozen=True)
class BeaconApiConfig:
    """Direct Beacon HTTP API configuration."""

    base_url: str
    timeout_seconds: float = 30.0
    verify_tls: bool = True


@dataclasses.dataclass(frozen=True)
class BeaconDeploymentConfig:
    """Complete configuration required by Beacon ingestion workflows."""

    mongo: BeaconMongoConfig
    api: BeaconApiConfig


def _as_bool(value: Any, *, default: bool = False) -> bool:
    """Convert common configuration representations to boolean."""

    if value is None:
        return default

    if isinstance(value, bool):
        return value

    if isinstance(value, int):
        return value != 0

    if isinstance(value, str):
        normalized = value.strip().lower()

        if normalized in {"true", "yes", "on", "1"}:
            return True

        if normalized in {"false", "no", "off", "0", ""}:
            return False

    raise ValueError(
        f"Expected a boolean-compatible configuration value, got {value!r}."
    )


def _require_string(value: Any, configuration_path: str) -> str:
    """Return a required non-empty string configuration value."""

    if not isinstance(value, str) or not value.strip():
        raise ValueError(
            f"{configuration_path} is required in the "
            "impact-tools configuration."
        )

    return value.strip()


def build_beacon_deployment_config(
    cfg: dict | None = None,
) -> BeaconDeploymentConfig:
    """Build typed Beacon configuration.

    Configuration is read from package defaults, user overrides and an
    optional explicit configuration already loaded by the CLI.

    The MongoDB password is read first from
    ``IMPACT_TOOLS_BEACON_MONGO_PASSWORD``. The configuration-file value is
    accepted only as a fallback during migration.
    """

    if cfg is None:
        cfg = load_configuration()

    def _get(path: str, default: Any = None) -> Any:
        return get_config_value(
            cfg,
            f"beacon.{path}",
            default,
        )

    mongo_password = (
        os.environ.get(MONGO_PASSWORD_ENV)
        or _get("mongo.password")
    )

    if not mongo_password:
        raise ValueError(
            "MongoDB password is required. Set the "
            f"{MONGO_PASSWORD_ENV} environment variable."
        )

    mongo = BeaconMongoConfig(
        host=_require_string(
            _get("mongo.host"),
            "beacon.mongo.host",
        ),
        port=int(_get("mongo.port", 27017)),
        user=_require_string(
            _get("mongo.user"),
            "beacon.mongo.user",
        ),
        password=mongo_password,
        auth_source=_get("mongo.auth_source", "admin"),
        database=_get("mongo.database", "beacon"),
        direct=_as_bool(
            _get("mongo.direct", True),
            default=True,
        ),
        tls=_as_bool(
            _get("mongo.tls", False),
            default=False,
        ),
        tls_ca=_get("mongo.tls_ca"),
        tls_cert=_get("mongo.tls_cert"),
        tls_allow_invalid=_as_bool(
            _get("mongo.tls_allow_invalid", False),
            default=False,
        ),
        server_timeout_ms=int(
            _get(
                "mongo.server_timeout_ms",
                10_000,
            )
        ),
        connect_timeout_ms=int(
            _get(
                "mongo.connect_timeout_ms",
                10_000,
            )
        ),
        socket_timeout_ms=int(
            _get(
                "mongo.socket_timeout_ms",
                30_000,
            )
        ),
    )

    # remote.api_url is accepted temporarily for compatibility with older
    # configuration files. Prefer beacon.api.base_url.
    api_base_url = _get(
        "api.base_url",
        _get("remote.api_url"),
    )

    api = BeaconApiConfig(
        base_url=_require_string(
            api_base_url,
            "beacon.api.base_url",
        ).rstrip("/"),
        timeout_seconds=float(
            _get("api.timeout_seconds", 30.0)
        ),
        verify_tls=_as_bool(
            _get("api.verify_tls", True),
            default=True,
        ),
    )

    return BeaconDeploymentConfig(
        mongo=mongo,
        api=api,
    )

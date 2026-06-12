"""SSH/SFTP helpers and config loading for Beacon remote VM operations."""

from __future__ import annotations

import dataclasses
import logging
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

LOGGER = logging.getLogger(__name__)

# DEFAULT_CONFIG_PATH = Path("~/.config/impact-tools/config.yaml")


# ---------------------------------------------------------------------------
# Config dataclasses (mapped from config.yaml beacon.* keys)
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class BeaconRemoteConfig:
    host: str
    user: str
    port: int = 22
    password: str | None = None
    identity_file: str | None = None
    beacon_dir: str = "/opt/beacon/beacon2-pi-api-isciii"
    input_dir: str = "/impact_data/lega_data/beacon/inputs"
    log_dir: str = "/var/log/local/beacon/apps/ri-tools"
    api_url: str = "http://beaconaf-isciiiciber.isciiides.es:8443"
    
    datasets_conf_dir: str = "/opt/beacon/beacon2-pi-api-isciii/beacon/conf/datasets"
    datasets_permissions_dir: str = "/opt/beacon/beacon2-pi-api-isciii/beacon/permissions/datasets"

    @property
    def ri_tools_conf_base(self) -> str:
        """Base directory for per-dataset RI-tools conf.py on the VM.

        Derives from input_dir: /path/to/beacon/inputs → /path/to/beacon/config/
        """
        return str(PurePosixPath(self.input_dir).parent / "config")

    @property
    def datasets_conf_yml(self) -> str:
        """Absolute path to the global datasets_conf.yml on the VM."""
        return f"{self.datasets_conf_dir}/datasets_conf.yml"

    @property
    def datasets_permissions_yml(self) -> str:
        """Absolute path to the global datasets_permissions.yml on the VM."""
        return f"{self.datasets_permissions_dir}/datasets_permissions.yml"

@dataclasses.dataclass
class BeaconContainersConfig:
    mongo: str = "mongoprod"
    api: str = "beaconprod"


@dataclasses.dataclass
class BeaconMongoConfig:
    user: str = "root"
    password: str = "example"
    auth_source: str = "admin"
    database: str = "beacon"
    tls: bool = True
    tls_cert: str = "/etc/mongo/certs/server.pem"
    tls_ca: str = "/etc/mongo/certs/ca.crt"
    tls_allow_invalid: bool = True


@dataclasses.dataclass
class BeaconRuntimeConfig:
    remote_container_runtime: str = "podman"


@dataclasses.dataclass
class BeaconRitoolsConfig:
    """Configuration for running beacon2-ri-tools-v2 on the Beacon VM."""

    python: str = "/opt/localEGA/tools/micromamba/envs/impact-tools/bin/python"
    db_host: str = "localhost"
    tls_ca: str = "/opt/beacon/beacon2-pi-api-isciii/certs/ca.crt"
    tls_cert: str = "/opt/beacon/beacon2-pi-api-isciii/certs/server.pem"


@dataclasses.dataclass
class BeaconDeploymentConfig:
    """Full deployment configuration parsed from config.yaml."""

    remote: BeaconRemoteConfig
    containers: BeaconContainersConfig
    mongo: BeaconMongoConfig
    runtime: BeaconRuntimeConfig
    ritools: BeaconRitoolsConfig 


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def build_beacon_deployment_config(
    cfg: dict | None = None,
) -> BeaconDeploymentConfig:
    """Build a BeaconDeploymentConfig from the impact-tools config system.

    Reads configuration via impact_tools.config.load_configuration (defaults
    + ~/.impact_tools/extra_config.json + optional explicit overrides) and
    constructs the typed dataclasses used by the rest of remote.py.

    This replaces the legacy load_beacon_deployment_config() that read a
    standalone ~/.config/impact-tools/config.yaml.
    """
    from impact_tools.config import load_configuration, get_config_value

    if cfg is None:
        cfg = load_configuration()

    def _g(path: str, default=None):
        return get_config_value(cfg, f"beacon.{path}", default)

    host = _g("remote.host")
    if not host:
        raise ValueError(
            "beacon.remote.host is required in the impact-tools configuration."
        )

    remote = BeaconRemoteConfig(
        host=host,
        user=_g("remote.user", "bioinfo"),
        port=int(_g("remote.port", 22)),
        password=_g("remote.password"),
        identity_file=_g("remote.identity_file"),
        beacon_dir=_g(
            "remote.beacon_dir",
            "/opt/beacon/beacon2-pi-api-isciii",
        ),
        input_dir=_g(
            "remote.input_dir",
            "/impact_data/lega_data/beacon/inputs",
        ),
        log_dir=_g(
            "remote.log_dir",
            "/var/log/local/beacon/apps/ri-tools",
        ),
                api_url=_g(
            "remote.api_url",
            "http://beaconaf-isciiiciber.isciiides.es:8443",
        ),
        datasets_conf_dir=_g(
            "remote.datasets_conf_dir",
            "/opt/beacon/beacon2-pi-api-isciii/beacon/conf/datasets",
        ),
        datasets_permissions_dir=_g(
            "remote.datasets_permissions_dir",
            "/opt/beacon/beacon2-pi-api-isciii/beacon/permissions/datasets",
        ),
    )

    containers = BeaconContainersConfig(
        mongo=_g("containers.mongo", "mongoprod"),
    )

    mongo = BeaconMongoConfig(
        user=_g("mongo.user", "root"),
        password=_g("mongo.password", "example"),
        auth_source=_g("mongo.auth_source", "admin"),
        database=_g("mongo.database", "beacon"),
        tls=bool(_g("mongo.tls", True)),
        tls_cert=_g("mongo.tls_cert", "/etc/mongo/certs/server.pem"),
        tls_ca=_g("mongo.tls_ca", "/etc/mongo/certs/ca.crt"),
        tls_allow_invalid=bool(_g("mongo.tls_allow_invalid", True)),
    )

    runtime = BeaconRuntimeConfig(
            remote_container_runtime=_g(
                "runtime.remote_container_runtime",
                "podman",
            ),
        )

    ritools = BeaconRitoolsConfig(
        python=_g(
            "ritools.python",
            "/opt/localEGA/tools/micromamba/envs/impact-tools/bin/python",
        ),
        db_host=_g("ritools.db_host", "localhost"),
        tls_ca=_g(
            "ritools.tls_ca",
            "/opt/beacon/beacon2-pi-api-isciii/certs/ca.crt",
        ),
        tls_cert=_g(
            "ritools.tls_cert",
            "/opt/beacon/beacon2-pi-api-isciii/certs/server.pem",
        ),
    )

    return BeaconDeploymentConfig(
        remote=remote,
        containers=containers,
        mongo=mongo,
        runtime=runtime,
        ritools=ritools,
    )


# ---------------------------------------------------------------------------
# SSH / SFTP helpers
# ---------------------------------------------------------------------------


@dataclasses.dataclass
class RemoteExecResult:
    command: str
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@contextmanager
def managed_ssh(cfg: BeaconRemoteConfig):
    """Context manager that opens and closes a paramiko SSH connection."""
    client = _open_ssh_client(cfg)
    try:
        yield client
    finally:
        client.close()


def _open_ssh_client(cfg: BeaconRemoteConfig):
    import paramiko

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    kwargs: dict = dict(
        hostname=cfg.host,
        port=cfg.port,
        username=cfg.user,
        look_for_keys=cfg.identity_file is None and cfg.password is None,
        allow_agent=cfg.identity_file is None and cfg.password is None,
    )
    if cfg.identity_file is not None:
        kwargs["key_filename"] = str(
            Path(cfg.identity_file).expanduser().resolve()
        )
    if cfg.password is not None:
        kwargs["password"] = cfg.password

    LOGGER.info("SSH connect: %s@%s:%d", cfg.user, cfg.host, cfg.port)
    client.connect(**kwargs)
    return client


def exec_remote(
    client,
    command: str,
    stdin_data: bytes | None = None,
) -> RemoteExecResult:
    """Execute a shell command on the remote host, optionally piping stdin."""
    LOGGER.debug("Remote exec: %s", command)
    stdin_ch, stdout_ch, stderr_ch = client.exec_command(command)
    if stdin_data is not None:
        stdin_ch.write(stdin_data)
        stdin_ch.channel.shutdown_write()
    returncode = stdout_ch.channel.recv_exit_status()
    return RemoteExecResult(
        command=command,
        returncode=returncode,
        stdout=stdout_ch.read().decode("utf-8", errors="replace"),
        stderr=stderr_ch.read().decode("utf-8", errors="replace"),
    )


def sftp_upload(client, local_path: Path, remote_path: str) -> None:
    """Upload a local file to the remote host via SFTP."""
    with client.open_sftp() as sftp:
        _sftp_mkdir_parents(sftp, str(PurePosixPath(remote_path).parent))
        LOGGER.info("SFTP upload: %s → %s", local_path, remote_path)
        sftp.put(str(local_path), remote_path)


def sftp_read_text(client, remote_path: str) -> str | None:
    """Read a UTF-8 text file from the remote host. Returns None if absent."""
    with client.open_sftp() as sftp:
        try:
            with sftp.open(remote_path, "r") as fh:
                return fh.read().decode("utf-8")
        except FileNotFoundError:
            return None


def sftp_write_text(client, remote_path: str, content: str) -> None:
    """Write UTF-8 text to a remote file via SFTP, creating parents as needed."""
    with client.open_sftp() as sftp:
        _sftp_mkdir_parents(sftp, str(PurePosixPath(remote_path).parent))
        LOGGER.info("SFTP write: %s", remote_path)
        with sftp.open(remote_path, "w") as fh:
            fh.write(content.encode("utf-8"))


def _sftp_mkdir_parents(sftp, remote_dir: str) -> None:
    """Create a remote directory and all parents (equivalent to mkdir -p)."""
    path = PurePosixPath(remote_dir)
    parts = list(path.parts)
    current = ""
    for part in parts:
        current = str(PurePosixPath(current) / part) if current else part
        try:
            sftp.mkdir(current)
        except OSError:
            pass  # already exists


# ---------------------------------------------------------------------------
# Beacon ingest orchestration
# ---------------------------------------------------------------------------

def upload_ritools_conf(
    client,
    local_conf: Path,
    remote_cfg: BeaconRemoteConfig,
    dataset_id: str,
) -> str:
    """Upload a per-dataset conf.py to <ri_tools_conf_base>/<dataset_id>/conf.py.
    Returns the remote path where the conf was written.
    """
    remote_path = f"{remote_cfg.ri_tools_conf_base}/{dataset_id}/conf.py"
    sftp_upload(client, local_conf, remote_path)
    return remote_path

def upload_metrics_file(
    client,
    local_metrics: Path,
    remote_cfg: BeaconRemoteConfig,
    dataset_id: str,
) -> str:
    """Upload a local metrics JSON file to the remote Beacon log directory.

    Metrics are stored under:
      <remote.log_dir>/<dataset_id>/<metrics_file_name>

    Returns the remote path.
    """
    remote_path = f"{remote_cfg.log_dir}/{dataset_id}/{local_metrics.name}"
    sftp_upload(client, local_metrics, remote_path)
    return remote_path


def update_yaml_block_remote(
    client,
    remote_path: str,
    dataset_id: str,
    new_block: str,
) -> str:
    """Update a top-level dataset block in a remote YAML file. """
    import datetime as _dt

    # 1. Read current remote content
    current = sftp_read_text(client, remote_path)
    if current is None:
        current = ""

    # 2. Drop existing block for this dataset_id, if any
    cleaned = _drop_yaml_top_level_block(current, dataset_id)

    # 3. Append the new block (ensure exactly one blank line between blocks)
    cleaned_stripped = cleaned.rstrip("\n")
    if cleaned_stripped:
        merged = f"{cleaned_stripped}\n{new_block.rstrip()}\n"
    else:
        merged = f"{new_block.rstrip()}\n"

    # 4. Backup the original (only if it had content)
    backup_path = ""
    if current:
        ts = _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_path = f"{remote_path}.bak_{ts}"
        sftp_write_text(client, backup_path, current)
        LOGGER.info("Backup written: %s", backup_path)

    # 5. Write merged content
    sftp_write_text(client, remote_path, merged)
    LOGGER.info("Updated YAML block for '%s' in %s", dataset_id, remote_path)

    return backup_path


def _drop_yaml_top_level_block(content: str, dataset_id: str) -> str:
    """Remove the top-level YAML block for `dataset_id`, preserving the rest.

    Top-level keys are detected by being non-indented (no leading whitespace).
    The block runs until the next non-indented key or end of file.
    """
    lines = content.splitlines(keepends=True)
    result: list[str] = []
    skipping = False
    target = f"{dataset_id}:"

    for line in lines:
        if not line.startswith((" ", "\t")) and line.strip():
            if line.rstrip("\n").rstrip() == target:
                skipping = True
                continue
            else:
                skipping = False

        if not skipping:
            result.append(line)

    return "".join(result)


def restart_beacon_api(
    client,
    remote_cfg: BeaconRemoteConfig,
    wait_seconds: int = 15,
    service: str = "beaconprod",
) -> None:
    """Restart the Beacon API service on the remote VM.

    Restart is required after updating datasets_conf.yml or
    datasets_permissions.yml so the API reloads them on startup.
    """
    import time

    command = (
        f"cd {remote_cfg.beacon_dir} && "
        f"podman-compose restart {service}"
    )

    LOGGER.info("Restarting Beacon API (%s)...", service)
    result = exec_remote(client, command)

    if not result.ok:
        raise RuntimeError(
            f"restart of {service} failed: {result.stderr or result.stdout}"
        )

    LOGGER.info("Waiting %ds for API to come back up...", wait_seconds)
    time.sleep(wait_seconds)
    LOGGER.info("Beacon API restart completed.")


def verify_dataset_via_api(
    client,
    remote_cfg: BeaconRemoteConfig,
    dataset_id: str,
) -> bool:
    """Verify that a dataset is visible via the Beacon API.

    Runs `curl` from the VM against the public Beacon endpoint and checks
    whether the dataset_id appears in the response from /api/datasets.

    Returns True if the dataset is exposed by the API, False otherwise.
    """
    command = (
        f"curl -s '{remote_cfg.api_url}/api/datasets"
        f"?requestedGranularity=record&limit=1000' "
        f"| grep -c '\"id\": \"{dataset_id}\"'"
    )

    LOGGER.info("Verifying dataset '%s' via API...", dataset_id)
    result = exec_remote(client, command)

    if result.returncode not in (0, 1):
        raise RuntimeError(
            f"verify_dataset_via_api failed for {dataset_id}.\n"
            f"STDERR: {result.stderr}"
        )

    count = int(result.stdout.strip() or "0")
    found = count > 0

    if found:
        LOGGER.info("Dataset '%s' is visible via API.", dataset_id)
    else:
        LOGGER.warning("Dataset '%s' is NOT visible via API.", dataset_id)

    return found


def verify_variant_count_via_api(
    client,
    remote_cfg: BeaconRemoteConfig,
    dataset_id: str,
    expected_count: int,
) -> bool:
    """Verify that a dataset exposes the expected number of variants via API.

    Runs a Beacon genomic variants query from the VM and checks whether
    responseSummary.numTotalResults matches the expected count.

    Returns True if the API count matches, False otherwise.
    """
    import json

    command = (
        f"curl -s '{remote_cfg.api_url}/api/g_variants"
        f"?datasets={dataset_id}"
        f"&requestedGranularity=count"
        f"&limit=0'"
    )

    LOGGER.info(
        "Verifying variant count for dataset '%s' via API...",
        dataset_id,
    )
    result = exec_remote(client, command)

    if not result.ok:
        raise RuntimeError(
            f"verify_variant_count_via_api failed for {dataset_id}.\n"
            f"STDERR: {result.stderr}"
        )

    try:
        payload = json.loads(result.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(
            "Could not parse Beacon API response as JSON.\n"
            f"Dataset ID: {dataset_id}\n"
            f"Response: {result.stdout[:1000]}"
        ) from exc

    observed_count = (
        payload
        .get("responseSummary", {})
        .get("numTotalResults")
    )

    if observed_count != expected_count:
        LOGGER.warning(
            "Variant count mismatch via API for %s: observed=%s expected=%s",
            dataset_id,
            observed_count,
            expected_count,
        )
        return False

    LOGGER.info(
        "Variant count verified via API for %s: %d variants",
        dataset_id,
        expected_count,
    )

    return True
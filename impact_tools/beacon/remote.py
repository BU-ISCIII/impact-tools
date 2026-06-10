"""SSH/SFTP helpers and config loading for Beacon remote VM operations."""

from __future__ import annotations

import dataclasses
import logging
from contextlib import contextmanager
from pathlib import Path, PurePosixPath

LOGGER = logging.getLogger(__name__)

DEFAULT_CONFIG_PATH = Path("~/.config/impact-tools/config.yaml")


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
    # ri_tools: str = "ri-tools"


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
class BeaconDeploymentConfig:
    """Full deployment configuration parsed from config.yaml."""

    remote: BeaconRemoteConfig
    containers: BeaconContainersConfig
    mongo: BeaconMongoConfig
    runtime: BeaconRuntimeConfig


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------


def load_beacon_deployment_config(
    config_path: Path = DEFAULT_CONFIG_PATH,
) -> BeaconDeploymentConfig:
    """Load Beacon deployment config from a YAML file."""
    import yaml  # pyyaml — optional at import time

    path = Path(config_path).expanduser()
    if not path.exists():
        raise FileNotFoundError(
            f"Beacon config not found: {path}. "
            "Create ~/.config/impact-tools/config.yaml with a 'beacon' section."
        )

    with path.open("r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh) or {}

    beacon = raw.get("beacon", {})

    remote_raw = beacon.get("remote", {})
    if not remote_raw.get("host"):
        raise ValueError(
            f"beacon.remote.host is required in {path}"
        )

    remote = BeaconRemoteConfig(
        host=remote_raw["host"],
        user=remote_raw.get("user", remote_raw.get("username", "bioinfo")),
        port=int(remote_raw.get("port", 22)),
        password=remote_raw.get("password"),
        identity_file=remote_raw.get("identity_file"),
        beacon_dir=remote_raw.get("beacon_dir", "/opt/beacon/beacon2-pi-api-isciii"),
        input_dir=remote_raw.get("input_dir", "/impact_data/lega_data/beacon/inputs"),
        log_dir=remote_raw.get("log_dir", "/var/log/local/beacon/apps/ri-tools"),
        datasets_conf_dir=remote_raw.get(
            "datasets_conf_dir",
            "/opt/beacon/beacon2-pi-api-isciii/beacon/conf/datasets"
        ),
        datasets_permissions_dir=remote_raw.get(
            "datasets_permissions_dir",
            "/opt/beacon/beacon2-pi-api-isciii/beacon/permissions/datasets",
        ),
        api_url=remote_raw.get(
            "api_url",
            "http://beaconaf-isciiiciber.isciiides.es:8443",
        ),
    )

    containers_raw = beacon.get("containers", {})
    containers = BeaconContainersConfig(
        mongo=containers_raw.get("mongo", "mongoprod"),
        # ri_tools=containers_raw.get("ri_tools", "ri-tools"),
    )

    mongo_raw = beacon.get("mongo", {})
    mongo = BeaconMongoConfig(
        user=mongo_raw.get("user", "root"),
        password=mongo_raw.get("password", "example"),
        auth_source=mongo_raw.get("auth_source", "admin"),
        database=mongo_raw.get("database", "beacon"),
        tls=mongo_raw.get("tls", True),
        tls_cert=mongo_raw.get("tls_cert", "/etc/mongo/certs/server.pem"),
        tls_ca=mongo_raw.get("tls_ca", "/etc/mongo/certs/ca.crt"),
        tls_allow_invalid=bool(mongo_raw.get("tls_allow_invalid", True)),
    )

    runtime_raw = beacon.get("runtime", {})
    runtime = BeaconRuntimeConfig(
        remote_container_runtime=runtime_raw.get("remote_container_runtime", "podman"),
    )

    return BeaconDeploymentConfig(
        remote=remote,
        containers=containers,
        mongo=mongo,
        runtime=runtime,
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

def mongo_count_dataset(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    dataset_id: str,
) -> int:
    """Count documents in MongoDB.datasets with the given id.

    Returns the number of matching documents (0 if not present, 1 if present).
    Useful for verifying that an ingest succeeded before declaring the
    dataset registered.
    """
    eval_js = (
        f'db.getSiblingDB("{mongo_cfg.database}").datasets'
        f'.countDocuments({{id: "{dataset_id}"}})'
    )

    command_parts = [
        f"podman exec {containers_cfg.mongo}",
        "mongosh",
        "--quiet",
        f"-u {mongo_cfg.user}",
        f"-p {mongo_cfg.password}",
        f"--authenticationDatabase {mongo_cfg.auth_source}",
    ]

    if mongo_cfg.tls:
        command_parts.extend([
            "--tls",
            f"--tlsCAFile {mongo_cfg.tls_ca}",
            f"--tlsCertificateKeyFile {mongo_cfg.tls_cert}",
        ])
        if mongo_cfg.tls_allow_invalid:
            command_parts.append("--tlsAllowInvalidCertificates")

    command_parts.append(f"--eval '{eval_js}'")
    command = " ".join(command_parts)

    result = exec_remote(client, command)

    if not result.ok:
        raise RuntimeError(
            f"mongo_count_dataset failed for {dataset_id}.\n"
            f"Command: {result.command}\n"
            f"STDERR: {result.stderr}"
        )

    output = result.stdout.strip()
    try:
        return int(output)
    except ValueError as exc:
        raise RuntimeError(
            f"Unexpected output from mongosh (not an integer): {output!r}"
        ) from exc
    

def mongo_import_datasets(
    client,
    mongo_cfg: BeaconMongoConfig,
    containers_cfg: BeaconContainersConfig,
    local_json: Path,
    remote_tmp: str = "/tmp/datasets.json",
) -> int:
    """Import a datasets.json into MongoDB.<database>.datasets.

    The JSON is uploaded to the VM, copied into the MongoDB container,
    and ingested with `mongoimport --jsonArray`. Connection uses the URI
    form (TLS options embedded) because mongoimport does not accept TLS
    flags as separate arguments.

    Returns the number of documents reported as imported by mongoimport.
    """
    from urllib.parse import quote_plus

    # 1. SFTP upload to the VM host filesystem
    sftp_upload(client, local_json, remote_tmp)

    # 2. Copy from VM host into the Mongo container
    cp_cmd = (
        f"podman cp {remote_tmp} "
        f"{containers_cfg.mongo}:{remote_tmp}"
    )
    cp_result = exec_remote(client, cp_cmd)
    if not cp_result.ok:
        raise RuntimeError(
            f"podman cp failed: {cp_result.stderr or cp_result.stdout}"
        )

    # 3. Build the mongoimport URI with TLS embedded
    uri_params = [f"authSource={mongo_cfg.auth_source}"]
    if mongo_cfg.tls:
        uri_params.extend([
            "tls=true",
            f"tlsCAFile={mongo_cfg.tls_ca}",
            f"tlsCertificateKeyFile={mongo_cfg.tls_cert}",
        ])
    uri = (
        f"mongodb://{quote_plus(mongo_cfg.user)}:{quote_plus(mongo_cfg.password)}"
        f"@127.0.0.1:27017/{mongo_cfg.database}?{'&'.join(uri_params)}"
    )

    # 4. Run mongoimport
    import_parts = [
        f"podman exec {containers_cfg.mongo}",
        "mongoimport",
        "--jsonArray",
        f'--uri "{uri}"',
    ]
    if mongo_cfg.tls and mongo_cfg.tls_allow_invalid:
        import_parts.append("--tlsInsecure")
    import_parts.extend([
        f"--file {remote_tmp}",
        "--collection datasets",
    ])
    import_cmd = " ".join(import_parts)

    result = exec_remote(client, import_cmd)

    if not result.ok:
        raise RuntimeError(
            f"mongoimport failed: {result.stderr or result.stdout}"
        )

    # 5. Parse the mongoimport summary line, e.g.:
    #    "N document(s) imported successfully. M document(s) failed to import."
    import re
    match = re.search(
        r"(\d+) document\(s\) imported successfully",
        result.stderr + result.stdout,
    )
    if not match:
        raise RuntimeError(
            f"Could not parse mongoimport output:\n{result.stdout}\n{result.stderr}"
        )

    imported = int(match.group(1))
    return imported


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

    # grep -c returns:
    #   0 (success) + stdout="N" → found
    #   1 (no match) + stdout="0" → not found
    #   2+ → other error
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
"""SSH/SFTP helpers for Beacon remote VM operations."""

from __future__ import annotations

import dataclasses
import logging
import shutil
import socket
import subprocess
from contextlib import contextmanager
from io import BytesIO
from pathlib import Path, PurePosixPath

from impact_tools.beacon.config import BeaconRemoteConfig

LOGGER = logging.getLogger(__name__)


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


def _is_local_host(host: str) -> bool:
    """Return True if *host* resolves to an IP address owned by this machine."""
    if host in ("localhost", "127.0.0.1", "::1", ""):
        return True
    try:
        resolved = socket.gethostbyname(host)
    except socket.gaierror:
        return False
    try:
        local_addrs = {
            info[4][0]
            for info in socket.getaddrinfo(socket.gethostname(), None)
        }
        local_addrs.add("127.0.0.1")
    except socket.gaierror:
        local_addrs: set[str] = set()
    return resolved in local_addrs


class _LocalSftpClient:
    """SFTP-compatible client that operates on local files directly."""

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass

    def open(self, path: str, mode: str = "r"):
        return open(path, "rb" if "r" in mode else "wb")

    def mkdir(self, path: str) -> None:
        try:
            Path(path).mkdir()
        except FileExistsError:
            pass

    def put(self, local_path: str, remote_path: str) -> None:
        shutil.copy2(local_path, remote_path)


class _LocalShellClient:
    """SSH-compatible client that runs commands locally via subprocess."""

    class _Channel:
        def __init__(self, returncode: int):
            self._returncode = returncode

        def recv_exit_status(self) -> int:
            return self._returncode

        def shutdown_write(self) -> None:
            pass

    class _Stream:
        def __init__(self, data: bytes, channel):
            self._buf = BytesIO(data)
            self.channel = channel

        def read(self) -> bytes:
            return self._buf.read()

        def write(self, data: bytes) -> None:
            pass

    def exec_command(self, command: str):
        proc = subprocess.run(
            command,
            shell=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        ch = self._Channel(proc.returncode)
        return (
            self._Stream(b"", ch),
            self._Stream(proc.stdout, ch),
            self._Stream(proc.stderr, ch),
        )

    def open_sftp(self) -> _LocalSftpClient:
        return _LocalSftpClient()

    def close(self) -> None:
        pass


@contextmanager
def managed_ssh(cfg: BeaconRemoteConfig):
    """Open an SSH connection, or fall back to local execution when on the same host."""
    if _is_local_host(cfg.host):
        LOGGER.info("Local exec mode: %s resolves to this machine — skipping SSH", cfg.host)
        yield _LocalShellClient()
        return

    client = _open_ssh_client(cfg)
    try:
        yield client
    finally:
        client.close()


def _open_ssh_client(cfg: BeaconRemoteConfig):
    import logging as _logging
    import paramiko

    # Suppress paramiko's transport-level INFO messages (Connected, Auth banner).
    _logging.getLogger("paramiko.transport").setLevel(_logging.WARNING)

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
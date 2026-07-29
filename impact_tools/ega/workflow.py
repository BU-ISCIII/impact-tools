"""End-to-end Crypt4GH encryption and LocalEGA Inbox upload workflow."""

from __future__ import annotations

import json
import time
from dataclasses import asdict, dataclass, replace
from pathlib import Path

from impact_tools.ega.encrypt import EncryptionConfig, EncryptionResult, run_encryption
from impact_tools.ega.execution import collect_execution_environment, make_run_id
from impact_tools.ega.html_report import write_workflow_report
from impact_tools.ega.upload_inbox import (
    InboxUploadConfig,
    InboxUploadResult,
    run_inbox_upload,
)


UPLOADABLE_ENCRYPTION_STATUSES = {
    "ok",
    "skipped_existing",
    "skipped_registered",
}


@dataclass(frozen=True)
class EncryptUploadConfig:
    """Settings shared by the encryption and upload stages."""

    encryption: EncryptionConfig
    upload: InboxUploadConfig
    output_dir: Path
    run_profile: str = "local"
    generate_report_charts: bool = True


@dataclass(frozen=True)
class EncryptUploadResult:
    """Combined result and report for an end-to-end run."""

    run_id: str
    output_dir: Path
    manifest_file: Path
    report_file: Path
    upload_input_list: Path
    upload_source_dir: Path
    encryption: EncryptionResult
    upload: InboxUploadResult
    wall_seconds: float


def run_encrypt_upload(config: EncryptUploadConfig) -> EncryptUploadResult:
    """Encrypt selected inputs and upload exactly their resulting files."""
    output_dir = config.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    run_id = make_run_id()
    start = time.perf_counter()

    encryption = run_encryption(
        replace(config.encryption, run_profile=config.run_profile)
    )
    if encryption.failed:
        raise RuntimeError(
            f"Encryption produced {encryption.failed} failed file(s); upload aborted"
        )

    upload_source_dir = output_dir / f"upload_sources_{run_id}"
    upload_files = _prepare_upload_sources(
        encryption.manifest_file,
        upload_source_dir,
    )
    if not upload_files:
        raise RuntimeError("Encryption did not produce any uploadable files")
    _validate_upload_layout(upload_files, config.upload.remote_layout)

    upload_input_list = output_dir / f"upload_inputs_{run_id}.txt"
    upload_input_list.write_text(
        "".join(f"{path}\n" for path in upload_files),
        encoding="utf-8",
    )
    upload = run_inbox_upload(
        replace(
            config.upload,
            input_dir=upload_source_dir,
            input_list=upload_input_list,
            run_profile=config.run_profile,
        )
    )
    wall_seconds = time.perf_counter() - start
    manifest_file = output_dir / f"encrypt_upload_manifest_{run_id}.json"
    report_file = output_dir / f"encrypt_upload_report_{run_id}.html"
    payload = {
        "run_id": run_id,
        "execution": collect_execution_environment(config.run_profile).as_dict(),
        "wall_seconds": round(wall_seconds, 6),
        "report_file": str(report_file),
        "upload_input_list": str(upload_input_list),
        "upload_source_dir": str(upload_source_dir),
        "encryption": _result_payload(encryption),
        "upload": _result_payload(upload),
    }
    manifest_file.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    encryption_payload = json.loads(
        encryption.manifest_file.read_text(encoding="utf-8")
    )
    upload_payload = json.loads(upload.manifest_file.read_text(encoding="utf-8"))
    write_workflow_report(
        report_file,
        payload,
        encryption_payload.get("files", []),
        upload_payload.get("files", []),
        include_charts=config.generate_report_charts,
    )
    return EncryptUploadResult(
        run_id=run_id,
        output_dir=output_dir,
        manifest_file=manifest_file,
        report_file=report_file,
        upload_input_list=upload_input_list,
        upload_source_dir=upload_source_dir,
        encryption=encryption,
        upload=upload,
        wall_seconds=wall_seconds,
    )


def _prepare_upload_sources(manifest_file: Path, source_dir: Path) -> list[Path]:
    """Create a closed, relative upload tree without duplicating encrypted data."""
    payload = json.loads(manifest_file.read_text(encoding="utf-8"))
    outputs: list[Path] = []
    for record in payload.get("files", []):
        if record.get("status") not in UPLOADABLE_ENCRYPTION_STATUSES:
            continue
        output = Path(record["output_file"]).resolve()
        if not output.is_file():
            continue
        relative_output = Path(record.get("sample_id") or "") / output.name
        if relative_output.is_absolute() or ".." in relative_output.parts:
            raise ValueError(
                f"Unsafe sample path in encryption manifest: {relative_output}"
            )
        staged_output = source_dir / relative_output
        staged_output.parent.mkdir(parents=True, exist_ok=True)
        staged_output.symlink_to(output)
        outputs.append(staged_output.absolute())
    return outputs


def _validate_upload_layout(upload_files: list[Path], remote_layout: str) -> None:
    """Prevent different sample files from colliding in a flat Inbox."""
    if remote_layout != "flat":
        return
    by_name: dict[str, list[Path]] = {}
    for path in upload_files:
        by_name.setdefault(path.name, []).append(path)
    collisions = {
        name: paths
        for name, paths in by_name.items()
        if len(paths) > 1
    }
    if not collisions:
        return
    details = "; ".join(
        f"{name}: {', '.join(str(path) for path in paths)}"
        for name, paths in sorted(collisions.items())
    )
    raise ValueError(
        "Flat Inbox layout would overwrite files with the same basename. "
        "Use --remote-layout relative or unique per-sample filenames. "
        f"Collisions: {details}"
    )


def _result_payload(result) -> dict:
    payload = asdict(result)
    return {
        key: str(value) if isinstance(value, Path) else value
        for key, value in payload.items()
    }

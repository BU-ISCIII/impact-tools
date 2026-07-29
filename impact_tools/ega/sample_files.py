"""Strict per-sample CRAM and VCF discovery for EGA workflows."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class SampleFile:
    """One input file selected for a sample."""

    sample_id: str
    role: str
    path: Path


def discover_sample_files(
    input_dir: Path,
    output_dir: Path,
    sample_list: Path,
) -> list[SampleFile]:
    """Resolve exactly one CRAM and one VCF for every requested sample.

    Sample identifiers are read from ``sample_list``. The preferred layout is
    ``input_dir/<sample_id>/``. Nested sample directories are supported when
    their basename is the sample identifier, and a flat directory is supported
    when files use the exact names ``<sample_id>.cram`` and
    ``<sample_id>.vcf[.gz]``.
    """
    sample_ids = read_sample_ids(sample_list)
    selections: list[SampleFile] = []
    sample_roots, root_errors = _find_sample_roots(
        input_dir=input_dir,
        output_dir=output_dir,
        sample_ids=sample_ids,
    )
    flat_files = _index_flat_files(
        input_dir=input_dir,
        output_dir=output_dir,
        sample_ids=[
            sample_id
            for sample_id in sample_ids
            if sample_roots.get(sample_id) is None
            and sample_id not in root_errors
        ],
    )
    errors = list(root_errors.values())
    selected_paths: dict[Path, str] = {}

    for sample_id in sample_ids:
        if sample_id in root_errors:
            continue
        try:
            sample_files = _discover_one_sample(
                sample_id=sample_id,
                sample_root=sample_roots.get(sample_id),
                flat_files=flat_files,
                output_dir=output_dir,
            )
        except ValueError as exc:
            errors.append(str(exc))
            continue

        for selected in sample_files:
            file_identity = selected.path.resolve()
            previous_sample = selected_paths.get(file_identity)
            if previous_sample is not None:
                errors.append(
                    f"Sample {sample_id!r}: {selected.path} was also selected "
                    f"for sample {previous_sample!r}"
                )
                continue
            selected_paths[file_identity] = sample_id
            selections.append(selected)

    if errors:
        details = "\n".join(f"- {error}" for error in errors)
        raise ValueError(
            "CRAM/VCF discovery failed for the requested samples:\n" + details
        )
    return selections


def read_sample_ids(sample_list: Path) -> list[str]:
    """Read unique, safe sample identifiers from a text file."""
    path = sample_list.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Sample list does not exist: {path}")

    sample_ids: list[str] = []
    seen: set[str] = set()
    errors: list[str] = []
    with path.open("r", encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            sample_id = raw_line.strip()
            if not sample_id or sample_id.startswith("#"):
                continue
            if not _safe_sample_id(sample_id):
                errors.append(
                    f"{path}:{line_number}: unsafe sample identifier "
                    f"{sample_id!r}; identifiers cannot contain path separators"
                )
                continue
            if sample_id in seen:
                errors.append(
                    f"{path}:{line_number}: duplicate sample identifier "
                    f"{sample_id!r}"
                )
                continue
            seen.add(sample_id)
            sample_ids.append(sample_id)

    if errors:
        raise ValueError("\n".join(errors))
    if not sample_ids:
        raise ValueError(f"Sample list contains no sample identifiers: {path}")
    return sample_ids


def _discover_one_sample(
    sample_id: str,
    sample_root: Path | None,
    flat_files: dict[str, list[Path]],
    output_dir: Path,
) -> list[SampleFile]:
    if sample_root is None:
        cram_candidates = flat_files.get(f"{sample_id}.cram", [])
        vcf_candidates = [
            *flat_files.get(f"{sample_id}.vcf.gz", []),
            *flat_files.get(f"{sample_id}.vcf", []),
        ]
    else:
        cram_candidates, vcf_candidates = _role_candidates(
            sample_root=sample_root,
            output_dir=output_dir,
        )

    cram = _require_one(sample_id, "CRAM", cram_candidates)
    vcf = _require_one(sample_id, "VCF", vcf_candidates)
    return [
        SampleFile(sample_id=sample_id, role="cram", path=cram),
        SampleFile(sample_id=sample_id, role="vcf", path=vcf),
    ]


def _find_sample_roots(
    input_dir: Path,
    output_dir: Path,
    sample_ids: list[str],
) -> tuple[dict[str, Path | None], dict[str, str]]:
    roots: dict[str, Path | None] = {}
    unresolved: set[str] = set()
    for sample_id in sample_ids:
        if input_dir.name == sample_id:
            roots[sample_id] = input_dir
            continue
        direct = input_dir / sample_id
        if direct.is_dir() and not _inside_output(direct, output_dir):
            roots[sample_id] = direct
            continue
        roots[sample_id] = None
        unresolved.add(sample_id)

    nested: dict[str, list[Path]] = {sample_id: [] for sample_id in unresolved}
    if unresolved:
        for path in input_dir.rglob("*"):
            if (
                path.is_dir()
                and path.name in unresolved
                and not _inside_output(path, output_dir)
            ):
                nested[path.name].append(path)

    errors: dict[str, str] = {}
    for sample_id, matches in nested.items():
        matches.sort()
        if len(matches) == 1:
            roots[sample_id] = matches[0]
        elif len(matches) > 1:
            rendered = ", ".join(str(path) for path in matches)
            errors[sample_id] = (
                f"Sample {sample_id!r}: multiple sample directories found: "
                f"{rendered}"
            )
    return roots, errors


def _role_candidates(
    sample_root: Path,
    output_dir: Path,
) -> tuple[list[Path], list[Path]]:
    files = sorted(
        path
        for path in sample_root.rglob("*")
        if path.is_file()
        and not _inside_output(path, output_dir)
    )
    cram = [path for path in files if path.name.endswith(".cram")]
    vcf = [
        path
        for path in files
        if path.name.endswith(".vcf") or path.name.endswith(".vcf.gz")
    ]
    return cram, vcf


def _index_flat_files(
    input_dir: Path,
    output_dir: Path,
    sample_ids: list[str],
) -> dict[str, list[Path]]:
    wanted = {
        filename
        for sample_id in sample_ids
        for filename in (
            f"{sample_id}.cram",
            f"{sample_id}.vcf.gz",
            f"{sample_id}.vcf",
        )
    }
    matches: dict[str, list[Path]] = {}
    if not wanted:
        return matches
    for path in input_dir.rglob("*"):
        if (
            path.is_file()
            and path.name in wanted
            and not _inside_output(path, output_dir)
        ):
            matches.setdefault(path.name, []).append(path)
    for paths in matches.values():
        paths.sort()
    return matches


def _require_one(sample_id: str, label: str, candidates: list[Path]) -> Path:
    if not candidates:
        raise ValueError(f"Sample {sample_id!r}: no {label} file found")
    if len(candidates) > 1:
        rendered = ", ".join(str(path) for path in candidates)
        raise ValueError(
            f"Sample {sample_id!r}: multiple {label} files found: {rendered}"
        )
    candidate = candidates[0]
    if candidate.stat().st_size == 0:
        raise ValueError(f"Sample {sample_id!r}: {label} file is empty: {candidate}")
    return candidate


def _inside_output(path: Path, output_dir: Path) -> bool:
    lexical_path = path.absolute()
    lexical_output = output_dir.absolute()
    resolved_path = path.resolve(strict=False)
    resolved_output = output_dir.resolve(strict=False)
    return (
        lexical_path.is_relative_to(lexical_output)
        or resolved_path.is_relative_to(resolved_output)
    )


def _safe_sample_id(sample_id: str) -> bool:
    return (
        sample_id not in {".", ".."}
        and "/" not in sample_id
        and "\\" not in sample_id
        and "\x00" not in sample_id
    )

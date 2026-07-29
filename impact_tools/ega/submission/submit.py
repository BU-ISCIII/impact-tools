"""Submit prepared EGA metadata drafts to the Submitter Portal API."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


@dataclass(frozen=True)
class SubmitSubmissionConfig:
    """Configuration for submitter-portal draft submission."""

    draft_file: Path
    output_dir: Path
    api_base: str | None = None
    token: str | None = None
    token_file: Path | None = None
    resume_state_file: Path | None = None
    submission_id: str | None = None
    execute: bool = False
    timeout_seconds: float = 60.0
    verify_tls: bool = True


@dataclass(frozen=True)
class SubmitSubmissionResult:
    """Files written by submit-submission."""

    output_dir: Path
    payload_dir: Path
    response_dir: Path
    state_file: Path
    plan_file: Path


@dataclass(frozen=True)
class SubmissionStep:
    """One ordered API submission step."""

    name: str
    method: str
    path: str
    payload: dict[str, Any]
    enabled: bool = True
    entity: str | None = None
    entity_key: str | None = None


def submit_submission(config: SubmitSubmissionConfig) -> SubmitSubmissionResult:
    """Create payloads from a prepared draft and optionally submit them."""
    draft_path = config.draft_file.expanduser().resolve()
    if not draft_path.is_file():
        raise FileNotFoundError(f"Draft file does not exist: {draft_path}")

    draft = _read_mapping_file(draft_path)
    draft_sha256 = hashlib.sha256(draft_path.read_bytes()).hexdigest()
    output_dir = config.output_dir.expanduser().resolve()
    payload_dir = output_dir / "payloads"
    response_dir = output_dir / "responses"
    output_dir.mkdir(parents=True, exist_ok=True)
    payload_dir.mkdir(parents=True, exist_ok=True)
    response_dir.mkdir(parents=True, exist_ok=True)

    token = _read_token(config.token, config.token_file)
    if config.execute and not token:
        raise ValueError("--execute requires --token or --token-file")
    if config.execute and not config.api_base:
        raise ValueError("--execute requires --api-base")

    state_file = output_dir / "submission_state.json"
    state: dict[str, Any] = {
        "mode": "execute" if config.execute else "dry-run",
        "api_base": config.api_base,
        "draft_file": str(draft_path),
        "draft_sha256": draft_sha256,
        "ids": {},
        "files": {},
        "steps": [],
    }
    _apply_resume_state(state, config.resume_state_file)
    if config.submission_id:
        state.setdefault("ids", {})["submission"] = str(config.submission_id)

    steps = _build_steps(draft, state)
    _write_payloads(payload_dir, steps, state)
    _write_plan(output_dir / "submission_plan.json", steps, state)

    try:
        if config.execute:
            _validate_execute_plan(steps, state)
            _validate_execute_payloads(steps, state)
            _execute_steps(config, token, response_dir, steps, state)
        else:
            _apply_dry_run_ids(steps, state)
    finally:
        state_file.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")

    return SubmitSubmissionResult(
        output_dir=output_dir,
        payload_dir=payload_dir,
        response_dir=response_dir,
        state_file=state_file,
        plan_file=output_dir / "submission_plan.json",
    )


def _build_steps(draft: dict[str, Any], state: dict[str, Any]) -> list[SubmissionStep]:
    submission = _clean_payload(draft.get("submission") or {})
    study = _clean_payload(draft.get("study") or {})
    samples = _draft_entities(draft, "sample", "samples")
    experiments = _draft_entities(draft, "experiment", "experiments")
    runs = _draft_entities(draft, "run", "runs")
    analyses = _draft_entities(draft, "analysis", "analyses", required=False)
    analyses = [(key, payload) for key, payload in analyses if payload.get("files")]
    _validate_entity_keys(samples, experiments, runs, analyses)
    dataset = _clean_payload(draft.get("dataset") or {})

    submission_id = _state_id(state, "submission", "{submission_provisional_id}")
    study_id = _state_id(state, "study", "{study_provisional_id}")
    existing_ids = state.get("ids", {})
    expected_files: list[dict[str, Any]] = []

    sample_steps = []
    for index, (key, sample) in enumerate(samples, start=1):
        state_key = _entity_state_key("sample", key)
        sample_steps.append(
            SubmissionStep(
                _entity_step_name("03_sample", index, len(samples)),
                "POST",
                f"/submissions/{submission_id}/samples",
                sample,
                enabled=bool(sample) and state_key not in existing_ids,
                entity="sample",
                entity_key=key,
            )
        )

    experiment_steps = []
    for index, (key, experiment) in enumerate(experiments, start=1):
        if "study_accession_id" not in experiment and "study_provisional_id" not in experiment:
            experiment["study_provisional_id"] = study_id
        state_key = _entity_state_key("experiment", key)
        experiment_steps.append(
            SubmissionStep(
                _entity_step_name("04_experiment", index, len(experiments)),
                "POST",
                f"/submissions/{submission_id}/experiments",
                experiment,
                enabled=bool(experiment) and state_key not in existing_ids,
                entity="experiment",
                entity_key=key,
            )
        )

    run_steps = []
    run_ids = []
    for index, (key, run) in enumerate(runs, start=1):
        run_expected = _expected_entity_files(run, entity="run", entity_key=key)
        expected_files.extend(run_expected)
        run["files"] = [_file_placeholder(item["lookup_name"]) for item in run_expected]
        if "experiment_accession_id" not in run and "experiment_provisional_id" not in run:
            run["experiment_provisional_id"] = _entity_placeholder("experiment", key)
        if "sample_accession_id" not in run and "sample_provisional_id" not in run:
            run["sample_provisional_id"] = _entity_placeholder("sample", key)
        state_key = _entity_state_key("run", key)
        run_ids.append(_entity_placeholder("run", key))
        run_steps.append(
            SubmissionStep(
                _entity_step_name("06_run", index, len(runs)),
                "POST",
                f"/submissions/{submission_id}/runs",
                run,
                enabled=bool(run) and state_key not in existing_ids,
                entity="run",
                entity_key=key,
            )
        )

    analysis_steps = []
    analysis_ids = []
    for index, (key, analysis) in enumerate(analyses, start=1):
        analysis_expected = _expected_entity_files(analysis, entity="analysis", entity_key=key)
        expected_files.extend(analysis_expected)
        analysis["files"] = [_file_placeholder(item["lookup_name"]) for item in analysis_expected]
        if "study_accession_id" not in analysis and "study_provisional_id" not in analysis:
            analysis["study_provisional_id"] = study_id
        if "experiment_accession_ids" not in analysis and "experiment_provisional_ids" not in analysis:
            analysis["experiment_provisional_ids"] = [_entity_placeholder("experiment", key)]
        if "sample_accession_ids" not in analysis and "sample_provisional_ids" not in analysis:
            analysis["sample_provisional_ids"] = [_entity_placeholder("sample", key)]
        state_key = _entity_state_key("analysis", key)
        analysis_ids.append(_entity_placeholder("analysis", key))
        analysis_steps.append(
            SubmissionStep(
                _entity_step_name("07_analysis", index, len(analyses)),
                "POST",
                f"/submissions/{submission_id}/analyses",
                analysis,
                enabled=bool(analysis) and state_key not in existing_ids,
                entity="analysis",
                entity_key=key,
            )
        )

    state["expected_files"] = expected_files
    if "run_accession_ids" not in dataset and "run_provisional_ids" not in dataset:
        dataset["run_provisional_ids"] = run_ids
    if analysis_ids and "analysis_accession_ids" not in dataset and "analysis_provisional_ids" not in dataset:
        dataset["analysis_provisional_ids"] = analysis_ids

    return [
        SubmissionStep(
            "01_submission",
            "POST",
            "/submissions",
            submission,
            enabled=bool(submission) and "submission" not in existing_ids,
            entity="submission",
        ),
        SubmissionStep(
            "02_study",
            "POST",
            f"/submissions/{submission_id}/studies",
            study,
            enabled=bool(study) and "study" not in existing_ids,
            entity="study",
        ),
        *sample_steps,
        *experiment_steps,
        SubmissionStep(
            "05_resolve_files",
            "GET",
            "/files",
            {"expected_files": expected_files, "status": "inbox"},
            enabled=bool(expected_files) and not _expected_files_already_resolved(expected_files, state),
        ),
        *run_steps,
        *analysis_steps,
        SubmissionStep(
            "08_dataset",
            "POST",
            f"/submissions/{submission_id}/datasets",
            dataset,
            enabled=bool(dataset) and "dataset" not in existing_ids,
            entity="dataset",
        ),
    ]


def _draft_entities(
    draft: dict[str, Any],
    singular: str,
    plural: str,
    *,
    required: bool = True,
) -> list[tuple[str | None, dict[str, Any]]]:
    is_batch = plural in draft
    raw_items = draft.get(plural) if is_batch else [draft.get(singular)]
    if raw_items is None:
        raw_items = []
    if not isinstance(raw_items, list):
        raise ValueError(f"Draft field {plural!r} must be a list")
    entities = []
    seen = set()
    for index, raw_item in enumerate(raw_items, start=1):
        if raw_item is None and not is_batch:
            raw_item = {}
        if not isinstance(raw_item, dict):
            raise ValueError(f"Draft field {plural}[{index}] must be a mapping")
        item = _clean_payload(raw_item)
        key_value = item.pop("_key", None)
        key = str(key_value) if key_value is not None else None
        if is_batch and not key:
            raise ValueError(f"Draft field {plural}[{index}] requires a non-empty _key")
        if key in seen:
            raise ValueError(f"Draft field {plural} contains duplicate _key {key!r}")
        seen.add(key)
        if item or required:
            entities.append((key, item))
    if required and not entities:
        raise ValueError(f"Draft field {plural!r} must contain at least one item")
    return entities


def _expected_run_files(run: dict[str, Any]) -> list[dict[str, Any]]:
    """Backward-compatible alias for callers using the original helper."""
    return _expected_entity_files(run, entity="run", entity_key=None)


def _validate_entity_keys(
    samples: list[tuple[str | None, dict[str, Any]]],
    experiments: list[tuple[str | None, dict[str, Any]]],
    runs: list[tuple[str | None, dict[str, Any]]],
    analyses: list[tuple[str | None, dict[str, Any]]],
) -> None:
    sample_keys = {key for key, _ in samples}
    for label, entities in (
        ("experiments", experiments),
        ("runs", runs),
        ("analyses", analyses),
    ):
        keys = {key for key, _ in entities}
        if keys and keys != sample_keys:
            raise ValueError(
                f"Draft {label} _key values must match samples exactly; "
                f"samples={sorted(str(key) for key in sample_keys)}, "
                f"{label}={sorted(str(key) for key in keys)}"
            )


def _expected_entity_files(
    payload: dict[str, Any],
    *,
    entity: str,
    entity_key: str | None,
) -> list[dict[str, Any]]:
    files = payload.get("files")
    if not isinstance(files, list):
        return []
    expected = []
    for item in files:
        if isinstance(item, int):
            continue
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("relative_path") or item.get("display_name")
        if not name:
            continue
        encrypted_name = name if str(name).endswith(".c4gh") else f"{name}.c4gh"
        expected.append(
            {
                "source_name": name,
                "lookup_name": encrypted_name,
                "unencrypted_md5": item.get("md5"),
                "extension": item.get("extension"),
                "entity": entity,
                "entity_key": entity_key,
            }
        )
    return expected


def _entity_step_name(prefix: str, index: int, count: int) -> str:
    return prefix if count == 1 else f"{prefix}_{index:03d}"


def _entity_state_key(entity: str, key: str | None) -> str:
    return entity if key is None else f"{entity}:{key}"


def _entity_placeholder(entity: str, key: str | None) -> str:
    suffix = "" if key is None else f":{key}"
    return f"{{{entity}_provisional_id{suffix}}}"


def _file_placeholder(name: str) -> str:
    return "{file_provisional_id:" + name + "}"


def _expected_files_already_resolved(expected_files: list[dict[str, Any]], state: dict[str, Any]) -> bool:
    files = state.get("files", {})
    return bool(expected_files) and all(item.get("lookup_name") in files for item in expected_files)


def _validate_execute_plan(steps: list[SubmissionStep], state: dict[str, Any]) -> None:
    existing_ids = state.get("ids", {})
    disabled = []
    for step in steps:
        if step.enabled:
            continue
        if step.name == "05_resolve_files" and _expected_files_already_resolved(
            state.get("expected_files") or [],
            state,
        ):
            continue
        state_key = _entity_state_key(step.entity, step.entity_key) if step.entity else None
        if state_key and state_key in existing_ids:
            continue
        disabled.append(step.name)
    if disabled:
        raise ValueError(
            "Cannot execute submission because required payloads are empty: " + ", ".join(disabled)
        )


def _validate_execute_payloads(steps: list[SubmissionStep], state: dict[str, Any]) -> None:
    errors: list[str] = []
    for step in steps:
        if not step.enabled or step.name == "05_resolve_files":
            continue
        payload = _resolve_placeholders(step.payload, state)
        for path, value in _walk_payload(payload):
            if isinstance(value, str) and value.startswith("EXAMPLE:"):
                errors.append(f"{step.name}.{path} contains placeholder example value: {value!r}")
        if step.name.startswith("04_experiment"):
            errors.extend(_validate_experiment_payload(payload))
        if step.name.startswith("07_analysis"):
            errors.extend(_validate_analysis_payload(payload, step.name))
        if step.name == "08_dataset":
            errors.extend(_validate_dataset_payload(payload))
    if errors:
        raise ValueError("Cannot execute submission because payload validation failed:\n- " + "\n- ".join(errors))


def _validate_experiment_payload(payload: dict[str, Any]) -> list[str]:
    errors = []
    required = [
        "design_description",
        "instrument_model_id",
        "library_layout",
        "library_strategy",
        "library_source",
        "library_selection",
        "study_provisional_id",
    ]
    for key in required:
        if key not in payload:
            errors.append(f"04_experiment.{key} is required")
    if "instrument_model_id" in payload and not isinstance(payload["instrument_model_id"], int):
        errors.append("04_experiment.instrument_model_id must be an integer platform model id")
    if "study_provisional_id" in payload and not _is_integer_reference(payload["study_provisional_id"]):
        errors.append("04_experiment.study_provisional_id must be an integer")
    return errors


def _validate_analysis_payload(payload: dict[str, Any], step_name: str) -> list[str]:
    errors = []
    for key in (
        "title",
        "description",
        "analysis_type",
        "files",
        "experiment_types",
        "genome_id",
        "chromosomes",
        "study_provisional_id",
        "experiment_provisional_ids",
        "sample_provisional_ids",
    ):
        if key not in payload:
            errors.append(f"{step_name}.{key} is required")
    for key in ("files", "experiment_provisional_ids", "sample_provisional_ids"):
        values = payload.get(key)
        if values is not None and not (
            isinstance(values, list)
            and bool(values)
            and all(_is_integer_reference(item) for item in values)
        ):
            errors.append(f"{step_name}.{key} must be a non-empty list of integers")
    if "genome_id" in payload and not isinstance(payload["genome_id"], int):
        errors.append(f"{step_name}.genome_id must be an integer genome id")
    if "analysis_type" in payload and not isinstance(payload["analysis_type"], str):
        errors.append(f"{step_name}.analysis_type must be a string enum value")
    if "description" in payload and not isinstance(payload["description"], str):
        errors.append(f"{step_name}.description must be a string")
    experiment_types = payload.get("experiment_types")
    if experiment_types is not None and not (
        isinstance(experiment_types, list)
        and bool(experiment_types)
        and all(isinstance(item, str) for item in experiment_types)
    ):
        errors.append(f"{step_name}.experiment_types must be a non-empty list of strings")
    chromosomes = payload.get("chromosomes")
    if chromosomes is not None and not (
        isinstance(chromosomes, list)
        and bool(chromosomes)
        and all(_is_id_label_pair(item) for item in chromosomes)
    ):
        errors.append(
            f"{step_name}.chromosomes must be a non-empty list of "
            "[id, label] pairs or id/label mappings"
        )
    return errors


def _is_id_label_pair(value: Any) -> bool:
    if isinstance(value, (list, tuple)):
        return (
            len(value) == 2
            and isinstance(value[0], int)
            and isinstance(value[1], str)
            and bool(value[1].strip())
        )
    if isinstance(value, dict):
        return (
            isinstance(value.get("id"), int)
            and isinstance(value.get("label"), str)
            and bool(value["label"].strip())
        )
    return False


def _validate_dataset_payload(payload: dict[str, Any]) -> list[str]:
    errors = []
    for key in ("title", "description", "dataset_types", "policy_accession_id"):
        if key not in payload:
            errors.append(f"08_dataset.{key} is required")
    dataset_types = payload.get("dataset_types")
    if "dataset_types" in payload and not (
        isinstance(dataset_types, list) and all(isinstance(item, str) for item in dataset_types)
    ):
        errors.append("08_dataset.dataset_types must be a list of strings")
    run_ids = payload.get("run_provisional_ids")
    if run_ids is not None and not (
        isinstance(run_ids, list) and all(_is_integer_reference(item) for item in run_ids)
    ):
        errors.append("08_dataset.run_provisional_ids must be a list of integers")
    analysis_ids = payload.get("analysis_provisional_ids")
    if analysis_ids is not None and not (
        isinstance(analysis_ids, list) and all(_is_integer_reference(item) for item in analysis_ids)
    ):
        errors.append("08_dataset.analysis_provisional_ids must be a list of integers")
    policy_id = payload.get("policy_accession_id")
    if isinstance(policy_id, str) and not policy_id.startswith("EGAP"):
        errors.append("08_dataset.policy_accession_id must be an EGAP accession")
    return errors


def _is_integer_reference(value: Any) -> bool:
    return isinstance(value, int) or (
        isinstance(value, str)
        and value.startswith("{")
        and value.endswith("_provisional_id}")
    ) or (
        isinstance(value, str)
        and "_provisional_id:" in value
        and value.endswith("}")
    )


def _walk_payload(value: Any, prefix: str = "") -> list[tuple[str, Any]]:
    if isinstance(value, dict):
        items = []
        for key, item in value.items():
            child_prefix = f"{prefix}.{key}" if prefix else str(key)
            items.extend(_walk_payload(item, child_prefix))
        return items
    if isinstance(value, list):
        items = []
        for index, item in enumerate(value):
            child_prefix = f"{prefix}[{index}]"
            items.extend(_walk_payload(item, child_prefix))
        return items
    return [(prefix, value)]


def _resolve_files(client: Any, payload: dict[str, Any], state: dict[str, Any]) -> dict[str, Any]:
    expected_files = payload.get("expected_files") or []
    resolved = []
    missing = []
    for expected in expected_files:
        lookup_name = expected["lookup_name"]
        candidates = _fetch_file_candidates(client, lookup_name)
        match = _select_file_candidate(candidates, expected)
        if match is None:
            missing.append({"expected": expected, "candidates": candidates})
            continue
        file_id = match.get("provisional_id")
        if file_id is None:
            missing.append(
                {
                    "expected": expected,
                    "candidates": candidates,
                    "reason": "matched file has no provisional_id",
                }
            )
            continue
        state.setdefault("files", {})[lookup_name] = file_id
        resolved.append({"expected": expected, "file": match, "provisional_id": file_id})
    if missing:
        raise RuntimeError(
            "Could not resolve all Inbox files required by Runs and Analyses: "
            + json.dumps(missing, ensure_ascii=False)[:2000]
        )
    return {"resolved_files": resolved}


def _fetch_file_candidates(client: Any, lookup_name: str) -> list[dict[str, Any]]:
    normalized_name = lookup_name.lstrip("/")
    basename = Path(normalized_name).name
    prefixes = [f"/{normalized_name}", normalized_name]
    if basename != normalized_name:
        prefixes.extend([f"/{basename}", basename])
    if lookup_name.endswith(".c4gh"):
        unencrypted_name = normalized_name.removesuffix(".c4gh")
        prefixes.extend([f"/{unencrypted_name}", unencrypted_name])
    candidates: list[dict[str, Any]] = []
    seen = set()
    for prefix in prefixes:
        response = client.get("/files", params={"status": "inbox", "prefix": prefix})
        response_payload = _read_response_json(response)
        response.raise_for_status()
        _extend_unique_candidates(candidates, seen, _extract_records(response_payload))
        exact_path_matches = [
            candidate
            for candidate in candidates
            if any(
                str(candidate.get(field) or "").lstrip("/") == normalized_name
                for field in ("relative_path", "display_name", "name")
            )
        ]
        if len(exact_path_matches) == 1:
            return exact_path_matches
    if not candidates:
        response = client.get("/files", params={"status": "inbox"})
        response_payload = _read_response_json(response)
        response.raise_for_status()
        inbox_records = _extract_records(response_payload)
        _extend_unique_candidates(
            candidates,
            seen,
            [record for record in inbox_records if _file_candidate_matches_lookup(record, lookup_name)],
        )
    return candidates


def _extract_records(response_payload: dict[str, Any]) -> list[dict[str, Any]]:
    records = (
        response_payload.get("response")
        if isinstance(response_payload.get("response"), list)
        else response_payload
    )
    if isinstance(records, dict):
        records = records.get("items") or records.get("results") or records.get("data") or []
    if not isinstance(records, list):
        return []
    return [record for record in records if isinstance(record, dict)]


def _extend_unique_candidates(candidates: list[dict[str, Any]], seen: set[str], records: list[dict[str, Any]]) -> None:
    for record in records:
        key = json.dumps(record, sort_keys=True, ensure_ascii=False)
        if key in seen:
            continue
        seen.add(key)
        candidates.append(record)


def _file_candidate_matches_lookup(candidate: dict[str, Any], lookup_name: str) -> bool:
    lookup_basename = Path(lookup_name).name
    names = [
        candidate.get("relative_path"),
        candidate.get("display_name"),
        candidate.get("name"),
    ]
    for name in names:
        if not name:
            continue
        name_text = str(name)
        if (
            name_text == lookup_name
            or name_text.endswith(f"/{lookup_name}")
            or Path(name_text).name == lookup_basename
        ):
            return True
    return False


def _select_file_candidate(candidates: list[dict[str, Any]], expected: dict[str, Any]) -> dict[str, Any] | None:
    lookup_name = expected["lookup_name"]
    source_name = expected.get("source_name")
    unencrypted_md5 = expected.get("unencrypted_md5")
    lookup_basename = Path(lookup_name).name
    source_basename = Path(str(source_name)).name if source_name else None
    exact_name_matches = []
    for candidate in candidates:
        names = [
            candidate.get("relative_path"),
            candidate.get("display_name"),
            candidate.get("name"),
        ]
        basenames = [Path(str(name)).name for name in names if name]
        if (
            lookup_name in names
            or lookup_basename in basenames
            or source_name in names
            or source_basename in basenames
        ):
            exact_name_matches.append(candidate)
    if unencrypted_md5:
        checksum_matches = [
            candidate for candidate in exact_name_matches or candidates
            if candidate.get("unencrypted_checksum") == unencrypted_md5
        ]
        if len(checksum_matches) == 1:
            return checksum_matches[0]
    if len(exact_name_matches) == 1:
        return exact_name_matches[0]
    if len(candidates) == 1:
        return candidates[0]
    return None


def _execute_steps(
    config: SubmitSubmissionConfig,
    token: str | None,
    response_dir: Path,
    steps: list[SubmissionStep],
    state: dict[str, Any],
) -> None:
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {token}",
        "User-Agent": "impact-tools",
    }
    try:
        import httpx
    except ImportError as exc:
        raise RuntimeError("httpx is required for --execute. Install project dependencies first.") from exc

    with httpx.Client(
        base_url=(config.api_base or "").rstrip("/"),
        timeout=config.timeout_seconds,
        verify=config.verify_tls,
        headers=headers,
    ) as client:
        for step in steps:
            if not step.enabled:
                _record_step(state, step, skipped=True)
                continue
            path = _resolve_path(step.path, state)
            payload = _resolve_placeholders(step.payload, state)
            if step.name == "05_resolve_files":
                response_payload = _resolve_files(client, payload, state)
                (response_dir / f"{step.name}.json").write_text(
                    json.dumps(response_payload, indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                _record_step(state, step, path=path, status_code=200)
                continue
            response = client.request(step.method, path, json=payload)
            response_payload = _read_response_json(response)
            (response_dir / f"{step.name}.json").write_text(
                json.dumps(response_payload, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            response.raise_for_status()
            _update_state_ids(state, step, response_payload)
            _record_step(state, step, path=path, status_code=response.status_code)


def _apply_dry_run_ids(steps: list[SubmissionStep], state: dict[str, Any]) -> None:
    for step in steps:
        if not step.entity:
            continue
        state_key = _entity_state_key(step.entity, step.entity_key)
        state.setdefault("ids", {}).setdefault(
            state_key,
            f"DRY_RUN_{state_key.upper().replace(':', '_')}_PROVISIONAL_ID",
        )
    expected_files = state.get("expected_files") or []
    state.setdefault("files", {}).update(
        {
            item["lookup_name"]: f"DRY_RUN_FILE_PROVISIONAL_ID_{index}"
            for index, item in enumerate(expected_files, start=1)
        }
    )
    for step in steps:
        _record_step(state, step, path=_resolve_path(step.path, state), skipped=not step.enabled)


def _update_state_ids(state: dict[str, Any], step: SubmissionStep, payload: dict[str, Any]) -> None:
    if not step.entity:
        return
    identifier = _extract_identifier(payload)
    if identifier:
        state_key = _entity_state_key(step.entity, step.entity_key)
        state.setdefault("ids", {})[state_key] = identifier


def _extract_identifier(payload: Any) -> str | None:
    if isinstance(payload, dict):
        for key in ("provisional_id", "accession_id", "id"):
            value = payload.get(key)
            if isinstance(value, str) and value:
                return value
            if isinstance(value, int):
                return str(value)
        for value in payload.values():
            found = _extract_identifier(value)
            if found:
                return found
    if isinstance(payload, list):
        for item in payload:
            found = _extract_identifier(item)
            if found:
                return found
    return None


def _record_step(
    state: dict[str, Any],
    step: SubmissionStep,
    *,
    path: str | None = None,
    status_code: int | None = None,
    skipped: bool = False,
) -> None:
    state.setdefault("steps", []).append(
        {
            "name": step.name,
            "method": step.method,
            "path": path or step.path,
            "enabled": step.enabled,
            "skipped": skipped,
            "status_code": status_code,
        }
    )


def _write_payloads(payload_dir: Path, steps: list[SubmissionStep], state: dict[str, Any]) -> None:
    for step in steps:
        payload = _resolve_placeholders(step.payload, state)
        (payload_dir / f"{step.name}.json").write_text(
            json.dumps(payload, indent=2, ensure_ascii=False),
            encoding="utf-8",
        )


def _write_plan(path: Path, steps: list[SubmissionStep], state: dict[str, Any]) -> None:
    plan = {
        "mode": state["mode"],
        "api_base": state.get("api_base"),
        "steps": [
            {
                "name": step.name,
                "method": step.method,
                "path": step.path,
                "payload_file": f"payloads/{step.name}.json",
                "enabled": step.enabled,
            }
            for step in steps
        ],
    }
    path.write_text(json.dumps(plan, indent=2, ensure_ascii=False), encoding="utf-8")


def _clean_payload(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {}
        for key, item in value.items():
            clean_item = _clean_payload(item)
            if _has_payload_value(clean_item):
                cleaned[key] = clean_item
        return cleaned
    if isinstance(value, list):
        cleaned_list = [_clean_payload(item) for item in value]
        return [item for item in cleaned_list if _has_payload_value(item)]
    return value


def _has_payload_value(value: Any) -> bool:
    if value is None or value == "":
        return False
    if isinstance(value, (list, dict)) and not value:
        return False
    return True


def _state_id(state: dict[str, Any], key: str, placeholder: str) -> str:
    return state.get("ids", {}).get(key) or placeholder


def _resolve_path(path: str, state: dict[str, Any]) -> str:
    return str(_resolve_placeholders(path, state))


def _resolve_placeholders(value: Any, state: dict[str, Any]) -> Any:
    ids = state.get("ids", {})
    replacements = {
        "{submission_provisional_id}": ids.get("submission", "{submission_provisional_id}"),
        "{study_provisional_id}": ids.get("study", "{study_provisional_id}"),
    }
    if isinstance(value, str):
        entity_match = _entity_placeholder_pattern(value)
        if entity_match is not None:
            state_key, original = entity_match
            replacement = ids.get(state_key, original)
            if isinstance(replacement, str) and replacement.isdigit():
                return int(replacement)
            return replacement
        if value in replacements:
            replacement = replacements[value]
            if isinstance(replacement, str) and replacement.isdigit():
                return int(replacement)
            return replacement
        for old, new in replacements.items():
            value = value.replace(old, str(new))
        file_match = _file_placeholder_pattern(value)
        if file_match is not None:
            return state.get("files", {}).get(file_match, value)
        return value
    if isinstance(value, dict):
        return {key: _resolve_placeholders(item, state) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolve_placeholders(item, state) for item in value]
    return value


def _file_placeholder_pattern(value: str) -> str | None:
    prefix = "{file_provisional_id:"
    if value.startswith(prefix) and value.endswith("}"):
        return value[len(prefix):-1]
    return None


def _entity_placeholder_pattern(value: str) -> tuple[str, str] | None:
    if not (value.startswith("{") and value.endswith("}")):
        return None
    content = value[1:-1]
    marker = "_provisional_id"
    if marker not in content:
        return None
    entity, suffix = content.split(marker, 1)
    if entity not in {"sample", "experiment", "run", "analysis"}:
        return None
    key = suffix.removeprefix(":") or None
    return _entity_state_key(entity, key), value


def _read_token(token: str | None, token_file: Path | None) -> str | None:
    if token:
        return token.strip()
    if token_file is None:
        return None
    path = token_file.expanduser().resolve()
    payload = path.read_text(encoding="utf-8").strip()
    if not payload:
        return None
    try:
        parsed = json.loads(payload)
    except json.JSONDecodeError:
        return payload
    if isinstance(parsed, dict):
        for key in ("access_token", "token", "bearer"):
            value = parsed.get(key)
            if isinstance(value, str) and value:
                return value
    raise ValueError(f"Token file does not contain access_token/token/bearer: {path}")


def _apply_resume_state(state: dict[str, Any], resume_state_file: Path | None) -> None:
    if resume_state_file is None:
        return
    path = resume_state_file.expanduser().resolve()
    payload = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Resume state must contain a mapping: {path}")
    if payload.get("mode") != "execute":
        raise ValueError(
            f"Resume state was not produced by an executed submission: {path}"
        )
    resumed_digest = payload.get("draft_sha256")
    current_digest = state.get("draft_sha256")
    if resumed_digest is not None:
        if resumed_digest != current_digest:
            raise ValueError(
                "Resume state belongs to a different submission draft: "
                f"{path}"
            )
    else:
        resumed_draft = payload.get("draft_file")
        if not resumed_draft or Path(resumed_draft).expanduser().resolve() != Path(
            state["draft_file"]
        ).resolve():
            raise ValueError(
                "Legacy resume state cannot be verified against this draft: "
                f"{path}"
            )
    resumed_api = str(payload.get("api_base") or "").rstrip("/")
    current_api = str(state.get("api_base") or "").rstrip("/")
    if resumed_api and current_api and resumed_api != current_api:
        raise ValueError(
            "Resume state belongs to a different Submitter Portal API: "
            f"{resumed_api}"
        )
    ids = payload.get("ids")
    if isinstance(ids, dict):
        state.setdefault("ids", {}).update({key: str(value) for key, value in ids.items() if value is not None})
    files = payload.get("files")
    if isinstance(files, dict):
        state.setdefault("files", {}).update(files)
    state["resume_state_file"] = str(path)


def _read_response_json(response: httpx.Response) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as exc:
        raise RuntimeError(
            "Submitter Portal API returned a non-JSON response.\n"
            f"URL: {response.request.url}\n"
            f"Response: {response.text[:1000]}"
        ) from exc
    if not isinstance(payload, dict):
        return {"response": payload}
    return payload


def _read_mapping_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".json":
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:  # pragma: no cover - project depends on PyYAML
            raise RuntimeError("PyYAML is required to read YAML draft files.") from exc
        payload = yaml.safe_load(text) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Draft submission must contain a mapping: {path}")
    return payload

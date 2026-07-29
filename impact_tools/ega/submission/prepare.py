"""Prepare EGA submitter-portal draft metadata from provider deliveries."""

from __future__ import annotations

import csv
import gzip
import hashlib
import json
import re
from dataclasses import dataclass
from importlib import resources
from pathlib import Path
from typing import Any

from impact_tools.config import user_submission_profile_path
from impact_tools.ega.sample_files import discover_sample_files


DIRECT = "directly_extracted"
INFERRED = "inferred"
NOT_FOUND = "not_found"
NOT_APPLICABLE = "not_applicable"
API_GENERATED = "api_generated"
PROFILE_DEFAULT = "profile_default"
EXAMPLE_VALUE = "example_value"


@dataclass(frozen=True)
class PrepareSubmissionConfig:
    """Configuration for local submission draft preparation."""

    input_dir: Path
    output_dir: Path
    provider: str = "cnio"
    sample_id: str | None = None
    sample_list: Path | None = None
    metadata_file: Path | None = None
    profile_file: Path | None = None
    include_examples: bool = False


@dataclass(frozen=True)
class PrepareSubmissionResult:
    """Files written by submission draft preparation."""

    output_dir: Path
    evidence_file: Path
    evidence_json: Path
    draft_file: Path
    missing_file: Path
    inventory_file: Path


@dataclass(frozen=True)
class Evidence:
    """Single field evidence record."""

    field: str
    value: Any
    status: str
    confidence: str
    evidence_file: str
    location: str
    notes: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value": self.value,
            "status": self.status,
            "confidence": self.confidence,
            "evidence_file": self.evidence_file,
            "location": self.location,
            "notes": self.notes,
        }


def prepare_submission(config: PrepareSubmissionConfig) -> PrepareSubmissionResult:
    """Prepare evidence, draft metadata and missing-field reports."""
    provider = config.provider.lower().replace("-", "_")
    if provider not in {"cnio", "go_impact_cnio"}:
        raise ValueError(f"Unsupported submission provider profile: {config.provider}")

    input_dir = config.input_dir.expanduser().resolve()
    if not input_dir.is_dir():
        raise FileNotFoundError(f"Input directory does not exist: {input_dir}")

    output_dir = config.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    profile_defaults = _read_profile_defaults(provider, config.profile_file)
    if config.sample_list is not None:
        if config.sample_id is not None:
            raise ValueError("--sample-id and --sample-list are mutually exclusive")
        prepared = _prepare_sample_batch(config, input_dir, output_dir, profile_defaults)
        evidence_payload = [
            {
                "sample_id": sample_id,
                "evidence": [item.as_dict() for item in evidence],
            }
            for sample_id, _, evidence, _ in prepared
        ]
        draft = _batch_draft(prepared)
        missing = {
            sample_id: _missing_required_fields(evidence)
            for sample_id, _, evidence, _ in prepared
        }
        evidence_markdown = "\n\n".join(
            _render_evidence_markdown(sample_dir, sample_id, evidence)
            for sample_id, sample_dir, evidence, _ in prepared
        )
        inventory = _file_inventory_recursive(input_dir, output_dir)
    else:
        sample_id = config.sample_id or input_dir.name
        sample_dir, evidence, draft = _prepare_one_sample(
            input_dir,
            sample_id,
            config.metadata_file,
            profile_defaults,
            config.include_examples,
        )
        evidence_payload = [item.as_dict() for item in evidence]
        missing = _missing_required_fields(evidence)
        evidence_markdown = _render_evidence_markdown(sample_dir, sample_id, evidence)
        inventory = _file_inventory(input_dir)

    evidence_json = output_dir / "evidence.json"
    evidence_file = output_dir / "evidence.md"
    draft_file = output_dir / "draft_submission.yaml"
    missing_file = output_dir / "missing_fields.yaml"
    inventory_file = output_dir / "file_inventory.tsv"

    evidence_json.write_text(
        json.dumps(evidence_payload, indent=2, ensure_ascii=False),
        encoding="utf-8",
    )
    evidence_file.write_text(evidence_markdown, encoding="utf-8")
    draft_file.write_text(_render_yaml(draft), encoding="utf-8")
    missing_file.write_text(_render_yaml(missing), encoding="utf-8")
    inventory_file.write_text(_render_inventory(inventory), encoding="utf-8")

    return PrepareSubmissionResult(
        output_dir=output_dir,
        evidence_file=evidence_file,
        evidence_json=evidence_json,
        draft_file=draft_file,
        missing_file=missing_file,
        inventory_file=inventory_file,
    )


def _prepare_sample_batch(
    config: PrepareSubmissionConfig,
    input_dir: Path,
    output_dir: Path,
    profile_defaults: dict[str, Any],
) -> list[tuple[str, Path, list[Evidence], dict[str, Any]]]:
    selected = discover_sample_files(
        input_dir=input_dir,
        output_dir=output_dir,
        sample_list=config.sample_list,
    )
    by_sample: dict[str, dict[str, Path]] = {}
    for item in selected:
        by_sample.setdefault(item.sample_id, {})[item.role] = item.path

    prepared = []
    for sample_id, selected_files in by_sample.items():
        sample_dir = selected_files["cram"].parent
        prepared_sample_dir, evidence, draft = _prepare_one_sample(
            sample_dir,
            sample_id,
            config.metadata_file,
            profile_defaults,
            config.include_examples,
            selected_files=selected_files,
        )
        # The sample-list workflow intentionally encrypts and uploads the CRAM
        # and VCF only. Keep an optional CRAI in single-sample legacy drafts,
        # but do not create an Inbox dependency that the batch did not upload.
        draft["run"]["files"] = draft["run"]["files"][:1]
        draft["run"]["extra_attributes"] = [
            item
            for item in (draft["run"].get("extra_attributes") or [])
            if item.get("tag") not in {"index_file_name", "unencrypted_crai_md5"}
        ] or None
        for section in ("run", "analysis"):
            for file_item in draft[section].get("files") or []:
                name = file_item.get("name")
                if name and Path(str(name)).parent == Path("."):
                    file_item["name"] = f"{sample_id}/{name}"
        prepared.append((sample_id, prepared_sample_dir, evidence, draft))
    return prepared


def _prepare_one_sample(
    input_dir: Path,
    sample_id: str,
    metadata_file: Path | None,
    profile_defaults: dict[str, Any],
    include_examples: bool,
    *,
    selected_files: dict[str, Path] | None = None,
) -> tuple[Path, list[Evidence], dict[str, Any]]:
    files = _cnio_files(input_dir, sample_id)
    if selected_files:
        files.update(selected_files)
        cram = files["cram"]
        files["crai"] = _find_cram_index(cram)
    else:
        files["vcf"] = _find_vcf(input_dir, sample_id)
    provider_metadata = _read_provider_metadata(metadata_file, sample_id)
    evidence = _cnio_evidence(input_dir, sample_id, files, provider_metadata)
    evidence = _merge_profile_default_evidence(evidence, profile_defaults)
    if include_examples:
        evidence = _merge_profile_example_evidence(evidence, profile_defaults)
    draft = _draft_from_evidence(
        sample_id,
        evidence,
        profile_defaults,
        vcf_file=files.get("vcf"),
    )
    return input_dir, evidence, draft


def _find_cram_index(cram: Path) -> Path:
    candidates = [
        Path(f"{cram}.crai"),
        cram.with_suffix(".crai"),
    ]
    return next((path for path in candidates if path.is_file()), candidates[0])


def _find_vcf(input_dir: Path, sample_id: str) -> Path:
    candidates = [
        input_dir / f"{sample_id}.vcf.gz",
        input_dir / f"{sample_id}.vcf",
    ]
    existing = [path for path in candidates if path.is_file()]
    if len(existing) > 1:
        raise ValueError(
            f"Sample {sample_id!r}: multiple VCF files found: "
            + ", ".join(str(path) for path in existing)
        )
    return existing[0] if existing else candidates[0]


def _cnio_files(input_dir: Path, sample_id: str) -> dict[str, Path]:
    names = {
        "cram": f"{sample_id}.cram",
        "crai": f"{sample_id}.cram.crai",
        "vcf": f"{sample_id}.vcf.gz",
        "cram_header": f"{sample_id}.cram.header.sam",
        "cram_md5": f"{sample_id}.cram.md5sum",
        "checksum_md5": "checksum.md5",
        "metrics_json": f"{sample_id}.metrics.json",
        "mapping_metrics": f"{sample_id}.mapping_metrics.csv",
        "insert_stats": f"{sample_id}.insert-stats.tab",
        "fastqc_metrics": f"{sample_id}.fastqc_metrics.csv",
        "wgs_coverage_metrics": f"{sample_id}.wgs_coverage_metrics.csv",
        "wgs_overall_mean_cov": f"{sample_id}.wgs_overall_mean_cov.csv",
        "targeted_json": f"{sample_id}.targeted.json",
        "replay_json": f"{sample_id}-replay.json",
    }
    return {key: input_dir / value for key, value in names.items()}


def _cnio_evidence(
    input_dir: Path,
    sample_id: str,
    files: dict[str, Path],
    provider_metadata: dict[str, Any],
) -> list[Evidence]:
    rg = _read_groups(files["cram_header"])
    pg = _program_records(files["cram_header"])
    checksums = _read_checksums(files["cram_md5"], files["checksum_md5"])
    metrics = _read_json(files["metrics_json"])
    targeted = _read_json(files["targeted_json"])
    replay = _read_json(files["replay_json"])
    mapping_rows = _read_csv_rows(files["mapping_metrics"])
    insert_rows = _read_tsv_rows(files["insert_stats"])
    fastqc_rows = _read_csv_rows(files["fastqc_metrics"])
    wgs_coverage_rows = _read_csv_rows(files["wgs_coverage_metrics"])
    wgs_overall_rows = _read_csv_rows(files["wgs_overall_mean_cov"])

    evidence: list[Evidence] = []
    evidence.extend(_provider_metadata_evidence(provider_metadata))
    evidence.append(_sample_alias(sample_id, rg, files["cram_header"]))
    evidence.append(_metadata_or_missing("SampleRequest.subject_id", provider_metadata, "subject_id"))
    evidence.append(_biological_sex(provider_metadata, mapping_rows, files["mapping_metrics"]))
    evidence.append(_phenotype(provider_metadata, targeted, files["targeted_json"]))
    evidence.append(_instrument_model(provider_metadata, rg, files["cram_header"]))
    evidence.append(_library_layout(mapping_rows, files["mapping_metrics"]))
    evidence.append(_library_strategy(metrics, files["metrics_json"]))
    evidence.append(_metadata_or_missing("ExperimentRequest.library_source", provider_metadata, "library_source"))
    evidence.append(_metadata_or_missing("ExperimentRequest.library_selection", provider_metadata, "library_selection"))
    evidence.append(_library_name(rg, files["cram_header"]))
    evidence.append(
        _metadata_or_missing(
            "ExperimentRequest.library_construction_protocol",
            provider_metadata,
            "library_construction_protocol",
        )
    )
    evidence.append(_paired_nominal_length(mapping_rows, insert_rows, files["mapping_metrics"], files["insert_stats"]))
    evidence.append(_paired_nominal_sdev(mapping_rows, insert_rows, files["mapping_metrics"], files["insert_stats"]))
    evidence.extend(_run_files(files, checksums))
    evidence.append(_analysis_file_evidence(files.get("vcf")))
    evidence.append(_reference(metrics, targeted, pg, files["metrics_json"], files["targeted_json"], files["cram_header"]))
    evidence.extend(_pipeline(metrics, replay, pg, files["metrics_json"], files["replay_json"], files["cram_header"]))
    evidence.append(_sequencing_run_ids(provider_metadata, rg, files["cram_header"], input_dir))
    evidence.append(_flowcell_id(provider_metadata, rg, files["cram_header"]))
    evidence.append(_read_group_ids(rg, files["cram_header"]))
    evidence.append(_technical_sample_id(sample_id, rg, targeted, files["cram_header"], files["targeted_json"]))
    evidence.append(_library_id(rg, files["cram_header"]))
    evidence.append(_lane(rg, files["cram_header"]))
    evidence.extend(_run_file_extra_attributes(files, checksums))
    evidence.extend(_run_qc_extra_attributes(mapping_rows, fastqc_rows, wgs_coverage_rows, wgs_overall_rows, files))
    return evidence


def _provider_metadata_evidence(metadata: dict[str, Any]) -> list[Evidence]:
    fields = {
        "SubmissionRequest.title": ("title", "submission_title"),
        "SubmissionRequest.description": ("description", "submission_description"),
        "SubmissionRequest.collaborators": ("collaborators",),
        "StudyRequest.title": ("study_title",),
        "StudyRequest.description": ("study_description",),
        "StudyRequest.study_type": ("study_type",),
        "StudyRequest.repositories": ("repositories",),
        "StudyRequest.pubmed_ids": ("pubmed_ids",),
        "StudyRequest.custom_tags": ("custom_tags",),
        "SampleRequest.biosample_id": ("biosample_id",),
        "SampleRequest.description": ("sample_description",),
        "SampleRequest.organism_part": ("organism_part",),
        "ExperimentRequest.design_description": ("design_description",),
        "AnalysisRequest.description": ("analysis_description",),
        "AnalysisRequest.analysis_type": ("analysis_type",),
        "AnalysisRequest.experiment_types": ("analysis_experiment_types", "experiment_types"),
        "AnalysisRequest.genome_id": ("analysis_genome_id", "genome_id"),
        "AnalysisRequest.chromosomes": ("analysis_chromosomes", "chromosomes"),
        "AnalysisRequest.platform": ("analysis_platform",),
        "DatasetRequest.title": ("dataset_title",),
        "DatasetRequest.description": ("dataset_description",),
        "DatasetRequest.dataset_types": ("dataset_types",),
        "DatasetRequest.policy_accession_id": ("policy_accession_id",),
        "DatasetRequest.run_accession_ids": ("run_accession_ids",),
        "DatasetRequest.run_provisional_ids": ("run_provisional_ids",),
        "DatasetRequest.analysis_accession_ids": ("analysis_accession_ids",),
        "DatasetRequest.analysis_provisional_ids": ("analysis_provisional_ids",),
        "SubmissionFinaliseRequest.expected_release_date": ("expected_release_date",),
        "SubmissionFinaliseRequest.dataset_changelogs": ("dataset_changelogs",),
    }
    evidence = []
    for field, keys in fields.items():
        value = None
        source_key = keys[0]
        for key in keys:
            if metadata.get(key) not in {None, ""}:
                value = metadata[key]
                source_key = key
                break
        if _has_value(value):
            evidence.append(Evidence(field, value, DIRECT, "high", "provider_metadata", source_key))
    extra_sections = {
        "StudyRequest": ("project_code",),
        "SampleRequest": (
            "country_of_birth",
            "country_of_birth_mother",
            "country_of_birth_father",
            "karyotypicSex",
            "pedigrees",
            "treatments",
            "interventionsOrProcedures",
            "measurements",
            "collectionDate",
            "collectionMoment",
            "obtentionProcedure",
        ),
        "ExperimentRequest": ("sequencing_run_ids", "flowcell_id"),
        "RunRequest": ("run_center",),
        "DatasetRequest": (
            "id",
            "createDateTime",
            "updateDateTime",
            "version",
            "cohortType",
            "inclusionCriteria",
            "exclusionCriteria",
            "cohortSize",
        ),
    }
    for prefix, keys in extra_sections.items():
        for key in keys:
            value = metadata.get(key)
            if _has_value(value):
                evidence.append(Evidence(f"{prefix}.extra_attributes.{key}", value, DIRECT, "high", "provider_metadata", key))
    return evidence


def _read_provider_metadata(metadata_file: Path | None, sample_id: str) -> dict[str, Any]:
    if metadata_file is None:
        return {}
    path = metadata_file.expanduser().resolve()
    if not path.is_file():
        raise FileNotFoundError(f"Provider metadata file does not exist: {path}")
    if path.suffix.lower() in {".json"}:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(payload, dict) and sample_id in payload:
            return payload[sample_id]
        if isinstance(payload, dict):
            return payload
        return {}
    delimiter = "\t" if path.suffix.lower() in {".tsv", ".tab"} else ","
    with path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        for row in reader:
            if row.get("sample_id") == sample_id or row.get("alias") == sample_id:
                return {key: value for key, value in row.items() if _has_value(value)}
    return {}


def _read_profile_defaults(provider: str, profile_file: Path | None) -> dict[str, Any]:
    if profile_file is not None:
        path = profile_file.expanduser().resolve()
        if not path.is_file():
            raise FileNotFoundError(f"Submission profile file does not exist: {path}")
        return _read_mapping_file(path)

    profile_name = f"{provider}.yaml"
    user_profile = user_submission_profile_path(provider)
    if user_profile.is_file():
        return _read_mapping_file(user_profile)

    try:
        profile = resources.files("impact_tools.conf.ega.submission_profiles").joinpath(profile_name)
    except ModuleNotFoundError:
        return {}
    if not profile.is_file():
        return {}
    text = profile.read_text(encoding="utf-8")
    return _parse_mapping_text(text, profile_name)


def _read_mapping_file(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    return _parse_mapping_text(text, path.name)


def _parse_mapping_text(text: str, source: str) -> dict[str, Any]:
    if source.lower().endswith(".json"):
        payload = json.loads(text)
    else:
        try:
            import yaml
        except ImportError as exc:
            raise RuntimeError(
                f"YAML profile {source} cannot be read because PyYAML is not installed. "
                "Use a JSON profile file or install PyYAML."
            ) from exc
        payload = yaml.safe_load(text) or {}
    if not isinstance(payload, dict):
        raise ValueError(f"Submission profile must contain a mapping: {source}")
    return payload


def _submission_profile_field_map() -> dict[tuple[str, str], str]:
    return {
        ("sample", "subject_id"): "SampleRequest.subject_id",
        ("sample", "biological_sex"): "SampleRequest.biological_sex",
        ("sample", "phenotype"): "SampleRequest.phenotype",
        ("experiment", "design_description"): "ExperimentRequest.design_description",
        ("experiment", "instrument_model_id"): "ExperimentRequest.instrument_model_id",
        ("experiment", "library_layout"): "ExperimentRequest.library_layout",
        ("experiment", "library_strategy"): "ExperimentRequest.library_strategy",
        ("experiment", "library_source"): "ExperimentRequest.library_source",
        ("experiment", "library_selection"): "ExperimentRequest.library_selection",
        ("experiment", "library_construction_protocol"): "ExperimentRequest.library_construction_protocol",
        ("analysis", "description"): "AnalysisRequest.description",
        ("analysis", "analysis_type"): "AnalysisRequest.analysis_type",
        ("analysis", "experiment_types"): "AnalysisRequest.experiment_types",
        ("analysis", "genome_id"): "AnalysisRequest.genome_id",
        ("analysis", "chromosomes"): "AnalysisRequest.chromosomes",
        ("analysis", "platform"): "AnalysisRequest.platform",
        ("submission", "title"): "SubmissionRequest.title",
        ("submission", "description"): "SubmissionRequest.description",
        ("submission", "collaborators"): "SubmissionRequest.collaborators",
        ("study", "title"): "StudyRequest.title",
        ("study", "description"): "StudyRequest.description",
        ("study", "study_type"): "StudyRequest.study_type",
        ("study", "repositories"): "StudyRequest.repositories",
        ("study", "pubmed_ids"): "StudyRequest.pubmed_ids",
        ("study", "custom_tags"): "StudyRequest.custom_tags",
        ("sample", "biosample_id"): "SampleRequest.biosample_id",
        ("sample", "description"): "SampleRequest.description",
        ("sample", "organism_part"): "SampleRequest.organism_part",
        ("dataset", "title"): "DatasetRequest.title",
        ("dataset", "description"): "DatasetRequest.description",
        ("dataset", "dataset_types"): "DatasetRequest.dataset_types",
        ("dataset", "policy_accession_id"): "DatasetRequest.policy_accession_id",
    }


def _merge_profile_default_evidence(evidence: list[Evidence], profile_defaults: dict[str, Any]) -> list[Evidence]:
    defaults = profile_defaults.get("defaults", profile_defaults)
    return _merge_profile_section_evidence(
        evidence,
        defaults,
        status=PROFILE_DEFAULT,
        location_prefix="defaults",
        notes="Default value from the selected submission profile, not extracted from provider technical files.",
    )


def _merge_profile_example_evidence(evidence: list[Evidence], profile_defaults: dict[str, Any]) -> list[Evidence]:
    examples = profile_defaults.get("examples")
    return _merge_profile_section_evidence(
        evidence,
        examples,
        status=EXAMPLE_VALUE,
        location_prefix="examples",
        notes="EXAMPLE testing value from submission profile. Replace before production use.",
    )


def _merge_profile_section_evidence(
    evidence: list[Evidence],
    section_values: Any,
    *,
    status: str,
    location_prefix: str,
    notes: str,
) -> list[Evidence]:
    if not isinstance(section_values, dict):
        return evidence
    by_field = {item.field: item for item in evidence}
    accepted_existing_statuses = {DIRECT, PROFILE_DEFAULT, EXAMPLE_VALUE}
    merged = list(evidence)
    for (section, key), field in _submission_profile_field_map().items():
        section_defaults = section_values.get(section)
        if not isinstance(section_defaults, dict):
            continue
        value = section_defaults.get(key)
        if not _has_value(value):
            continue
        current = by_field.get(field)
        if current is not None and current.status in accepted_existing_statuses and _has_value(current.value):
            continue
        merged = [item for item in merged if item.field != field]
        merged.append(
            Evidence(
                field,
                value,
                status,
                "high",
                "submission_profile",
                f"{location_prefix}.{section}.{key}",
                notes,
            )
        )
    return merged


def _sample_alias(sample_id: str, rg: list[dict[str, str]], header: Path) -> Evidence:
    values = sorted({record.get("SM", "") for record in rg if record.get("SM")})
    if len(values) == 1:
        return Evidence(
            "SampleRequest.alias",
            values[0],
            DIRECT,
            "high",
            header.name,
            "@RG SM tag",
            "Technical sample identifier from the CRAM read groups.",
        )
    return Evidence(
        "SampleRequest.alias",
        sample_id,
        DIRECT,
        "medium",
        "input directory",
        "directory name",
        "Fallback to input directory/sample prefix.",
    )


def _metadata_or_missing(field: str, metadata: dict[str, Any], key: str) -> Evidence:
    value = metadata.get(key)
    if value:
        return Evidence(field, value, DIRECT, "high", "provider_metadata", key)
    return Evidence(
        field,
        None,
        NOT_FOUND,
        "high",
        "provider_metadata",
        key,
        "Expected from sequencing service or cohort/project metadata.",
    )


def _biological_sex(metadata: dict[str, Any], mapping_rows: list[list[str]], mapping_file: Path) -> Evidence:
    value = metadata.get("biological_sex") or metadata.get("sex")
    if value:
        return Evidence("SampleRequest.biological_sex", value, DIRECT, "high", "provider_metadata", "biological_sex/sex")
    ploidy = _metric_value(mapping_rows, "Provided sex chromosome ploidy")
    if ploidy and ploidy != "NA":
        return Evidence(
            "SampleRequest.biological_sex",
            ploidy,
            INFERRED,
            "medium",
            mapping_file.name,
            "Provided sex chromosome ploidy",
            "Technical ploidy evidence; requires confirmation before submission.",
        )
    return Evidence(
        "SampleRequest.biological_sex",
        None,
        NOT_FOUND,
        "high",
        mapping_file.name if mapping_file.exists() else "provider_metadata",
        "Provided sex chromosome ploidy",
        "No explicit biological sex found.",
    )


def _phenotype(metadata: dict[str, Any], targeted: dict[str, Any], targeted_file: Path) -> Evidence:
    value = metadata.get("phenotype")
    if value:
        return Evidence("SampleRequest.phenotype", value, DIRECT, "high", "provider_metadata", "phenotype")
    annotations = []
    for record in targeted.get("locusAnnotations", [])[:50]:
        annotation = record.get("phenotypeDatabaseAnnotation")
        if annotation and annotation != "None":
            annotations.append(annotation)
    annotations.extend(
        value for key in ("cyp2b6", "cyp2d6")
        if isinstance(targeted.get(key), dict)
        for value in [targeted[key].get("phenotypeDatabaseAnnotation")]
        if value and value != "None"
    )
    if annotations:
        unique = sorted(set(annotations))
        return Evidence(
            "SampleRequest.phenotype",
            "; ".join(unique),
            INFERRED,
            "medium",
            targeted_file.name,
            "$.locusAnnotations[*].phenotypeDatabaseAnnotation",
            "PGx/targeted derived annotations, not necessarily clinical sample phenotype.",
        )
    return Evidence("SampleRequest.phenotype", None, NOT_FOUND, "high", targeted_file.name, "phenotype search")


def _instrument_model(metadata: dict[str, Any], rg: list[dict[str, str]], header: Path) -> Evidence:
    value = metadata.get("instrument_model_id") or metadata.get("instrument_model")
    if value:
        return Evidence("ExperimentRequest.instrument_model_id", value, DIRECT, "high", "provider_metadata", "instrument_model")
    pm_values = sorted({record.get("PM", "") for record in rg if record.get("PM")})
    if len(pm_values) == 1:
        return Evidence("ExperimentRequest.instrument_model_id", pm_values[0], DIRECT, "high", header.name, "@RG PM tag")
    return Evidence(
        "ExperimentRequest.instrument_model_id",
        None,
        NOT_FOUND,
        "high",
        header.name,
        "@RG PM tag",
        "Physical sequencer model not found; DRAGEN is analysis software.",
    )


def _library_layout(rows: list[list[str]], mapping_file: Path) -> Evidence:
    mate = _metric_value(rows, "Reads with mate sequenced")
    paired = _metric_value(rows, "Paired reads (itself & mate mapped)")
    if mate and mate != "0" and paired and paired != "0":
        return Evidence(
            "ExperimentRequest.library_layout",
            "PAIRED",
            DIRECT,
            "high",
            mapping_file.name,
            "Reads with mate sequenced; Paired reads (itself & mate mapped)",
        )
    return Evidence("ExperimentRequest.library_layout", None, NOT_FOUND, "high", mapping_file.name, "paired read metrics")


def _library_strategy(metrics: dict[str, Any], metrics_file: Path) -> Evidence:
    if _get(metrics, "modules.coverageSummary.wgs") is not None:
        return Evidence("ExperimentRequest.library_strategy", "WGS", DIRECT, "high", metrics_file.name, "$.modules.coverageSummary.wgs")
    return Evidence("ExperimentRequest.library_strategy", None, NOT_FOUND, "high", metrics_file.name, "$.modules.coverageSummary")


def _library_name(rg: list[dict[str, str]], header: Path) -> Evidence:
    values = sorted({record.get("LB", "") for record in rg if record.get("LB")})
    if len(values) == 1:
        return Evidence("ExperimentRequest.library_name", values[0], DIRECT, "high", header.name, "@RG LB tag")
    return Evidence("ExperimentRequest.library_name", None, NOT_FOUND, "high", header.name, "@RG LB tag")


def _paired_nominal_length(
    mapping_rows: list[list[str]],
    insert_rows: list[dict[str, str]],
    mapping_file: Path,
    insert_file: Path,
) -> Evidence:
    means = _insert_metric(mapping_rows, "Insert length: mean")
    if means:
        return Evidence("ExperimentRequest.paired_nominal_length", means, INFERRED, "medium", mapping_file.name, "Insert length: mean")
    summary = _insert_stats_summary(insert_rows)
    if summary:
        return Evidence("ExperimentRequest.paired_nominal_length", summary, INFERRED, "medium", insert_file.name, "MEAN/Q50 rows")
    return Evidence("ExperimentRequest.paired_nominal_length", None, NOT_FOUND, "high", insert_file.name, "insert-size metrics")


def _paired_nominal_sdev(
    mapping_rows: list[list[str]],
    insert_rows: list[dict[str, str]],
    mapping_file: Path,
    insert_file: Path,
) -> Evidence:
    sdevs = _insert_metric(mapping_rows, "Insert length: standard deviation")
    if sdevs:
        return Evidence("ExperimentRequest.paired_nominal_sdev", sdevs, INFERRED, "medium", mapping_file.name, "Insert length: standard deviation")
    summary = _insert_stats_summary(insert_rows)
    if summary:
        return Evidence("ExperimentRequest.paired_nominal_sdev", summary, INFERRED, "medium", insert_file.name, "STD rows")
    return Evidence("ExperimentRequest.paired_nominal_sdev", None, NOT_FOUND, "high", insert_file.name, "insert-size metrics")


def _run_files(files: dict[str, Path], checksums: dict[str, str]) -> list[Evidence]:
    items = []
    for key, field in [("cram", "Run.files.primary"), ("crai", "Run.files.index")]:
        path = files[key]
        value = {
            "name": path.name,
            "extension": "".join(path.suffixes[-2:]) if path.name.endswith(".cram.crai") else path.suffix,
            "exists": path.exists(),
            "size_bytes": path.stat().st_size if path.exists() else None,
            "md5": checksums.get(path.name),
        }
        status = DIRECT if path.exists() or value["md5"] else NOT_FOUND
        items.append(Evidence(field, value, status, "high", path.name if path.exists() else "checksum.md5", "file inventory/checksum"))
    return items


def _analysis_file_evidence(path: Path | None) -> Evidence:
    value = _analysis_file(path) if path is not None else None
    return Evidence(
        "AnalysisRequest.files",
        value,
        DIRECT if path is not None and path.is_file() else NOT_FOUND,
        "high",
        path.name if path is not None else "file inventory",
        "VCF selected for sample",
    )


def _reference(metrics: dict[str, Any], targeted: dict[str, Any], pg: list[dict[str, str]], metrics_file: Path, targeted_file: Path, header: Path) -> Evidence:
    value = _get(metrics, "metadata.runInfo.reference") or targeted.get("genomeBuild")
    if value:
        return Evidence(
            "ExperimentRequest.extra_attributes.reference_genome",
            value,
            DIRECT,
            "high",
            metrics_file.name if _get(metrics, "metadata.runInfo.reference") else targeted_file.name,
            "reference/genomeBuild",
        )
    for record in pg:
        command = record.get("CL", "")
        if "GRCh38" in command or "hg38" in command:
            return Evidence(
                "ExperimentRequest.extra_attributes.reference_genome",
                "hg38/GRCh38",
                DIRECT,
                "medium",
                header.name,
                "@PG CL",
            )
    return Evidence(
        "ExperimentRequest.extra_attributes.reference_genome",
        None,
        NOT_FOUND,
        "high",
        metrics_file.name,
        "reference search",
    )


def _pipeline(metrics: dict[str, Any], replay: dict[str, Any], pg: list[dict[str, str]], metrics_file: Path, replay_file: Path, header: Path) -> list[Evidence]:
    version = _get(metrics, "metadata.dragenVersion") or _get(replay, "system.dragen_version")
    if version:
        source = metrics_file.name if _get(metrics, "metadata.dragenVersion") else replay_file.name
        location = "dragenVersion"
        return [
            Evidence("ExperimentRequest.extra_attributes.analysis_pipeline", "DRAGEN", DIRECT, "high", source, location),
            Evidence("ExperimentRequest.extra_attributes.analysis_pipeline_version", version, DIRECT, "high", source, location),
        ]
    for record in pg:
        if "DRAGEN SW build" in record.get("ID", ""):
            return [
                Evidence("ExperimentRequest.extra_attributes.analysis_pipeline", "DRAGEN", DIRECT, "high", header.name, "@PG DRAGEN SW build"),
                Evidence(
                    "ExperimentRequest.extra_attributes.analysis_pipeline_version",
                    record.get("VN"),
                    DIRECT,
                    "high",
                    header.name,
                    "@PG DRAGEN SW build VN",
                ),
            ]
    return [
        Evidence("ExperimentRequest.extra_attributes.analysis_pipeline", None, NOT_FOUND, "high", header.name, "@PG"),
        Evidence("ExperimentRequest.extra_attributes.analysis_pipeline_version", None, NOT_FOUND, "high", header.name, "@PG"),
    ]


def _sequencing_run_ids(metadata: dict[str, Any], rg: list[dict[str, str]], header: Path, input_dir: Path) -> Evidence:
    value = metadata.get("sequencing_run_ids") or metadata.get("sequencing_run_id")
    if value:
        return Evidence("ExperimentRequest.extra_attributes.sequencing_run_ids", value, DIRECT, "high", "provider_metadata", "sequencing_run_ids")
    candidates = []
    for record in rg:
        pu = record.get("PU")
        if pu and pu.count(":") >= 2:
            candidates.append(":".join(pu.split(":")[:3]))
    values = sorted(set(candidates))
    if values:
        return Evidence(
            "ExperimentRequest.extra_attributes.sequencing_run_ids",
            "; ".join(values),
            INFERRED,
            "medium",
            header.name,
            "@RG PU",
            "Sequencing run identifiers inferred from platform units; validate with sequencing centre.",
        )
    fastq_values = _sequencing_run_ids_from_fastq(input_dir)
    if fastq_values:
        return Evidence(
            "ExperimentRequest.extra_attributes.sequencing_run_ids",
            "; ".join(fastq_values),
            INFERRED,
            "medium",
            str(input_dir),
            "FASTQ read headers",
            "Sequencing run identifiers inferred from FASTQ read names as instrument:run:flowcell; validate with sequencing centre.",
        )
    return Evidence(
        "ExperimentRequest.extra_attributes.sequencing_run_ids",
        None,
        NOT_FOUND,
        "high",
        "provider_metadata",
        "sequencing_run_ids",
        "Expected from sequencing centre when a sample spans one or more sequencing runs.",
    )


def _sequencing_run_ids_from_fastq(input_dir: Path, max_records_per_file: int = 1000) -> list[str]:
    values: set[str] = set()
    fastq_files = sorted(
        path for path in input_dir.rglob("*")
        if path.is_file() and _is_fastq_path(path)
    )
    for fastq in fastq_files:
        for header in _iter_fastq_headers(fastq, max_records=max_records_per_file):
            run_id = _sequencing_run_id_from_read_name(header)
            if run_id:
                values.add(run_id)
    return sorted(values)


def _is_fastq_path(path: Path) -> bool:
    name = path.name.lower()
    return name.endswith((".fastq", ".fq", ".fastq.gz", ".fq.gz"))


def _iter_fastq_headers(path: Path, max_records: int) -> list[str]:
    opener = gzip.open if path.name.lower().endswith(".gz") else open
    headers = []
    try:
        with opener(path, "rt", encoding="utf-8", errors="replace") as handle:
            for line_number, line in enumerate(handle):
                if line_number % 4 == 0 and line.startswith("@"):
                    headers.append(line.strip())
                    if len(headers) >= max_records:
                        break
    except OSError:
        return []
    return headers


def _sequencing_run_id_from_read_name(header: str) -> str | None:
    read_name = header[1:] if header.startswith("@") else header
    read_name = read_name.split()[0]
    parts = read_name.split(":")
    if len(parts) >= 3 and parts[0] and parts[1] and parts[2]:
        return ":".join(parts[:3])
    if len(parts) >= 2 and parts[0] and parts[1]:
        return ":".join(parts[:2])
    return None


def _flowcell_id(metadata: dict[str, Any], rg: list[dict[str, str]], header: Path) -> Evidence:
    value = metadata.get("flowcell_id") or metadata.get("flowcell_ids")
    if value:
        return Evidence("ExperimentRequest.extra_attributes.flowcell_id", value, DIRECT, "high", "provider_metadata", "flowcell_id")
    candidates = []
    for record in rg:
        pu = record.get("PU")
        if pu and pu.count(":") >= 2:
            candidates.append(pu.split(":")[2])
    values = sorted(set(candidates))
    if values:
        return Evidence(
            "ExperimentRequest.extra_attributes.flowcell_id",
            "; ".join(values),
            INFERRED,
            "medium",
            header.name,
            "@RG PU",
            "Flowcell identifiers inferred from platform units; validate with sequencing centre.",
        )
    return Evidence("ExperimentRequest.extra_attributes.flowcell_id", None, NOT_FOUND, "high", "provider_metadata", "flowcell_id")


def _read_group_ids(rg: list[dict[str, str]], header: Path) -> Evidence:
    values = [record.get("ID") for record in rg if record.get("ID")]
    return Evidence("Run.read_group_ids", values or None, DIRECT if values else NOT_FOUND, "high", header.name, "@RG ID")


def _technical_sample_id(sample_id: str, rg: list[dict[str, str]], targeted: dict[str, Any], header: Path, targeted_file: Path) -> Evidence:
    sm_values = sorted({record.get("SM", "") for record in rg if record.get("SM")})
    value = sm_values[0] if len(sm_values) == 1 else targeted.get("sampleId") or sample_id
    source = header.name if len(sm_values) == 1 else targeted_file.name
    return Evidence("Run.sample_id_technical", value, DIRECT, "high", source, "@RG SM / $.sampleId")


def _library_id(rg: list[dict[str, str]], header: Path) -> Evidence:
    values = sorted({record.get("LB", "") for record in rg if record.get("LB")})
    return Evidence("Run.library_id", values[0] if len(values) == 1 else None, DIRECT if len(values) == 1 else NOT_FOUND, "high", header.name, "@RG LB")


def _lane(rg: list[dict[str, str]], header: Path) -> Evidence:
    values = sorted({record.get("PU", "") for record in rg if record.get("PU")})
    if values:
        return Evidence("Run.lane", values, INFERRED, "medium", header.name, "@RG PU", "Likely lanes/platform units; confirm with sequencing centre.")
    return Evidence("Run.lane", None, NOT_FOUND, "high", header.name, "@RG PU")


def _run_file_extra_attributes(files: dict[str, Path], checksums: dict[str, str]) -> list[Evidence]:
    cram = files["cram"]
    crai = files["crai"]
    return [
        Evidence("RunRequest.extra_attributes.original_file_name", cram.name, DIRECT, "high", cram.name, "filename"),
        Evidence("RunRequest.extra_attributes.index_file_name", crai.name, DIRECT, "high", crai.name, "filename"),
        Evidence(
            "RunRequest.extra_attributes.unencrypted_cram_md5",
            checksums.get(cram.name),
            DIRECT if checksums.get(cram.name) else NOT_FOUND,
            "high",
            "checksum.md5 / *.cram.md5sum",
            cram.name,
        ),
        Evidence(
            "RunRequest.extra_attributes.unencrypted_crai_md5",
            checksums.get(crai.name),
            DIRECT if checksums.get(crai.name) else NOT_FOUND,
            "high",
            "checksum.md5",
            crai.name,
        ),
    ]


def _run_qc_extra_attributes(
    mapping_rows: list[list[str]],
    fastqc_rows: list[list[str]],
    wgs_coverage_rows: list[list[str]],
    wgs_overall_rows: list[list[str]],
    files: dict[str, Path],
) -> list[Evidence]:
    mean_cov = _coverage_metric(wgs_overall_rows, "Average alignment coverage over wgs") or _coverage_metric(
        wgs_coverage_rows, "Average alignment coverage over genome"
    )
    median_cov = _coverage_metric(wgs_coverage_rows, "Median autosomal coverage over genome")
    ratio = _coverage_metric(wgs_coverage_rows, "Mean/Median autosomal coverage ratio over genome")
    pct20 = _coverage_metric(wgs_coverage_rows, "PCT of genome with coverage [  20x: inf)")
    q20 = _fastqc_q20_percent(fastqc_rows)
    contamination = _metric_value(mapping_rows, "Estimated sample contamination")
    return [
        Evidence(
            "RunRequest.extra_attributes.mean_coverage",
            mean_cov,
            DIRECT if mean_cov else NOT_FOUND,
            "high",
            files["wgs_overall_mean_cov"].name if mean_cov else files["wgs_coverage_metrics"].name,
            "Average alignment coverage",
        ),
        Evidence(
            "RunRequest.extra_attributes.mean_median_coverage",
            ratio or median_cov,
            DIRECT if ratio or median_cov else NOT_FOUND,
            "high",
            files["wgs_coverage_metrics"].name,
            "Mean/Median autosomal coverage ratio over genome / median autosomal coverage",
        ),
        Evidence(
            "RunRequest.extra_attributes.percentage_20x_coverage",
            pct20,
            DIRECT if pct20 else NOT_FOUND,
            "high",
            files["wgs_coverage_metrics"].name,
            "PCT of genome with coverage [  20x: inf)",
        ),
        Evidence(
            "RunRequest.extra_attributes.percentage_based_quality_q20",
            q20,
            DIRECT if q20 else NOT_FOUND,
            "medium",
            files["fastqc_metrics"].name,
            "READ MEAN QUALITY Q>=20 reads / all reads",
            "Percentage of reads with mean quality >= Q20, not percentage of bases Q20.",
        ),
        Evidence(
            "RunRequest.extra_attributes.contamination_fraction",
            None if contamination == "NA" else contamination,
            DIRECT if contamination and contamination != "NA" else NOT_FOUND,
            "high",
            files["mapping_metrics"].name,
            "Estimated sample contamination",
        ),
    ]


def _coverage_metric(rows: list[list[str]], metric: str) -> str | None:
    for row in rows:
        if len(row) >= 3 and row[2] == metric:
            return row[3] if len(row) > 3 else None
        if len(row) >= 2 and row[0] == metric:
            return row[1].strip()
    return None


def _fastqc_q20_percent(rows: list[list[str]]) -> str | None:
    total = 0
    q20_or_better = 0
    for row in rows:
        if len(row) < 4 or row[0] != "READ MEAN QUALITY":
            continue
        match = re.search(r"Q(\d+) Reads", row[2])
        if not match:
            continue
        try:
            count = int(row[3])
        except ValueError:
            continue
        total += count
        if int(match.group(1)) >= 20:
            q20_or_better += count
    if not total:
        return None
    return f"{q20_or_better / total * 100:.3f}"


def _read_groups(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if not line.startswith("@RG\t"):
            continue
        records.append(_sam_tags(line))
    return records


def _program_records(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    records = []
    for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("@PG\t"):
            records.append(_sam_tags(line))
    return records


def _sam_tags(line: str) -> dict[str, str]:
    record = {}
    for token in line.split("\t")[1:]:
        if ":" in token:
            key, value = token.split(":", 1)
            record[key] = value
    return record


def _read_checksums(cram_md5: Path, checksum_md5: Path) -> dict[str, str]:
    checksums = {}
    if cram_md5.is_file():
        text = cram_md5.read_text(encoding="utf-8").strip()
        if text:
            parts = text.split()
            checksums[cram_md5.name.removesuffix(".md5sum")] = parts[0]
    if checksum_md5.is_file():
        for line in checksum_md5.read_text(encoding="utf-8").splitlines():
            parts = line.split()
            if len(parts) >= 2:
                checksums[Path(parts[-1]).name] = parts[0]
    return checksums


def _read_json(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    return json.loads(path.read_text(encoding="utf-8"))


def _read_csv_rows(path: Path) -> list[list[str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.reader(handle))


def _read_tsv_rows(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        return []
    with path.open("r", encoding="utf-8", newline="") as handle:
        return list(csv.DictReader(handle, delimiter="\t"))


def _metric_value(rows: list[list[str]], metric: str) -> str | None:
    for row in rows:
        if len(row) >= 4 and row[2] == metric:
            return row[3]
    return None


def _insert_metric(rows: list[list[str]], metric: str) -> str | None:
    values = []
    for row in rows:
        if len(row) >= 4 and row[2] == metric and row[1]:
            values.append(f"{row[1]}={row[3]}")
    return "; ".join(values) if values else None


def _insert_stats_summary(rows: list[dict[str, str]]) -> str | None:
    values = []
    for row in rows:
        try:
            npairs = int(row.get("NPAIRS") or "0")
        except ValueError:
            continue
        if npairs > 0 and row.get("MEAN") and row.get("STD"):
            values.append(
                f"RG {row.get('RG')}: Q50={row.get('Q50')}, "
                f"MEAN={row.get('MEAN')}, STD={row.get('STD')}, NPAIRS={row.get('NPAIRS')}"
            )
    return "; ".join(values) if values else None


def _get(payload: dict[str, Any], path: str) -> Any:
    current: Any = payload
    for part in path.split("."):
        if not isinstance(current, dict) or part not in current:
            return None
        current = current[part]
    return current


def _file_inventory(input_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(input_dir.iterdir()):
        if not path.is_file():
            continue
        rows.append(
            {
                "name": path.name,
                "size_bytes": path.stat().st_size,
                "md5": _md5(path) if path.stat().st_size < 100_000_000 else None,
            }
        )
    return rows


def _file_inventory_recursive(input_dir: Path, output_dir: Path) -> list[dict[str, Any]]:
    rows = []
    for path in sorted(input_dir.rglob("*")):
        if not path.is_file() or (
            path.absolute().is_relative_to(output_dir.absolute())
            or path.resolve(strict=False).is_relative_to(output_dir.resolve(strict=False))
        ):
            continue
        rows.append(
            {
                "name": str(path.relative_to(input_dir)),
                "size_bytes": path.stat().st_size,
                "md5": _md5(path) if path.stat().st_size < 100_000_000 else None,
            }
        )
    return rows


def _md5(path: Path) -> str:
    digest = hashlib.md5()  # noqa: S324 - file inventory compatibility with FEGA metadata.
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _draft_from_evidence(
    sample_id: str,
    evidence: list[Evidence],
    profile_defaults: dict[str, Any],
    *,
    vcf_file: Path | None = None,
) -> dict[str, Any]:
    values = {item.field: item for item in evidence}

    def submission_value(field: str) -> Any:
        item = values.get(field)
        if item and item.status in {DIRECT, PROFILE_DEFAULT, EXAMPLE_VALUE} and _has_value(item.value):
            return item.value
        return None

    def reviewed_value(field: str) -> Any:
        item = values.get(field)
        if item and item.status in {DIRECT, INFERRED, PROFILE_DEFAULT, EXAMPLE_VALUE} and _has_value(item.value):
            return item.value
        return None

    draft = {
        "submission": {
            "title": submission_value("SubmissionRequest.title"),
            "description": submission_value("SubmissionRequest.description"),
            "collaborators": submission_value("SubmissionRequest.collaborators"),
        },
        "study": {
            "title": submission_value("StudyRequest.title"),
            "description": submission_value("StudyRequest.description"),
            "study_type": submission_value("StudyRequest.study_type"),
            "repositories": submission_value("StudyRequest.repositories"),
            "pubmed_ids": submission_value("StudyRequest.pubmed_ids"),
            "custom_tags": submission_value("StudyRequest.custom_tags"),
            "extra_attributes": _extra_attributes("StudyRequest", evidence),
        },
        "sample": {
            "alias": submission_value("SampleRequest.alias") or sample_id,
            "subject_id": submission_value("SampleRequest.subject_id"),
            "biological_sex": submission_value("SampleRequest.biological_sex"),
            "phenotype": submission_value("SampleRequest.phenotype"),
            "biosample_id": submission_value("SampleRequest.biosample_id"),
            "description": submission_value("SampleRequest.description"),
            "organism_part": submission_value("SampleRequest.organism_part"),
            "extra_attributes": _extra_attributes("SampleRequest", evidence),
        },
        "experiment": {
            "design_description": submission_value("ExperimentRequest.design_description"),
            "instrument_model_id": submission_value("ExperimentRequest.instrument_model_id"),
            "library_layout": submission_value("ExperimentRequest.library_layout"),
            "library_strategy": submission_value("ExperimentRequest.library_strategy"),
            "library_source": submission_value("ExperimentRequest.library_source"),
            "library_selection": submission_value("ExperimentRequest.library_selection"),
            "library_name": submission_value("ExperimentRequest.library_name"),
            "library_construction_protocol": submission_value("ExperimentRequest.library_construction_protocol"),
            "paired_nominal_length": reviewed_value("ExperimentRequest.paired_nominal_length"),
            "paired_nominal_sdev": reviewed_value("ExperimentRequest.paired_nominal_sdev"),
            "study_accession_id": submission_value("ExperimentRequest.study_accession_id"),
            "study_provisional_id": submission_value("ExperimentRequest.study_provisional_id"),
            "extra_attributes": _extra_attributes("ExperimentRequest", evidence),
        },
        "run": {
            "experiment_accession_id": submission_value("RunRequest.experiment_accession_id"),
            "experiment_provisional_id": submission_value("RunRequest.experiment_provisional_id"),
            "sample_accession_id": submission_value("RunRequest.sample_accession_id"),
            "sample_provisional_id": submission_value("RunRequest.sample_provisional_id"),
            "run_file_type": "cram",
            "files": [
                reviewed_value("Run.files.primary"),
                reviewed_value("Run.files.index"),
            ],
            "extra_attributes": _extra_attributes("RunRequest", evidence),
        },
        "analysis": {
            "title": f"{sample_id} variant analysis",
            "description": submission_value("AnalysisRequest.description"),
            "analysis_type": submission_value("AnalysisRequest.analysis_type"),
            "experiment_types": submission_value("AnalysisRequest.experiment_types"),
            "genome_id": submission_value("AnalysisRequest.genome_id"),
            "chromosomes": submission_value("AnalysisRequest.chromosomes"),
            "platform": submission_value("AnalysisRequest.platform"),
            "study_accession_id": submission_value("AnalysisRequest.study_accession_id"),
            "study_provisional_id": submission_value("AnalysisRequest.study_provisional_id"),
            "experiment_accession_ids": submission_value("AnalysisRequest.experiment_accession_ids"),
            "experiment_provisional_ids": submission_value("AnalysisRequest.experiment_provisional_ids"),
            "sample_accession_ids": submission_value("AnalysisRequest.sample_accession_ids"),
            "sample_provisional_ids": submission_value("AnalysisRequest.sample_provisional_ids"),
            "files": [_analysis_file(vcf_file)] if vcf_file is not None else [],
            "extra_attributes": _extra_attributes("AnalysisRequest", evidence),
        },
        "dataset": {
            "title": submission_value("DatasetRequest.title"),
            "description": submission_value("DatasetRequest.description"),
            "dataset_types": submission_value("DatasetRequest.dataset_types"),
            "policy_accession_id": submission_value("DatasetRequest.policy_accession_id"),
            "run_accession_ids": submission_value("DatasetRequest.run_accession_ids"),
            "run_provisional_ids": submission_value("DatasetRequest.run_provisional_ids"),
            "analysis_accession_ids": submission_value("DatasetRequest.analysis_accession_ids"),
            "analysis_provisional_ids": submission_value("DatasetRequest.analysis_provisional_ids"),
            "extra_attributes": _extra_attributes("DatasetRequest", evidence),
        },
    }
    return _apply_profile_defaults(draft, profile_defaults)


def _analysis_file(path: Path) -> dict[str, Any]:
    return {
        "name": path.name,
        "extension": ".vcf.gz" if path.name.endswith(".vcf.gz") else path.suffix,
        "exists": path.is_file(),
        "size_bytes": path.stat().st_size if path.is_file() else None,
    }


def _batch_draft(
    prepared: list[tuple[str, Path, list[Evidence], dict[str, Any]]],
) -> dict[str, Any]:
    if not prepared:
        raise ValueError("Cannot prepare an empty submission batch")

    first = prepared[0][3]
    batch = {
        "submission": first["submission"],
        "study": first["study"],
        "samples": [],
        "experiments": [],
        "runs": [],
        "analyses": [],
        "dataset": first["dataset"],
    }
    for sample_id, _, _, draft in prepared:
        for section in ("submission", "study", "dataset"):
            if draft[section] != first[section]:
                raise ValueError(
                    f"Sample {sample_id!r} defines a different shared "
                    f"{section!r} payload; all samples in one batch must use "
                    "the same Submission, Study and Dataset metadata"
                )
        for plural, singular in (
            ("samples", "sample"),
            ("experiments", "experiment"),
            ("runs", "run"),
            ("analyses", "analysis"),
        ):
            payload = dict(draft[singular])
            payload["_key"] = sample_id
            batch[plural].append(payload)
    return batch


def _extra_attributes(prefix: str, evidence: list[Evidence]) -> list[dict[str, str]]:
    attrs = []
    marker = f"{prefix}.extra_attributes."
    for item in evidence:
        if not item.field.startswith(marker):
            continue
        if item.status not in {DIRECT, INFERRED, PROFILE_DEFAULT, EXAMPLE_VALUE} or item.value in {None, ""}:
            continue
        attrs.append({"tag": item.field.removeprefix(marker), "value": str(item.value)})
    return attrs


def _apply_profile_defaults(draft: dict[str, Any], profile_defaults: dict[str, Any]) -> dict[str, Any]:
    defaults = profile_defaults.get("defaults", profile_defaults)
    if not isinstance(defaults, dict):
        return draft
    for section, section_defaults in defaults.items():
        if not isinstance(section_defaults, dict) or section not in draft:
            continue
        target = draft[section]
        if not isinstance(target, dict):
            continue
        for key, value in section_defaults.items():
            if not _has_value(target.get(key)) and _has_value(value):
                target[key] = value
    return draft


def _missing_required_fields(evidence: list[Evidence]) -> dict[str, Any]:
    required_sources = {
        "SubmissionRequest.title": "COHORT / CONFIG",
        "StudyRequest.title": "COHORT / CONFIG",
        "StudyRequest.description": "COHORT",
        "StudyRequest.study_type": "COHORT / CONFIG",
        "SampleRequest.alias": "CNIO_FILE",
        "SampleRequest.biological_sex": "COHORT",
        "SampleRequest.subject_id": "COHORT",
        "SampleRequest.phenotype": "COHORT",
        "ExperimentRequest.design_description": "SEQ / CONFIG",
        "ExperimentRequest.instrument_model_id": "SEQ",
        "ExperimentRequest.library_layout": "CNIO_FILE",
        "ExperimentRequest.library_strategy": "CNIO_FILE / SEQ validation",
        "ExperimentRequest.library_source": "SEQ / CONFIG",
        "ExperimentRequest.library_selection": "SEQ / CONFIG",
        "RunRequest.files": "CNIO_FILE",
        "RunRequest.run_file_type": "CNIO_FILE",
        "AnalysisRequest.description": "ANALYSIS / CONFIG",
        "AnalysisRequest.analysis_type": "ANALYSIS / CONFIG",
        "AnalysisRequest.experiment_types": "ANALYSIS / CONFIG",
        "AnalysisRequest.genome_id": "REFERENCE / CONFIG",
        "AnalysisRequest.chromosomes": "REFERENCE / CONFIG",
        "AnalysisRequest.files": "CNIO_FILE",
        "DatasetRequest.title": "COHORT / CONFIG",
        "DatasetRequest.description": "COHORT",
        "DatasetRequest.dataset_types": "SEQ / CONFIG",
        "DatasetRequest.policy_accession_id": "COHORT / DAC",
    }
    practical_links = {
        "ExperimentRequest.study_provisional_id": "TMP-API",
        "RunRequest.experiment_provisional_id": "TMP-API",
        "RunRequest.sample_provisional_id": "TMP-API",
        "AnalysisRequest.study_provisional_id": "TMP-API",
        "AnalysisRequest.experiment_provisional_ids": "TMP-API",
        "AnalysisRequest.sample_provisional_ids": "TMP-API",
        "DatasetRequest.run_provisional_ids": "TMP-API",
        "DatasetRequest.analysis_provisional_ids": "TMP-API",
    }
    by_field = {item.field: item for item in evidence}
    missing_by_source: dict[str, dict[str, Any]] = {}
    missing_required: dict[str, Any] = {}

    for field, source in required_sources.items():
        if _field_is_satisfied(field, by_field):
            continue
        item = by_field.get(field)
        record = {
            "status": item.status if item else NOT_FOUND,
            "expected_source": source,
            "notes": item.notes if item else "",
        }
        missing_required[field] = record
        missing_by_source.setdefault(source, {})[field] = record

    missing_links: dict[str, Any] = {}
    for field, source in practical_links.items():
        if _field_is_satisfied(field, by_field):
            continue
        item = by_field.get(field)
        record = {
            "status": item.status if item else API_GENERATED,
            "expected_source": source,
            "notes": "Generated or selected after previous API calls; needed to link submission objects.",
        }
        missing_links[field] = record
        missing_by_source.setdefault(source, {})[field] = record

    return {
        "missing_required_fields": missing_required,
        "missing_practical_links": missing_links,
        "missing_by_source": missing_by_source,
    }


def _has_value(value: Any) -> bool:
    if value is None:
        return False
    if value == "":
        return False
    if isinstance(value, (list, dict, tuple, set)) and not value:
        return False
    return True


def _field_is_satisfied(field: str, by_field: dict[str, Evidence]) -> bool:
    if field == "RunRequest.files":
        primary = by_field.get("Run.files.primary")
        return bool(primary and primary.status == DIRECT and primary.value)
    if field == "RunRequest.run_file_type":
        return True
    item = by_field.get(field)
    return bool(item and item.status in {DIRECT, PROFILE_DEFAULT, EXAMPLE_VALUE} and _has_value(item.value))


def _render_evidence_markdown(input_dir: Path, sample_id: str, evidence: list[Evidence]) -> str:
    lines = [
        f"# EGA submission evidence for {sample_id}",
        "",
        f"Input directory: `{input_dir}`",
        "",
        "| FEGA field | Observed value | Status | Confidence | Evidence file | Exact location / command | Notes |",
        "|---|---|---|---|---|---|---|",
    ]
    for item in evidence:
        value = "" if item.value is None else str(item.value).replace("\n", " ")
        lines.append(
            "| "
            + " | ".join(
                _md_cell(part)
                for part in [
                    item.field,
                    value,
                    item.status,
                    item.confidence,
                    item.evidence_file,
                    item.location,
                    item.notes,
                ]
            )
            + " |"
        )
    lines.extend(
        [
            "",
            "## Notes",
            "",
            "- This file is generated from local technical evidence and optional provider metadata.",
            "- Values marked as `inferred` should be reviewed before submission.",
            "- Missing FEGA required fields are also listed in `missing_fields.yaml`.",
        ]
    )
    return "\n".join(lines) + "\n"


def _render_yaml(value: Any, indent: int = 0) -> str:
    lines: list[str] = []
    prefix = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if isinstance(item, (dict, list)):
                if not item:
                    lines.append(
                        f"{prefix}{key}: {'{}' if isinstance(item, dict) else '[]'}"
                    )
                else:
                    lines.append(f"{prefix}{key}:")
                    lines.append(_render_yaml(item, indent + 2).rstrip())
            else:
                lines.append(f"{prefix}{key}: {_yaml_scalar(item)}")
    elif isinstance(value, list):
        for item in value:
            if isinstance(item, (dict, list)):
                if not item:
                    lines.append(
                        f"{prefix}- {'{}' if isinstance(item, dict) else '[]'}"
                    )
                else:
                    lines.append(f"{prefix}-")
                    lines.append(_render_yaml(item, indent + 2).rstrip())
            else:
                lines.append(f"{prefix}- {_yaml_scalar(item)}")
    else:
        lines.append(f"{prefix}{_yaml_scalar(value)}")
    return "\n".join(lines) + "\n"


def _yaml_scalar(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return str(value)
    text = str(value).replace("\\", "\\\\").replace('"', '\\"')
    return f'"{text}"'


def _render_inventory(rows: list[dict[str, Any]]) -> str:
    output = ["name\tsize_bytes\tmd5"]
    for row in rows:
        output.append(f"{row['name']}\t{row['size_bytes']}\t{row['md5'] or ''}")
    return "\n".join(output) + "\n"


def _md_cell(value: str) -> str:
    return value.replace("|", "\\|")

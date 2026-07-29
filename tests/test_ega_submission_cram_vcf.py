"""Batch CRAM/VCF submission preparation and planning tests."""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType
from types import SimpleNamespace
from unittest.mock import patch

import yaml

from impact_tools.ega.submission import (
    PrepareSubmissionConfig,
    SubmitSubmissionConfig,
    prepare_submission,
    submit_submission,
)
from impact_tools.ega.submission.submit import (
    _build_steps,
    _fetch_file_candidates,
    _validate_execute_payloads,
)


class CramVcfSubmissionTests(unittest.TestCase):
    def test_batch_draft_has_one_dataset_and_per_sample_analysis(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_dir, sample_list, profile = _batch_inputs(root)

            result = prepare_submission(
                PrepareSubmissionConfig(
                    input_dir=input_dir,
                    output_dir=root / "prepared",
                    sample_list=sample_list,
                    profile_file=profile,
                )
            )

            draft = yaml.safe_load(result.draft_file.read_text(encoding="utf-8"))
            self.assertNotIn("sample", draft)
            self.assertEqual([item["_key"] for item in draft["samples"]], ["S1", "S2"])
            self.assertEqual(len(draft["runs"]), 2)
            self.assertEqual(len(draft["analyses"]), 2)
            self.assertNotIn("finalise", draft)
            for run in draft["runs"]:
                tags = {
                    item["tag"]
                    for item in (run.get("extra_attributes") or [])
                }
                self.assertNotIn("index_file_name", tags)
                self.assertNotIn("unencrypted_crai_md5", tags)
            self.assertEqual(
                [item["files"][0]["name"] for item in draft["analyses"]],
                ["S1/S1.vcf.gz", "S2/S2.vcf"],
            )
            self.assertIsInstance(draft["dataset"], dict)

    def test_submission_plan_links_all_runs_and_analyses_to_shared_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_dir, sample_list, profile = _batch_inputs(root)
            prepared = prepare_submission(
                PrepareSubmissionConfig(
                    input_dir=input_dir,
                    output_dir=root / "prepared",
                    sample_list=sample_list,
                    profile_file=profile,
                )
            )

            submitted = submit_submission(
                SubmitSubmissionConfig(
                    draft_file=prepared.draft_file,
                    output_dir=root / "submission",
                )
            )

            dataset = json.loads(
                (submitted.payload_dir / "08_dataset.json").read_text(encoding="utf-8")
            )
            self.assertEqual(
                dataset["run_provisional_ids"],
                ["{run_provisional_id:S1}", "{run_provisional_id:S2}"],
            )
            self.assertEqual(
                dataset["analysis_provisional_ids"],
                ["{analysis_provisional_id:S1}", "{analysis_provisional_id:S2}"],
            )
            analysis_payloads = sorted(submitted.payload_dir.glob("07_analysis_*.json"))
            self.assertEqual(len(analysis_payloads), 2)
            first_analysis = json.loads(analysis_payloads[0].read_text(encoding="utf-8"))
            self.assertEqual(first_analysis["files"], ["{file_provisional_id:S1/S1.vcf.gz.c4gh}"])
            self.assertEqual(first_analysis["chromosomes"], [[51, "CM000663.2"]])
            self.assertEqual(
                first_analysis["sample_provisional_ids"],
                ["{sample_provisional_id:S1}"],
            )

    def test_single_sample_draft_remains_supported(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_dir = root / "S1"
            sample_dir.mkdir()
            _write(sample_dir / "S1.cram")
            _write(sample_dir / "S1.cram.crai")
            _write(sample_dir / "S1.vcf.gz")
            profile = _profile(root)

            prepared = prepare_submission(
                PrepareSubmissionConfig(
                    input_dir=sample_dir,
                    output_dir=root / "prepared",
                    profile_file=profile,
                )
            )
            draft = yaml.safe_load(prepared.draft_file.read_text(encoding="utf-8"))

            self.assertIn("sample", draft)
            self.assertEqual(draft["analysis"]["files"][0]["name"], "S1.vcf.gz")

    def test_fresh_batch_payloads_validate_before_ids_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_dir, sample_list, profile = _batch_inputs(root)
            prepared = prepare_submission(
                PrepareSubmissionConfig(
                    input_dir=input_dir,
                    output_dir=root / "prepared",
                    sample_list=sample_list,
                    profile_file=profile,
                )
            )
            draft = yaml.safe_load(prepared.draft_file.read_text(encoding="utf-8"))
            state = {"ids": {}, "files": {}}
            steps = _build_steps(draft, state)

            _validate_execute_payloads(steps, state)
            self.assertNotIn("09_finalise", {step.name for step in steps})

    def test_profile_defaults_satisfy_batch_experiment_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_dir, sample_list, profile = _batch_inputs(root)

            prepared = prepare_submission(
                PrepareSubmissionConfig(
                    input_dir=input_dir,
                    output_dir=root / "prepared",
                    sample_list=sample_list,
                    profile_file=profile,
                )
            )

            missing = yaml.safe_load(
                prepared.missing_file.read_text(encoding="utf-8")
            )
            for sample_id in ("S1", "S2"):
                required = missing[sample_id]["missing_required_fields"] or {}
                practical = missing[sample_id]["missing_practical_links"] or {}
                self.assertNotIn("ExperimentRequest.library_layout", required)
                self.assertNotIn("ExperimentRequest.library_strategy", required)
                self.assertNotIn(
                    "SubmissionFinaliseRequest.expected_release_date",
                    required,
                )
                self.assertIn(
                    "ExperimentRequest.study_provisional_id",
                    practical,
                )

    def test_batch_rejects_conflicting_shared_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_dir, sample_list, profile = _batch_inputs(root)
            metadata = root / "metadata.json"
            metadata.write_text(
                json.dumps(
                    {
                        "S1": {"dataset_title": "Dataset one"},
                        "S2": {"dataset_title": "Dataset two"},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "different shared 'dataset'"):
                prepare_submission(
                    PrepareSubmissionConfig(
                        input_dir=input_dir,
                        output_dir=root / "prepared",
                        sample_list=sample_list,
                        metadata_file=metadata,
                        profile_file=profile,
                    )
                )

    def test_inbox_lookup_uses_leading_slash_first(self) -> None:
        client = _FakeClient()

        matches = _fetch_file_candidates(client, "S1.vcf.gz.c4gh")

        self.assertEqual(client.prefixes[0], "/S1.vcf.gz.c4gh")
        self.assertEqual(matches[0]["provisional_id"], 42)

    def test_execute_resolves_per_sample_ids_before_shared_dataset(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_dir, sample_list, profile = _batch_inputs(root)
            prepared = prepare_submission(
                PrepareSubmissionConfig(
                    input_dir=input_dir,
                    output_dir=root / "prepared",
                    sample_list=sample_list,
                    profile_file=profile,
                )
            )
            client = _ExecutionClient()

            fake_httpx = ModuleType("httpx")
            fake_httpx.Client = lambda **kwargs: client
            with patch.dict(sys.modules, {"httpx": fake_httpx}):
                result = submit_submission(
                    SubmitSubmissionConfig(
                        draft_file=prepared.draft_file,
                        output_dir=root / "executed",
                        api_base="https://submitter.example.test",
                        token="token",
                        execute=True,
                    )
                )

            self.assertEqual(client.dataset_payload["run_provisional_ids"], [501, 502])
            self.assertEqual(client.dataset_payload["analysis_provisional_ids"], [601, 602])
            state = json.loads(result.state_file.read_text(encoding="utf-8"))
            self.assertEqual(state["ids"]["sample:S1"], "301")
            self.assertEqual(state["ids"]["analysis:S2"], "602")

    def test_resume_rejects_state_from_different_draft(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_dir, sample_list, profile = _batch_inputs(root)
            prepared = prepare_submission(
                PrepareSubmissionConfig(
                    input_dir=input_dir,
                    output_dir=root / "prepared",
                    sample_list=sample_list,
                    profile_file=profile,
                )
            )
            resume_state = root / "resume.json"
            resume_state.write_text(
                json.dumps(
                    {
                        "mode": "execute",
                        "api_base": "https://submitter.example.test",
                        "draft_file": str(prepared.draft_file),
                        "draft_sha256": "not-the-current-digest",
                        "ids": {"submission": "74"},
                    }
                ),
                encoding="utf-8",
            )

            with self.assertRaisesRegex(ValueError, "different submission draft"):
                submit_submission(
                    SubmitSubmissionConfig(
                        draft_file=prepared.draft_file,
                        output_dir=root / "resumed",
                        api_base="https://submitter.example.test",
                        resume_state_file=resume_state,
                    )
                )

    def test_resume_rejects_dry_run_state(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            input_dir, sample_list, profile = _batch_inputs(root)
            prepared = prepare_submission(
                PrepareSubmissionConfig(
                    input_dir=input_dir,
                    output_dir=root / "prepared",
                    sample_list=sample_list,
                    profile_file=profile,
                )
            )
            dry_run = submit_submission(
                SubmitSubmissionConfig(
                    draft_file=prepared.draft_file,
                    output_dir=root / "dry-run",
                )
            )

            with self.assertRaisesRegex(ValueError, "not produced by an executed"):
                submit_submission(
                    SubmitSubmissionConfig(
                        draft_file=prepared.draft_file,
                        output_dir=root / "resumed",
                        resume_state_file=dry_run.state_file,
                    )
                )


def _batch_inputs(root: Path) -> tuple[Path, Path, Path]:
    input_dir = root / "delivery"
    for sample_id, vcf_suffix in (("S1", ".vcf.gz"), ("S2", ".vcf")):
        sample_dir = input_dir / sample_id
        sample_dir.mkdir(parents=True)
        _write(sample_dir / f"{sample_id}.cram")
        _write(sample_dir / f"{sample_id}.cram.crai")
        _write(sample_dir / f"{sample_id}{vcf_suffix}")
    sample_list = root / "samples.txt"
    sample_list.write_text("S1\nS2\n", encoding="utf-8")
    return input_dir, sample_list, _profile(root)


def _profile(root: Path) -> Path:
    profile = root / "profile.json"
    profile.write_text(
        json.dumps(
            {
                "defaults": {
                    "submission": {"title": "Submission", "description": "Description"},
                    "study": {
                        "title": "Study",
                        "description": "Study description",
                        "study_type": "Whole Genome Sequencing",
                    },
                    "sample": {
                        "subject_id": "subject",
                        "biological_sex": "unknown",
                        "phenotype": "normal",
                    },
                    "experiment": {
                        "design_description": "WGS",
                        "instrument_model_id": 81,
                        "library_layout": "PAIRED",
                        "library_strategy": "WGS",
                        "library_source": "GENOMIC",
                        "library_selection": "RANDOM",
                    },
                    "analysis": {
                        "description": "Variant calls",
                        "analysis_type": "SEQUENCE VARIATION",
                        "experiment_types": ["Whole genome sequencing"],
                        "genome_id": 15,
                        "chromosomes": [[51, "CM000663.2"]],
                    },
                    "dataset": {
                        "title": "Dataset",
                        "description": "Dataset description",
                        "dataset_types": ["Whole genome sequencing"],
                        "policy_accession_id": "EGAP50000000110",
                    },
                }
            }
        ),
        encoding="utf-8",
    )
    return profile


def _write(path: Path) -> None:
    path.write_bytes(b"content\n")


class _FakeResponse:
    def __init__(
        self,
        payload: object,
        *,
        status_code: int = 200,
        url: str = "https://submitter.example.test",
    ) -> None:
        self._payload = payload
        self.status_code = status_code
        self.request = SimpleNamespace(url=url)
        self.text = json.dumps(payload)

    def json(self) -> object:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _FakeClient:
    def __init__(self) -> None:
        self.prefixes: list[str | None] = []

    def get(self, path: str, params: dict[str, str]) -> _FakeResponse:
        self.prefixes.append(params.get("prefix"))
        if params.get("prefix") == "/S1.vcf.gz.c4gh":
            return _FakeResponse(
                [
                    {
                        "provisional_id": 42,
                        "relative_path": "/S1.vcf.gz.c4gh",
                    }
                ]
            )
        return _FakeResponse([])


class _ExecutionClient:
    def __init__(self) -> None:
        self.counts = {
            "samples": iter((301, 302)),
            "experiments": iter((401, 402)),
            "runs": iter((501, 502)),
            "analyses": iter((601, 602)),
        }
        self.dataset_payload: dict[str, object] = {}

    def __enter__(self) -> "_ExecutionClient":
        return self

    def __exit__(self, *args: object) -> None:
        return None

    def get(self, path: str, params: dict[str, str]) -> _FakeResponse:
        prefix = params.get("prefix")
        if prefix and prefix.startswith("/") and prefix.endswith(".c4gh"):
            return _FakeResponse(
                [{"provisional_id": abs(hash(prefix)) % 10000 + 1000, "relative_path": prefix}]
            )
        return _FakeResponse([])

    def request(self, method: str, path: str, json: dict[str, object]) -> _FakeResponse:
        if path == "/submissions":
            identifier = 101
        elif path.endswith("/studies"):
            identifier = 201
        elif path.endswith("/datasets"):
            self.dataset_payload = json
            identifier = 701
        else:
            entity = path.rsplit("/", 1)[-1]
            identifier = next(self.counts[entity])
        return _FakeResponse(
            {"provisional_id": identifier},
            status_code=201,
            url=f"https://submitter.example.test{path}",
        )


if __name__ == "__main__":
    unittest.main()

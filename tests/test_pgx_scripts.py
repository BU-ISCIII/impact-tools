"""Tests for the bundled PGx helper scripts (tag_variant_qc, check_beacon_alleles).

These scripts run inside the pgx_pilot container and depend on pysam, which is
not a runtime dependency of impact-tools itself. The whole module is skipped when
pysam is unavailable so the rest of the suite still runs.
"""

from __future__ import annotations

import importlib.util
import tempfile
from pathlib import Path

import pytest

pytest.importorskip("pysam")

_SCRIPTS = (
    Path(__file__).resolve().parent.parent
    / "impact_tools"
    / "beacon"
    / "resources"
    / "scripts"
)


def _load(module_name: str):
    """Load one of the bundled scripts by file path."""
    spec = importlib.util.spec_from_file_location(
        module_name, _SCRIPTS / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(module)
    return module


tag_variant_qc = _load("tag_variant_qc")
check_beacon_alleles = _load("check_beacon_alleles")


_DEFAULT_THRESHOLDS = dict(
    qual=30.0,
    qd=2.0,
    mq=40.0,
    fs=60.0,
    readpos=-8.0,
    hwe=1e-6,
    min_dp=10,
    min_gq=20,
    ab_ratio=0.2,
    max_missing=0.1,
)


class TestAllPositive:
    def test_all_positive(self):
        ap = tag_variant_qc._all_positive
        assert ap(1) is True
        assert ap((2, 5)) is True
        assert ap(0) is False
        assert ap((3, 0)) is False
        assert ap(None) is False
        assert ap(()) is False
        assert ap(".") is False


def _write_vcf(body: str) -> Path:
    header = (
        "##fileformat=VCFv4.2\n"
        "##contig=<ID=chr1,length=10000>\n"
        '##INFO=<ID=AC,Number=A,Type=Integer,Description="Allele count">\n'
        '##INFO=<ID=F_MISSING,Number=1,Type=Float,Description="Fraction missing">\n'
        "#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO\n"
    )
    f = tempfile.NamedTemporaryFile("w", suffix=".vcf", delete=False)
    f.write(header + body)
    f.close()
    return Path(f.name)


class TestFailAc:
    def test_ac_zero_or_missing_fails(self):
        import pysam

        in_vcf = _write_vcf(
            "chr1\t100\t.\tA\tG\t50\t.\tAC=1;F_MISSING=0\n"
            "chr1\t200\t.\tA\tG\t50\t.\tAC=0;F_MISSING=0.05\n"
            "chr1\t300\t.\tA\tG\t50\t.\tAC=0;F_MISSING=0.5\n"
            "chr1\t400\t.\tA\tG\t50\t.\tF_MISSING=0\n"
        )
        out_vcf = in_vcf.with_name(in_vcf.stem + ".out.vcf")
        try:
            tag_variant_qc.tag_variant_qc(in_vcf, out_vcf, **_DEFAULT_THRESHOLDS)
            # htslib splits a comma-containing String INFO value into a tuple on
            # read (e.g. "FAIL_MISSING,FAIL_AC" -> ("FAIL_MISSING","FAIL_AC")),
            # so rejoin before comparing. In production the Snakefile filter
            # QC_STATUS="PASS" excludes these multi-reason records regardless.
            def _norm(v):
                return ",".join(v) if isinstance(v, tuple) else v
            with pysam.VariantFile(str(out_vcf)) as vcf:
                status = {rec.pos: _norm(rec.info.get("QC_STATUS")) for rec in vcf}
            assert status == {
                100: "PASS",
                200: "FAIL_AC",
                300: "FAIL_MISSING,FAIL_AC",
                400: "FAIL_AC",
            }
        finally:
            in_vcf.unlink(missing_ok=True)
            out_vcf.unlink(missing_ok=True)


class TestCheckBeaconAlleles:
    def _check(self, records):
        body = "".join(
            f"chr1\t{pos}\t.\t{ref}\t{alt}\t.\tPASS\t.\n" for pos, ref, alt in records
        )
        vcf = _write_vcf(body)
        try:
            return check_beacon_alleles.find_invalid_alleles(vcf)
        finally:
            vcf.unlink(missing_ok=True)

    def test_uppercase_alleles_pass(self):
        n_invalid, examples = self._check(
            [(100, "A", "G"), (200, "ACGT", "A"), (300, "N", "C,T")]
        )
        assert n_invalid == 0
        assert examples == []

    def test_lowercase_alleles_fail(self):
        n_invalid, examples = self._check(
            [(100, "Cgtgt", "C"), (200, "A", "G,Atg"), (300, "A", "G")]
        )
        assert n_invalid == 2
        assert examples == ["chr1:100:Cgtgt:C", "chr1:200:A:G,Atg"]

    def test_spanning_deletion_fails(self):
        n_invalid, _ = self._check([(100, "A", "*")])
        assert n_invalid == 1

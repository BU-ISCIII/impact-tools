"""Tests for VCF-driven populations.json generation."""

from __future__ import annotations

import gzip

import pytest

from impact_tools.beacon import populations
from impact_tools.beacon.populations import (
    InfoDefinition,
    PopulationsMappingError,
    build_populations_document,
    detect_groups,
    parse_info_definitions,
)


def _freq(id_: str) -> InfoDefinition:
    return InfoDefinition(id=id_, number="A", type="Float")


def _count(id_: str) -> InfoDefinition:
    return InfoDefinition(id=id_, number="A", type="Integer")


def _number(id_: str) -> InfoDefinition:
    return InfoDefinition(id=id_, number="1", type="Integer")


def _defs(*definitions: InfoDefinition) -> dict[str, InfoDefinition]:
    return {definition.id: definition for definition in definitions}


def _triplet(group_suffix: str = "") -> list[InfoDefinition]:
    s = group_suffix
    return [_freq(f"AF{s}"), _count(f"AC{s}"), _number(f"AN{s}")]


# ── Parsing ──────────────────────────────────────────────────────────────────


VCF_HEADER = """\
##fileformat=VCFv4.2
##INFO=<ID=AF,Number=A,Type=Float,Description="Allele frequency, with commas">
##INFO=<ID=AC,Number=A,Type=Integer,Description="Allele count">
##INFO=<ID=AN,Number=1,Type=Integer,Description="Allele number">
##INFO=<ID=AF_ES_F,Number=A,Type=Float,Description="AF for ES females">
##INFO=<ID=AC_ES_F,Number=A,Type=Integer,Description="AC for ES females">
##INFO=<ID=AN_ES_F,Number=1,Type=Integer,Description="AN for ES females">
#CHROM\tPOS\tID\tREF\tALT\tQUAL\tFILTER\tINFO
1\t1\t.\tA\tT\t.\t.\tAF=0.1
"""


def test_parse_info_definitions_plain(tmp_path):
    vcf = tmp_path / "x.vcf"
    vcf.write_text(VCF_HEADER, encoding="utf-8")
    defs = parse_info_definitions(vcf)
    assert set(defs) == {"AF", "AC", "AN", "AF_ES_F", "AC_ES_F", "AN_ES_F"}
    assert defs["AF"] == InfoDefinition(id="AF", number="A", type="Float")
    assert defs["AN"] == InfoDefinition(id="AN", number="1", type="Integer")


def test_parse_info_definitions_gzip(tmp_path):
    vcf = tmp_path / "x.vcf.gz"
    with gzip.open(vcf, "wt", encoding="utf-8") as handle:
        handle.write(VCF_HEADER)
    defs = parse_info_definitions(vcf)
    assert "AF_ES_F" in defs


# ── Group detection ──────────────────────────────────────────────────────────


def test_detect_groups_total_and_compound_suffixes():
    ids = {"AF", "AF_AFR", "AF_ES_F", "AF_EUR_unrel", "AC_Hom_EUR", "MLEAF"}
    # 'Total' from AF; suffixes preserved verbatim; AC_* and MLEAF ignored.
    assert detect_groups(ids) == ["Total", "AFR", "ES_F", "EUR_unrel"]


def test_detect_groups_without_total():
    assert detect_groups({"AF_AFR"}) == ["AFR"]


# ── Document building ────────────────────────────────────────────────────────


def test_build_document_total_and_group_optional_counts_absent():
    defs = _defs(*_triplet(), *_triplet("_ES_F"))
    doc = build_populations_document(defs)

    assert doc["source"] == populations.SOURCE
    assert doc["sourceReference"] == populations.SOURCE_REFERENCE
    assert doc["populations"] == [
        {
            "population": "Total",
            "alleleFrequency": "AF",
            "alleleCount": "AC",
            "alleleNumber": "AN",
        },
        {
            "population": "ES_F",
            "alleleFrequency": "AF_ES_F",
            "alleleCount": "AC_ES_F",
            "alleleNumber": "AN_ES_F",
        },
    ]
    # No forbidden/extra keys slip in.
    for entry in doc["populations"]:
        assert "numberOfPopulations" not in entry
        assert set(entry) <= {
            "population",
            "alleleFrequency",
            "alleleCount",
            "alleleNumber",
            "alleleCountHomozygous",
            "alleleCountHeterozygous",
            "alleleCountHemizygous",
        }


def test_build_document_includes_present_zygosity_both_orderings():
    # Total uses AC_Hom; EUR uses the AC_EUR_Hom ordering.
    defs = _defs(
        *_triplet(),
        _count("AC_Hom"),
        *_triplet("_EUR"),
        _count("AC_EUR_Hom"),
    )
    doc = build_populations_document(defs)
    by_pop = {entry["population"]: entry for entry in doc["populations"]}
    assert by_pop["Total"]["alleleCountHomozygous"] == "AC_Hom"
    assert by_pop["EUR"]["alleleCountHomozygous"] == "AC_EUR_Hom"
    assert "alleleCountHeterozygous" not in by_pop["EUR"]


def test_build_document_ambiguous_zygosity_fails():
    defs = _defs(
        *_triplet("_EUR"),
        _count("AC_Hom_EUR"),
        _count("AC_EUR_Hom"),
    )
    with pytest.raises(PopulationsMappingError, match="Ambiguous"):
        build_populations_document(defs)


def test_build_document_incomplete_triplet_fails():
    # AF_EUR present (so EUR is detected) but AC_EUR/AN_EUR missing.
    defs = _defs(*_triplet(), _freq("AF_EUR"))
    with pytest.raises(PopulationsMappingError, match="Incomplete"):
        build_populations_document(defs)


def test_build_document_bad_definition_fails():
    defs = _defs(
        _freq("AF"),
        _count("AC"),
        InfoDefinition(id="AN", number="A", type="Integer"),  # should be Number=1
    )
    with pytest.raises(PopulationsMappingError, match="unexpected Number/Type"):
        build_populations_document(defs)


def test_build_document_no_allele_frequency_fails():
    defs = _defs(_count("AC"), _number("AN"))
    with pytest.raises(PopulationsMappingError, match="No allele-frequency"):
        build_populations_document(defs)


def test_generate_populations_file_writes_json(tmp_path):
    vcf = tmp_path / "x.vcf"
    vcf.write_text(VCF_HEADER, encoding="utf-8")
    out = tmp_path / "nested" / "populations.json"
    doc = populations.generate_populations_file(vcf, out)
    assert out.is_file()
    assert {entry["population"] for entry in doc["populations"]} == {"Total", "ES_F"}

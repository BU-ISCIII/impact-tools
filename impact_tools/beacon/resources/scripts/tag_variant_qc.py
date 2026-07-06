"""Add variant-level QC annotations to a cohort VCF."""

from __future__ import annotations

import argparse
from pathlib import Path
from typing import Any

import pysam


def _numeric_values(value: Any) -> list[float]:
    """Convert a scalar or tuple-like INFO value into numeric values."""
    if value is None:
        return []

    raw_values = value if isinstance(value, (tuple, list)) else (value,)
    values: list[float] = []

    for item in raw_values:
        if item is None:
            continue

        try:
            values.append(float(item))
        except (TypeError, ValueError):
            continue

    return values


def _any_below(value: Any, threshold: float) -> bool:
    """Return True when any available value is below the threshold."""
    return any(item < threshold for item in _numeric_values(value))


def _any_above(value: Any, threshold: float) -> bool:
    """Return True when any available value is above the threshold."""
    return any(item > threshold for item in _numeric_values(value))


def validate_thresholds(
    *,
    qual: float,
    qd: float,
    mq: float,
    fs: float,
    hwe: float,
    maf: float,
    min_dp: int,
    min_gq: int,
    ab_ratio: float,
    max_missing: float,
) -> None:
    """Validate QC thresholds before processing the VCF."""
    errors: list[str] = []

    if qual < 0:
        errors.append("--qual must be >= 0.")

    if qd < 0:
        errors.append("--qd must be >= 0.")

    if mq < 0:
        errors.append("--mq must be >= 0.")

    if fs < 0:
        errors.append("--fs must be >= 0.")

    if not 0 <= hwe <= 1:
        errors.append("--hwe must be between 0 and 1.")

    if not 0 <= maf <= 0.5:
        errors.append("--maf must be between 0 and 0.5.")

    if min_dp < 0:
        errors.append("--min_dp must be >= 0.")

    if min_gq < 0:
        errors.append("--min_gq must be >= 0.")

    if not 0 <= ab_ratio <= 0.5:
        errors.append("--ab_ratio must be between 0 and 0.5.")

    if not 0 <= max_missing <= 1:
        errors.append("--max_missing must be between 0 and 1.")

    if errors:
        raise ValueError(
            f"{len(errors)} invalid QC threshold(s):\n"
            + "\n".join(f"  {error}" for error in errors)
        )


def tag_variant_qc(
    input_vcf: Path,
    output_vcf: Path,
    *,
    qual: float,
    qd: float,
    mq: float,
    fs: float,
    readpos: float,
    hwe: float,
    maf: float,
    min_dp: int,
    min_gq: int,
    ab_ratio: float,
    max_missing: float,
) -> dict[str, int]:
    """Write a VCF containing the INFO/QC_STATUS annotation.

    The following variant-level fields are checked when present:

    - QUAL
    - INFO/QD
    - INFO/MQ
    - INFO/FS
    - INFO/ReadPosRankSum
    - INFO/DP
    - INFO/HWE
    - INFO/MAF
    - INFO/F_MISSING

    Genotype-level GQ, DP and allele-balance masking is performed earlier by
    the Snakefile. ``min_gq`` and ``ab_ratio`` are accepted here to preserve
    the complete set of QC parameters in the command interface.
    """
    validate_thresholds(
        qual=qual,
        qd=qd,
        mq=mq,
        fs=fs,
        hwe=hwe,
        maf=maf,
        min_dp=min_dp,
        min_gq=min_gq,
        ab_ratio=ab_ratio,
        max_missing=max_missing,
    )

    if not input_vcf.is_file():
        raise FileNotFoundError(f"Input VCF not found: {input_vcf}")

    if input_vcf.resolve() == output_vcf.resolve():
        raise ValueError("Input and output VCF paths must be different.")

    output_vcf.parent.mkdir(parents=True, exist_ok=True)

    counts = {
        "total": 0,
        "pass": 0,
        "fail": 0,
    }

    with pysam.VariantFile(str(input_vcf), "r") as vcf_in:
        # Modify the input header itself so records read from vcf_in recognise
        # the new QC_STATUS field.
        header = vcf_in.header

        if "QC_STATUS" not in header.info:
            header.info.add(
                "QC_STATUS",
                number=1,
                type="String",
                description=(
                    "Variant QC status: PASS or comma-separated failure reasons"
                ),
            )

        # Normally these fields are already declared by bcftools +fill-tags.
        # Add their definitions only when absent.
        info_definitions = {
            "HWE": (
                "A",
                "Float",
                "Hardy-Weinberg equilibrium exact test p-value",
            ),
            "MAF": (
                "1",
                "Float",
                "Minor allele frequency",
            ),
            "F_MISSING": (
                "1",
                "Float",
                "Fraction of missing genotypes",
            ),
        }

        for tag, (number, value_type, description) in info_definitions.items():
            if tag not in header.info:
                header.info.add(
                    tag,
                    number=number,
                    type=value_type,
                    description=description,
                )

        output_mode = "wz" if output_vcf.name.endswith(".gz") else "w"

        with pysam.VariantFile(
            str(output_vcf),
            output_mode,
            header=header,
        ) as vcf_out:
            for record in vcf_in:
                counts["total"] += 1
                reasons: list[str] = []

                if record.qual is not None and record.qual < qual:
                    reasons.append("FAIL_QUAL")

                info = record.info

                if "QD" in info and _any_below(info["QD"], qd):
                    reasons.append("FAIL_QD")

                if "MQ" in info and _any_below(info["MQ"], mq):
                    reasons.append("FAIL_MQ")

                if "FS" in info and _any_above(info["FS"], fs):
                    reasons.append("FAIL_FS")

                if (
                    "ReadPosRankSum" in info
                    and _any_below(info["ReadPosRankSum"], readpos)
                ):
                    reasons.append("FAIL_ReadPosRankSum")

                if "DP" in info and _any_below(info["DP"], float(min_dp)):
                    reasons.append("FAIL_DP")

                if "HWE" in info and _any_below(info["HWE"], hwe):
                    reasons.append("FAIL_HWE")

                if "MAF" in info and _any_below(info["MAF"], maf):
                    reasons.append("FAIL_MAF")

                if (
                    "F_MISSING" in info
                    and _any_above(info["F_MISSING"], max_missing)
                ):
                    reasons.append("FAIL_MISSING")

                if reasons:
                    record.info["QC_STATUS"] = ",".join(reasons)
                    counts["fail"] += 1
                else:
                    record.info["QC_STATUS"] = "PASS"
                    counts["pass"] += 1

                vcf_out.write(record)

    return counts


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Add INFO/QC_STATUS to a cohort VCF."
    )

    parser.add_argument(
        "input_vcf",
        type=Path,
        help="Input VCF or VCF.GZ.",
    )
    parser.add_argument(
        "output_vcf",
        type=Path,
        help="Output VCF or VCF.GZ.",
    )

    parser.add_argument("--qual", type=float, default=30.0)
    parser.add_argument("--qd", type=float, default=2.0)
    parser.add_argument("--mq", type=float, default=40.0)
    parser.add_argument("--fs", type=float, default=60.0)
    parser.add_argument("--readpos", type=float, default=-8.0)
    parser.add_argument("--hwe", type=float, default=1e-6)
    parser.add_argument("--maf", type=float, default=0.0)
    parser.add_argument("--min_dp", type=int, default=10)
    parser.add_argument("--min_gq", type=int, default=20)
    parser.add_argument("--ab_ratio", type=float, default=0.2)
    parser.add_argument("--max_missing", type=float, default=0.1)

    return parser


def main() -> None:
    args = build_parser().parse_args()

    counts = tag_variant_qc(
        args.input_vcf,
        args.output_vcf,
        qual=args.qual,
        qd=args.qd,
        mq=args.mq,
        fs=args.fs,
        readpos=args.readpos,
        hwe=args.hwe,
        maf=args.maf,
        min_dp=args.min_dp,
        min_gq=args.min_gq,
        ab_ratio=args.ab_ratio,
        max_missing=args.max_missing,
    )

    print(
        "QC tagging completed: "
        f"{counts['total']} variants, "
        f"{counts['pass']} PASS, "
        f"{counts['fail']} failed"
    )


if __name__ == "__main__":
    main()
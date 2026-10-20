"""Check that REF/ALT alleles in a VCF are accepted by Beacon."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

import pysam

# Pattern Beacon v2 applies to referenceBases and alternateBases (LegacyVariation
# in EGA-archive/beacon2-ri-tools-v2, validators/ga4gh/vcf.py). Uppercase IUPAC
# codes only: soft-masked lowercase bases and the spanning-deletion '*' are
# rejected.
BEACON_ALLELE_PATTERN = re.compile(r"^([ACGTUNRYSWKMBDHV\-\.]*)$")


def find_invalid_alleles(
    vcf_path: str | Path,
    max_examples: int = 10,
) -> tuple[int, list[str]]:
    """Find records whose REF or any ALT allele does not match the Beacon pattern.

    Returns the number of offending records and up to ``max_examples`` of them
    formatted as ``CHROM:POS:REF:ALT``.
    """
    n_invalid = 0
    examples: list[str] = []

    with pysam.VariantFile(str(vcf_path)) as vcf:
        for record in vcf:
            alts = list(record.alts or ())
            alleles = [record.ref or ""] + alts

            if not all(BEACON_ALLELE_PATTERN.match(allele) for allele in alleles):
                n_invalid += 1
                if len(examples) < max_examples:
                    examples.append(
                        f"{record.chrom}:{record.pos}:{record.ref}:{','.join(alts)}"
                    )

    return n_invalid, examples


def main() -> None:
    """Parse CLI arguments and exit with an error if any allele is rejected."""
    parser = argparse.ArgumentParser(
        description="Check REF/ALT alleles against the Beacon allele pattern."
    )
    parser.add_argument("vcf", help="VCF to check, e.g. the sites.pass output.")
    args = parser.parse_args()

    n_invalid, examples = find_invalid_alleles(args.vcf)

    if n_invalid:
        print(
            f"Error: {n_invalid} record(s) in {args.vcf} have REF/ALT alleles that "
            f"Beacon rejects (allowed: {BEACON_ALLELE_PATTERN.pattern}). "
            "First records:",
            file=sys.stderr,
        )
        for example in examples:
            print(f"  {example}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

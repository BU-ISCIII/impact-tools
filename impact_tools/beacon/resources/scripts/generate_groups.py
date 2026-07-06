"""Generate bcftools sample-group mappings from PGx sample metadata."""

from __future__ import annotations

import argparse
from pathlib import Path


VALID_SEX_VALUES = {"M", "F", "unknown"}


def _normalise_suffix(suffix: str) -> str:
    suffix = suffix.strip()

    if not suffix:
        return ""

    return suffix if suffix.startswith("_") else f"_{suffix}"


def generate_groups(
    samples_tsv: Path,
    output_file: Path,
    *,
    suffix: str = "",
) -> None:
    """Create the group mapping consumed by ``bcftools +fill-tags -S``.

    Input columns:

        sample_id, sex, country_code[, batch_id[, ancestry_group]]

    Output format:

        sample_id<TAB>group1,group2,...

    Generated groups:

        country
        sex
        country_sex

    Samples with sex ``unknown`` are included in country groups but are not
    assigned to sex-specific groups.
    """
    if not samples_tsv.is_file():
        raise ValueError(f"Samples TSV not found: {samples_tsv}")

    suffix = _normalise_suffix(suffix)

    output_lines: list[str] = []
    seen_samples: set[str] = set()
    errors: list[str] = []

    with samples_tsv.open(encoding="utf-8") as handle:
        for line_number, raw_line in enumerate(handle, start=1):
            line = raw_line.strip()

            if not line or line.startswith("#"):
                continue

            fields = line.split("\t")

            if fields[0].strip().lower() == "sample_id":
                continue

            if len(fields) < 3:
                errors.append(
                    f"Line {line_number}: expected at least 3 tab-separated "
                    f"columns, got {len(fields)}."
                )
                continue

            sample_id = fields[0].strip()
            sex = fields[1].strip()
            country_code = fields[2].strip().upper()

            if not sample_id:
                errors.append(f"Line {line_number}: empty sample_id.")
                continue

            if sample_id in seen_samples:
                errors.append(
                    f"Line {line_number}: duplicate sample_id {sample_id!r}."
                )
                continue

            seen_samples.add(sample_id)

            if sex not in VALID_SEX_VALUES:
                errors.append(
                    f"Line {line_number}: invalid sex {sex!r} for "
                    f"{sample_id!r}. Accepted: M, F or unknown."
                )
                continue

            groups: list[str] = []

            if country_code:
                groups.append(f"{country_code}{suffix}")

            if sex in {"M", "F"}:
                groups.append(f"{sex}{suffix}")

                if country_code:
                    groups.append(f"{country_code}_{sex}{suffix}")

            if not groups:
                errors.append(
                    f"Line {line_number}: no groups could be generated for "
                    f"{sample_id!r}."
                )
                continue

            output_lines.append(
                f"{sample_id}\t{','.join(groups)}"
            )

    if errors:
        raise ValueError(
            f"{len(errors)} error(s) while reading {samples_tsv}:\n"
            + "\n".join(f"  {error}" for error in errors)
        )

    if not output_lines:
        raise ValueError(
            f"No sample groups were generated from {samples_tsv}"
        )

    output_file.parent.mkdir(parents=True, exist_ok=True)
    output_file.write_text(
        "\n".join(output_lines) + "\n",
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description=(
            "Generate the sample-group mapping used by "
            "bcftools +fill-tags -S."
        )
    )
    parser.add_argument(
        "samples_tsv",
        type=Path,
        help="Input samples.tsv file.",
    )
    parser.add_argument(
        "output_file",
        type=Path,
        help="Destination group mapping file.",
    )
    parser.add_argument(
        "--suffix",
        default="",
        help="Optional suffix added to every group name, for example raw.",
    )

    args = parser.parse_args()

    generate_groups(
        args.samples_tsv,
        args.output_file,
        suffix=args.suffix,
    )


if __name__ == "__main__":
    main()
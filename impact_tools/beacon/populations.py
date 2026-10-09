"""Build RI-tools ``populations.json`` from a VCF's ``##INFO`` definitions.

``beacon2-ri-tools-v2`` consumes a ``populations.json`` that maps Beacon
allele-frequency properties to VCF ``INFO`` tag *names* (never values). The
installed script ships a fixed template and validates every VCF against it,
which fails whenever a VCF lacks one of the template's tags (e.g. ``AC_Hemi``).

This module derives that mapping from each VCF instead: it reads the ``##INFO``
definitions, detects the population groups actually present and emits a mapping
that matches the installed RI-tools models (``AllelePopulation``, which is
``extra='forbid'`` and requires ``alleleFrequency`` while treating the zygosity
counts as optional).

The document never invents tags, never substitutes missing data and never
derives ``source``/``sourceReference`` from the VCF -- those are supplied by the
caller.
"""

from __future__ import annotations

import dataclasses
import gzip
import json
import logging
import re
from pathlib import Path

LOGGER = logging.getLogger(__name__)

# Property keys of the RI-tools ``AllelePopulation`` model (extra='forbid').
# The frequency/count/number triplet is required; zygosity counts are optional.
_FREQUENCY = "alleleFrequency"
_COUNT = "alleleCount"
_NUMBER = "alleleNumber"
_HOMOZYGOUS = "alleleCountHomozygous"
_HETEROZYGOUS = "alleleCountHeterozygous"
_HEMIZYGOUS = "alleleCountHemizygous"

# Implicit group identified by the unsuffixed ``AF`` tag.
TOTAL = "Total"

_AF_PREFIX = "AF_"

# Provenance of the allele-frequency data. RI-tools requires ``source`` and
# ``sourceReference`` (both are mandatory in its pydantic model and are
# persisted to MongoDB with every frequency). Fixed for the ingested VCFs;
# never inferred from the VCF contents.
SOURCE = "The Genome of Europe"
SOURCE_REFERENCE = "https://genomeofeurope.eu/"


class PopulationsMappingError(ValueError):
    """Raised when a VCF cannot be mapped to a valid ``populations.json``."""


@dataclasses.dataclass(frozen=True)
class InfoDefinition:
    """One ``##INFO`` header definition (only the fields we validate)."""

    id: str
    number: str
    type: str


@dataclasses.dataclass(frozen=True)
class PopulationMapping:
    """Resolved tag mapping for one population group."""

    population: str
    allele_frequency: str
    allele_count: str
    allele_number: str
    allele_count_homozygous: str | None = None
    allele_count_heterozygous: str | None = None
    allele_count_hemizygous: str | None = None

    def as_json(self) -> dict[str, str]:
        """Return the population entry with only the keys RI-tools accepts."""
        entry = {
            "population": self.population,
            _FREQUENCY: self.allele_frequency,
            _COUNT: self.allele_count,
            _NUMBER: self.allele_number,
        }
        if self.allele_count_homozygous is not None:
            entry[_HOMOZYGOUS] = self.allele_count_homozygous
        if self.allele_count_heterozygous is not None:
            entry[_HETEROZYGOUS] = self.allele_count_heterozygous
        if self.allele_count_hemizygous is not None:
            entry[_HEMIZYGOUS] = self.allele_count_hemizygous
        return entry


_INFO_LINE = re.compile(r"^##INFO=<(?P<body>.+)>\s*$")


def _is_gzip(path: Path) -> bool:
    with open(path, "rb") as handle:
        return handle.read(2) == b"\x1f\x8b"


def _split_top_level(body: str) -> list[str]:
    """Split an ``##INFO`` body on commas that are outside double quotes."""
    parts: list[str] = []
    current: list[str] = []
    in_quotes = False
    for char in body:
        if char == '"':
            in_quotes = not in_quotes
            current.append(char)
        elif char == "," and not in_quotes:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def parse_info_definitions(vcf_path: Path) -> dict[str, InfoDefinition]:
    """Parse ``ID``/``Number``/``Type`` from every ``##INFO`` header line."""
    vcf_path = Path(vcf_path)
    definitions: dict[str, InfoDefinition] = {}
    opener = gzip.open if _is_gzip(vcf_path) else open
    with opener(vcf_path, "rt", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            if line.startswith("#CHROM"):
                break
            if not line.startswith("##INFO="):
                continue
            match = _INFO_LINE.match(line.strip())
            if not match:
                continue
            fields: dict[str, str] = {}
            for chunk in _split_top_level(match.group("body")):
                key, sep, value = chunk.partition("=")
                if sep:
                    fields[key.strip()] = value.strip().strip('"')
            info_id = fields.get("ID")
            if info_id:
                definitions[info_id] = InfoDefinition(
                    id=info_id,
                    number=fields.get("Number", ""),
                    type=fields.get("Type", ""),
                )
    return definitions


def detect_groups(info_ids: set[str]) -> list[str]:
    """Return population groups, derived only from ``AF`` / ``AF_<group>`` tags.

    ``AF`` yields the implicit ``Total`` group. ``AF_<group>`` yields
    ``<group>`` with the suffix preserved verbatim (``AF_ES_F`` -> ``ES_F``;
    ``AF_EUR_unrel`` -> ``EUR_unrel``). Allele-count tags never define a group:
    ``AC_Hom_EUR`` is a metric of ``EUR``, not a population ``Hom_EUR``.
    """
    groups: list[str] = []
    if "AF" in info_ids:
        groups.append(TOTAL)
    suffixes = sorted(
        info_id[len(_AF_PREFIX):]
        for info_id in info_ids
        if info_id.startswith(_AF_PREFIX) and len(info_id) > len(_AF_PREFIX)
    )
    groups.extend(suffixes)
    return groups


def _tags_for_group(group: str) -> tuple[str, str, str, dict[str, list[str]]]:
    """Return (frequency, count, number, zygosity_candidates) for a group.

    Zygosity candidates list both supported tag orderings; the caller selects
    whichever exists and rejects the case where both are present.
    """
    if group == TOTAL:
        return (
            "AF",
            "AC",
            "AN",
            {
                _HOMOZYGOUS: ["AC_Hom"],
                _HETEROZYGOUS: ["AC_Het"],
                _HEMIZYGOUS: ["AC_Hemi"],
            },
        )
    return (
        f"AF_{group}",
        f"AC_{group}",
        f"AN_{group}",
        {
            _HOMOZYGOUS: [f"AC_Hom_{group}", f"AC_{group}_Hom"],
            _HETEROZYGOUS: [f"AC_Het_{group}", f"AC_{group}_Het"],
            _HEMIZYGOUS: [f"AC_Hemi_{group}", f"AC_{group}_Hemi"],
        },
    )


def _check_definition(
    tag: str,
    kind: str,
    definitions: dict[str, InfoDefinition],
    problems: list[str],
) -> None:
    """Validate the ``Number``/``Type`` of an ``INFO`` definition by role."""
    expected = {
        "frequency": ("Float", "A"),
        "count": ("Integer", "A"),
        "number": ("Integer", "1"),
    }[kind]
    definition = definitions.get(tag)
    if definition is None:
        return
    if (definition.type, definition.number) != expected:
        problems.append(
            f"{tag}: expected Type={expected[0]},Number={expected[1]}; "
            f"got Type={definition.type or '?'},Number={definition.number or '?'}"
        )


def build_population_mappings(
    definitions: dict[str, InfoDefinition],
) -> list[PopulationMapping]:
    """Resolve and validate the population mappings present in a VCF."""
    info_ids = set(definitions)
    groups = detect_groups(info_ids)
    if not groups:
        raise PopulationsMappingError(
            "No allele-frequency INFO fields (AF or AF_<group>) found in the "
            "VCF header; cannot build a populations mapping."
        )

    incomplete: list[str] = []
    ambiguous: list[str] = []
    bad_definitions: list[str] = []
    mappings: list[PopulationMapping] = []

    for group in groups:
        frequency, count, number, zygosity = _tags_for_group(group)

        missing = [tag for tag in (frequency, count, number) if tag not in info_ids]
        if missing:
            incomplete.append(f"{group}: missing {', '.join(missing)}")
            continue

        resolved: dict[str, str] = {}
        for prop, candidates in zygosity.items():
            present = [tag for tag in candidates if tag in info_ids]
            if len(present) > 1:
                ambiguous.append(
                    f"{group}/{prop}: both {' and '.join(present)} present"
                )
            elif present:
                resolved[prop] = present[0]

        _check_definition(frequency, "frequency", definitions, bad_definitions)
        _check_definition(count, "count", definitions, bad_definitions)
        _check_definition(number, "number", definitions, bad_definitions)
        for tag in resolved.values():
            _check_definition(tag, "count", definitions, bad_definitions)

        mappings.append(
            PopulationMapping(
                population=group,
                allele_frequency=frequency,
                allele_count=count,
                allele_number=number,
                allele_count_homozygous=resolved.get(_HOMOZYGOUS),
                allele_count_heterozygous=resolved.get(_HETEROZYGOUS),
                allele_count_hemizygous=resolved.get(_HEMIZYGOUS),
            )
        )

    errors: list[str] = []
    if incomplete:
        errors.append(
            "Incomplete AF/AC/AN triplet(s):\n  " + "\n  ".join(incomplete)
        )
    if ambiguous:
        errors.append(
            "Ambiguous zygosity tags (both orderings present):\n  "
            + "\n  ".join(ambiguous)
        )
    if bad_definitions:
        errors.append(
            "INFO definitions with unexpected Number/Type:\n  "
            + "\n  ".join(bad_definitions)
        )
    if errors:
        raise PopulationsMappingError("\n\n".join(errors))

    return mappings


def build_populations_document(
    definitions: dict[str, InfoDefinition],
) -> dict:
    """Return the full ``populations.json`` document for a VCF."""
    mappings = build_population_mappings(definitions)
    LOGGER.info(
        "Detected %d population group(s): %s",
        len(mappings),
        ", ".join(mapping.population for mapping in mappings),
    )
    return {
        "source": SOURCE,
        "sourceReference": SOURCE_REFERENCE,
        "populations": [mapping.as_json() for mapping in mappings],
    }


def generate_populations_file(
    vcf_path: Path,
    output_path: Path,
) -> dict:
    """Generate the ``populations.json`` for ``vcf_path`` and write it."""
    definitions = parse_info_definitions(Path(vcf_path))
    document = build_populations_document(definitions)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(document, indent=4, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    LOGGER.info("Generated populations mapping: %s", output_path)
    for entry in document["populations"]:
        LOGGER.debug(
            "  %s -> %s",
            entry["population"],
            {key: value for key, value in entry.items() if key != "population"},
        )
    return document

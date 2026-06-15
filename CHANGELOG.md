# impact-tools Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
where possible.

## [Unreleased]

### Credits

- [Alejandro Bernabeu](https://github.com/Aberdur)
- [Magdalena Matito](https://github.com/magdasmat)

### Added

- Added SLURM encryption execution support to generate chunk files, task
  manifests, reusable `sbatch` array scripts and optional job submission for
  large Affiliated EGA encryption batches.
- Added persistent user configuration through `~/.impact_tools/extra_config.json`
  for logs, EGA encryption, Inbox uploads and SLURM defaults.
- Added Beacon dataset ingestion workflow through `impact-tools beacon ingest dataset`. [#8](https://github.com/BU-ISCIII/impact-tools/pull/8)
- Added a persistent SHA-256 processing registry to avoid re-encrypting or
  resubmitting files that reappear in later batches or under different paths.
- Added local, workstation and HPC execution profiles with host, process and
  SLURM metrics for comparable EGA runs.
- Added an end-to-end encryption and Inbox upload wrapper plus self-contained
  HTML dashboards for individual runs and independent or combined WS/HPC
  comparisons of encryption, upload and complete workflows.
- Added Beacon variants ingestion workflow, metrics reports and some pgx run fixes [#11](https://github.com/BU-ISCIII/impact-tools/pull/11)

## [0.1.0] - 2026-05-21

### Credits

- [Alejandro Bernabeu](https://github.com/Aberdur)
- [Magdalena Matito](https://github.com/magdasmat)

### Added

- Created the initial `impact-tools` package and CLI structure for Go-IMPaCT
  Beacon and Affiliated EGA operational workflows.[#2](https://github.com/BU-ISCIII/impact-tools/pull/2)
- Added Affiliated EGA commands to encrypt files with Crypt4GH and upload
  encrypted `.c4gh` files to a LocalEGA Inbox, including logs, metrics,
  summaries and manifests. [#2](https://github.com/BU-ISCIII/impact-tools/pull/2)
- Added Beacon `liftover` command to detect genome build and lift over VCFs from
  GRCh37 to GRCh38 using CrossMap via Docker and `pgx` command to prepare
  per-sample pgx_pilot workspaces and run the pgx_pilot Snakemake pipeline [#3](https://github.com/BU-ISCIII/impact-tools/pull/3)
- Fixed HPC symlink handling, Docker mount, sample ID mapping and bundled patched Snakefile for `beacon liftover` and `beacon pgx`. [#4](https://github.com/BU-ISCIII/impact-tools/pull/4)

# impact-tools Changelog

All notable changes to this project will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.0.0/),
and this project follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html)
where possible.

## [Unreleased]

### Notes

- Pending changes for the next release will be listed here.

## [0.1.0] - 2026-05-21

### Credits

- [Alejandro Bernabeu](https://github.com/Aberdur)

### Repository

- Created the initial `impact-tools` Python package structure.
- Added package metadata and editable installation support through `pyproject.toml`.
- Added a micromamba/conda environment definition with the required CLI,
  SFTP, plotting and Crypt4GH dependencies.
- Added top-level documentation describing the Beacon and Affiliated EGA
  workstreams.

### Beacon

- Added the initial documentation scaffold for Beacon-related operational notes.

### Affiliated EGA

#### Added

- Added the `impact-tools ega` command group.
- Added `impact-tools ega encrypt` to encrypt sequencing files with Crypt4GH.
- Added support for encrypting files discovered by glob pattern.
- Added support for encrypting a controlled set of files from a text file with
  one path per line.
- Added configurable Crypt4GH executable selection with `--crypt4gh-bin`.
- Added dry-run mode for encryption planning.
- Added resumable batch behavior through `--force` and `--fail-fast`.
- Added per-file encryption metrics including:
  - input and output size;
  - size ratio and overhead;
  - runtime and throughput;
  - input and encrypted SHA256 checksums;
  - status and error information.
- Added encryption summary reports, machine-readable manifests and detailed log
  files.
- Added optional PNG plots for encryption metrics when `matplotlib` is
  installed.
- Added `impact-tools ega upload-inbox` to upload encrypted `.c4gh` files to a
  LocalEGA Inbox over SFTP.
- Added password prompting and optional SSH identity-file support for Inbox SFTP
  authentication.
- Added host key handling modes for SFTP connections.
- Added flat and relative remote upload layouts.
- Added dry-run mode for Inbox upload planning.
- Added per-file upload metrics including:
  - local file size;
  - upload runtime and throughput;
  - local SHA256 checksum;
  - status and error information.
- Added upload summary reports, machine-readable manifests and detailed log
  files.

#### Operational notes

- The Inbox upload command transfers encrypted files to the LocalEGA Inbox. The
  downstream ingestion, accessioning, dataset mapping, release, DAC permission
  propagation and distribution steps remain part of the LocalEGA / CEGA
  workflow.

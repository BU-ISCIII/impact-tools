# impact-tools

Operational tooling for the Go-IMPaCT data platform at ISCIII-CIBER.

This repository groups reusable command-line tools and documentation for the two
main technical workstreams around Go-IMPaCT genomic data operations:

| Area | Purpose | Current status |
| --- | --- | --- |
| Beacon | Support data discovery workflows around Beacon v2 deployments. | Documentation scaffold. |
| Affiliated EGA | Prepare, encrypt and transfer files for a LocalEGA / Federated EGA Affiliate deployment. | Initial CLI tools available. |

## Repository Map

```text
impact-tools/
├── impact_tools/
│   ├── __main__.py              # CLI entry point: impact-tools
│   └── ega/
│       ├── encrypt.py           # Crypt4GH encryption workflow
│       └── upload_inbox.py      # LocalEGA Inbox SFTP upload workflow
├── docs/
│   ├── beacon/                  # Beacon workflow notes
│   └── ega/                     # Affiliated EGA workflow notes
├── environment.yml              # Conda/micromamba development environment
└── pyproject.toml               # Python package metadata
```

## Quick Start

Create the environment and install the package in editable mode:

```bash
micromamba create -f environment.yml
micromamba activate impact-tools
```

Check the CLI:

```bash
impact-tools --help
impact-tools ega --help
```

## Beacon Workstream

The Beacon part of this repository is intended to collect tools and operational
documentation for the Go-IMPaCT Beacon v2 deployment.

Planned scope:

| Topic | Description |
| --- | --- |
| Data preparation | Helpers to validate and transform genomic metadata before Beacon ingestion. |
| Ingestion support | Scripts and checks around loading data into the Beacon backend. |
| Deployment notes | Operational documentation for running and maintaining the Beacon stack. |
| Query validation | Reproducible checks for Beacon discovery and variant queries. |

Documentation entry point:

```text
docs/beacon/README.md
```

## Affiliated EGA Workstream

The Affiliated EGA tooling covers the operational steps needed before data can
be managed through the LocalEGA / Federated EGA Affiliate workflow.

Current focus:

1. Encrypt raw sequencing files with Crypt4GH.
2. Generate auditable metrics for encryption runs.
3. Upload encrypted `.c4gh` files to the LocalEGA Inbox over SFTP.
4. Generate upload metrics and manifests for traceability.

### Workflow Overview

```text
Raw sequencing files
        |
        v
impact-tools ega encrypt
        |
        |  .c4gh files
        |  metrics TSV
        |  summary TXT
        |  manifest JSON
        |  execution log
        v
Encrypted working area
        |
        v
impact-tools ega upload-inbox
        |
        v
LocalEGA Inbox SFTP
        |
        v
LocalEGA / CEGA ingestion, accessioning, release and distribution
```

### Encrypt Files

Encrypt all files matching the default pattern `*.fastq.gz`:

```bash
impact-tools ega encrypt \
  --input-dir /path/to/raw_data/ND1772 \
  --recipient-pubkey /path/to/service.key.pub
```

Use a specific Crypt4GH executable, for example from a micromamba environment:

```bash
impact-tools ega encrypt \
  --input-dir /path/to/raw_data/ND1772 \
  --recipient-pubkey /path/to/service.key.pub \
  --crypt4gh-bin /path/to/env/bin/crypt4gh
```

Preview the run without encrypting:

```bash
impact-tools ega encrypt \
  --input-dir /path/to/raw_data/ND1772 \
  --recipient-pubkey /path/to/service.key.pub \
  --dry-run
```

Encrypt only files listed in a text file:

```bash
impact-tools ega encrypt \
  --input-dir /path/to/raw_data \
  --input-list files_to_encrypt.txt \
  --recipient-pubkey /path/to/service.key.pub
```

Input lists accept one file per line. Empty lines and lines starting with `#`
are ignored. Relative paths are resolved from `--input-dir`.

```text
# files_to_encrypt.txt
ND1772/ND1772_S19_R1_001.fastq.gz
/impact_data/raw_data/lega/ND1772/ND1772_S19_R2_001.fastq.gz
```

By default, encrypted files are written to:

```text
<input-dir>/encrypted_c4gh/<sample-id>/
```

### Encryption Outputs

Each encryption run writes a complete audit bundle:

| File | Content |
| --- | --- |
| `encryption_metrics_<run_id>.tsv` | Per-file size, checksum, runtime, throughput, status and error fields. |
| `encryption_summary_<run_id>.txt` | Human-readable batch summary. |
| `encryption_manifest_<run_id>.json` | Machine-readable run manifest. |
| `encryption_<run_id>.log` | Detailed execution log. |
| `plots_<run_id>/` | Optional PNG plots when `matplotlib` is installed. |

### Upload to LocalEGA Inbox

Upload encrypted `.c4gh` files to an Inbox SFTP endpoint:

```bash
impact-tools ega upload-inbox \
  --input-dir /impact_data/raw_data/lega/encrypted_c4gh \
  --host localhost \
  --port 2222 \
  --username '<ega-user@example.org>' \
  --ask-password
```

By default, files are uploaded with `--remote-layout flat`, which places every
file directly under the remote Inbox directory. This matches the manual SFTP
workflow:

```sftp
put sample.fastq.gz.c4gh
```

Upload only selected files:

```bash
impact-tools ega upload-inbox \
  --input-dir /impact_data/raw_data/lega/encrypted_c4gh \
  --input-list files_to_upload.txt \
  --host localhost \
  --port 2222 \
  --username '<ega-user@example.org>' \
  --ask-password
```

Preserve relative sample directories remotely:

```bash
impact-tools ega upload-inbox \
  --input-dir /impact_data/raw_data/lega/encrypted_c4gh \
  --remote-layout relative \
  --host localhost \
  --port 2222 \
  --username '<ega-user@example.org>' \
  --ask-password
```

### Upload Outputs

Each upload run writes:

| File | Content |
| --- | --- |
| `inbox_upload_metrics_<run_id>.tsv` | Per-file upload status, size, timing, throughput and checksum. |
| `inbox_upload_summary_<run_id>.txt` | Human-readable batch summary. |
| `inbox_upload_manifest_<run_id>.json` | Machine-readable upload manifest. |
| `inbox_upload_<run_id>.log` | Detailed execution log. |

## Operational Notes

### Crypt4GH

`impact-tools ega encrypt` needs a working `crypt4gh` executable. If multiple
Python or micromamba environments are available, pass the exact binary with
`--crypt4gh-bin` to avoid using a broken system installation.

### LocalEGA Scope

The upload command transfers encrypted files to the Inbox. The subsequent
LocalEGA ingestion, accessioning, dataset mapping, release, DAC permission
propagation and distribution steps are handled by the LocalEGA / CEGA workflow.

## Development

Run syntax checks:

```bash
python3 -m py_compile \
  impact_tools/__main__.py \
  impact_tools/ega/encrypt.py \
  impact_tools/ega/upload_inbox.py
```

Inspect CLI help:

```bash
python3 -m impact_tools --help
python3 -m impact_tools ega encrypt --help
python3 -m impact_tools ega upload-inbox --help
```

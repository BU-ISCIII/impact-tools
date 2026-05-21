# impact-tools

Operational tooling for the Go-IMPaCT data platform at ISCIII-CIBER.

This repository groups reusable command-line tools and documentation for the two
main technical workstreams around Go-IMPaCT genomic data operations:

| Area | Purpose | Current status |
| --- | --- | --- |
| Beacon | Support data discovery workflows around Beacon v2 deployments. | Initial CLI tools available. |
| Affiliated EGA | Prepare, encrypt and transfer files for a LocalEGA / Federated EGA Affiliate deployment. | Initial CLI tools available. |

## Repository Map

```text
impact-tools/
├── impact_tools/
│   ├── __main__.py              # CLI entry point: impact-tools
│   ├── beacon/
│   │   ├── liftover.py          # Beacon liftover workflow (GRCh37 -> GRCh38)
│   │   └── pgx.py               # pgx_pilot workspace preparation and execution
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
impact-tools beacon --help
impact-tools ega --help
```

## Beacon Workstream

The Beacon tooling covers the operational steps needed to prepare genomic data
for ingestion into a Beacon v2 deployment.

Current focus:

1. Detect the genome build of input VCFs.
2. Lift over variants from GRCh37 to GRCh38 using CrossMap via Docker.
3. Fix contig headers, sort, compress and index the output.
4. Clean obsolete INFO annotations incompatible with downstream ingestion.
5. Infer sample sex from non-ref chrY variant counts.
6. Prepare per-sample pgx_pilot workspaces (symlinks, config, samples.tsv).
7. Run the pgx_pilot Snakemake pipeline via Docker to produce sites-only VCFs.

### Workflow Overview

```text
WGS VCFs (GRCh37, inputs/)
        |
        v
impact-tools beacon liftover
        |
        |  <sample>.GRCh38.clean.vcf.gz
        |  <sample>.GRCh38.clean.vcf.gz.tbi
        v
Lifted VCFs (GRCh38, liftover/)
        |
        v
impact-tools beacon pgx
        |
        |  inputs/samples.tsv          (sex inferred from chrY)
        |  pgx_runs/<sample>/          (workspace per sample)
        |  <sample>.sites.pass.vcf.gz  (Beacon-ready, PASS QC)
        v
Beacon v2 ingestion pipeline
```

### Liftover VCFs

The command expects input VCFs under `<base-dir>/inputs/` and liftover resources
under `<base-dir>/liftover/resources/`:

```text
<base-dir>/
├── inputs/                                                    # *.vcf.gz, may be symlinks
└── liftover/
    └── resources/
        ├── hg19ToHg38.over.chain.gz
        ├── GRCh38_full_analysis_set_plus_decoy_hla.fa
        └── GRCh38_full_analysis_set_plus_decoy_hla.fa.fai
```

The `liftover/` and `logs/` directories are created automatically on first run.

Check input builds without running liftover:

```bash
impact-tools beacon liftover --check
```

Run liftover from a specific working directory:

```bash
impact-tools beacon liftover \
  --base-dir /home/mmatitos/beacon_demo
```

Remove intermediate files after a successful run:

```bash
impact-tools beacon liftover \
  --base-dir /home/mmatitos/beacon_demo \
  --cleanup
```

Use alternative resource paths when chain and reference FASTA are not in the
default `resources/` location:

```bash
impact-tools beacon liftover \
  --chain /data/resources/hg19ToHg38.over.chain.gz \
  --fasta /data/resources/GRCh38_full_analysis_set_plus_decoy_hla.fa
```

Process samples in parallel (default 4 workers):

```bash
impact-tools beacon liftover --base-dir /home/mmatitos/beacon_demo --workers 8
```

### Liftover Outputs

Each liftover run writes per sample under `<base-dir>/liftover/`:

| File | Content |
| --- | --- |
| `<sample>.GRCh38.clean.vcf.gz` | Lifted, sorted VCF with obsolete INFO tags removed. |
| `<sample>.GRCh38.clean.vcf.gz.tbi` | Tabix index. |
| `<sample>_liftover.log` | Full stdout/stderr log from bcftools and CrossMap. |

### Prepare and Run pgx_pilot

The command reads lifted VCFs from `liftover/`, infers sample sex by counting
non-ref variants on chrY, creates one workspace per sample under `pgx_runs/`,
and runs the pgx_pilot Snakemake pipeline via Docker.

Sex inference is automatic. Samples whose chrY variant count falls between
`--sex-ambiguous-min` (default 5 000) and `--sex-ambiguous-max` (default 7 000)
are flagged and the user is prompted to enter the sex manually.

The `pgx_runs/Snakefile` must exist with the local patches applied before
running. The `pgx_pilot` Docker image must also be available locally.

Run the full pipeline (prepare workspaces + execute pgx_pilot):

```bash
impact-tools beacon pgx \
  --base-dir /home/mmatitos/beacon_demo \
  --pgx-repo /home/mmatitos/git/beacon2/pgx_pilot
```

Only prepare workspaces and `inputs/samples.tsv` without running the pipeline:

```bash
impact-tools beacon pgx --base-dir /home/mmatitos/beacon_demo --prepare
```

Only run pgx_pilot on already-prepared workspaces:

```bash
impact-tools beacon pgx \
  --base-dir /home/mmatitos/beacon_demo \
  --pgx-repo /home/mmatitos/git/beacon2/pgx_pilot \
  --run 
```

Process samples in parallel (default 4 workers — applies to sex inference, workspace prep and pgx_pilot runs):

```bash
impact-tools beacon pgx \
  --base-dir /home/mmatitos/beacon_demo \
  --pgx-repo /home/mmatitos/git/beacon2/pgx_pilot \
  --workers 8
```

The `--pgx-repo` path can also be set via the `PGX_REPO` environment variable.

### pgx_pilot Outputs

| File | Content |
| --- | --- |
| `inputs/samples.tsv` | Global sample manifest with inferred sex. Created and updated automatically. |
| `pgx_runs/<sample>/config.yaml` | pgx_pilot config for this sample. |
| `pgx_runs/<sample>/data/samples.tsv` | Single-row per-sample metadata (sex, country code). |
| `pgx_runs/<sample>/results/<sample>.sites.all.vcf.gz` | Sites-only VCF, all variants. |
| `pgx_runs/<sample>/results/<sample>.sites.pass.vcf.gz` | Sites-only VCF, PASS QC only (Beacon-ready). |
| `logs/<sample>_pgx.log` | Snakemake stdout/stderr log. |

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

### Parallel Execution

Both `beacon liftover` and `beacon pgx` support `--workers N` to process
multiple samples concurrently using threads. Each worker runs independent Docker
containers, so `--workers` also controls the maximum number of simultaneous
containers. The default is 4. Tune this value to your available CPU, memory, and
Docker daemon capacity.

## Development

Run syntax checks:

```bash
python3 -m py_compile \
  impact_tools/__main__.py \
  impact_tools/beacon/liftover.py \
  impact_tools/beacon/pgx.py \
  impact_tools/ega/encrypt.py \
  impact_tools/ega/upload_inbox.py
```

Inspect CLI help:

```bash
python3 -m impact_tools --help
python3 -m impact_tools beacon liftover --help
python3 -m impact_tools beacon pgx --help
python3 -m impact_tools ega encrypt --help
python3 -m impact_tools ega upload-inbox --help
```

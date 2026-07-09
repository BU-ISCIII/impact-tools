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
│   │   ├── ingest.py            # Dataset and variant ingestion workflows
│   │   ├── liftover.py          # VCF liftover workflow (GRCh37 → GRCh38)
│   │   └── pgx.py               # pgx_pilot workspace preparation and execution
│   └── ega/
│       ├── encrypt.py           # Crypt4GH encryption workflow
│       ├── slurm.py             # HPC/SLURM encryption planning
│       └── upload_inbox.py      # LocalEGA Inbox SFTP upload workflow
├── docs/
│   ├── beacon/                  # Beacon workflow notes
│   └── ega/                     # Affiliated EGA workflow notes
├── impact_tools/conf/
│   └── configuration.json       # Default operational settings
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

### User Configuration

Persistent machine- or user-specific paths can be stored in
`~/.impact_tools/extra_config.json`. This keeps values such as log locations,
Crypt4GH keys and Inbox connection settings out of repeated command lines.
The commented template
[`impact_tools/conf/initial_config.yaml`](impact_tools/conf/initial_config.yaml)
documents every available option.

Create an `impact-config.yaml` file:

```yaml
logs:
  default_outpath: /impact_data/logs/impact-tools
  modules_outpath:
    ega_encrypt: /impact_data/logs/impact-tools/ega/encrypt
    ega_upload_inbox: /impact_data/logs/impact-tools/ega/upload-inbox
    beacon_ingest_dataset: /impact_data/logs/impact-tools/beacon/ingest-dataset
    beacon_ingest_variants: /impact_data/logs/impact-tools/beacon/ingest-variants

ega:
  encryption:
    recipient_pubkey: /secure/localega/service.key.pub
    output_dir: /impact_data/raw_data/lega/encrypted_c4gh
  inbox:
    host: dcontainers00
    port: 2222
    username: user@example.org
    identity_file: ~/.ssh/localega_inbox

beacon:
  execution:
    profile: local

  mongo:
    host: dcontainers00
    port: 27017
    user: root
    password: null
    auth_source: admin
    database: beacon
    direct: true
    tls: false
    tls_ca: null
    tls_cert: null
    tls_allow_invalid: false
    server_timeout_ms: 10000
    connect_timeout_ms: 10000
    socket_timeout_ms: 30000

  api:
    base_url: http://beaconaf-isciiiciber.isciiides.es:8443
    timeout_seconds: 30
    verify_tls: true

```

Install it for the current user:

```bash
impact-tools add-extra-config --config-file impact-config.yaml
```

The priority is: explicit CLI arguments, an execution-specific
`--config-file`, `~/.impact_tools/extra_config.json`, then package defaults.
Do not store passwords or private key contents in this file; store only paths
to protected key files.

## Beacon Workstream

The Beacon tooling covers the operational steps needed to prepare genomic data
for ingestion into the Go-IMPaCT Beacon v2 deployment.

Current focus:

1. Detect the genome build of input VCFs.
2. Lift over variants from GRCh37 to GRCh38 using CrossMap via Docker.
3. Fix contig headers, sort, compress and index the output.
4. Clean obsolete INFO annotations incompatible with downstream ingestion.
5. Infer sample sex from non-ref chrY variant counts.
6. Prepare per-sample pgx_pilot workspaces (symlinks, config, samples.tsv).
7. Run the pgx_pilot Snakemake pipeline via Docker to produce sites-only VCFs.
8. Register Beacon datasets end-to-end: generate dataset artifacts locally,
   import dataset metadata into MongoDB, store dataset flags and permissions in
   MongoDB, register dataset-scoped DUO filtering terms, and verify dataset
   visibility via `/api/datasets`.

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
        |  pgx_runs/<release_id>/      (shared workspace)
        |  <release_id>.sites.pass.vcf.gz  (Beacon-ready, PASS QC, with AF)
        v
impact-tools beacon ingest dataset
        |
        |  datasets.csv                (dataset metadata)
        |  datasets.json               (BFF for MongoDB import)
        |  db.datasets                 (dataset metadata)
        |  db.datasetsConf             (isTest / isSynthetic flags)
        |  db.datasetsPermissions      (access level and granularity)
        |  db.filtering_terms          (dataset-scoped DUO terms)
        v
Beacon dataset registered (visible via /api/datasets)
        |
        v
impact-tools beacon ingest variants
        |
        |  Run RI-tools locally against a staging dataset ID
        |  Verify staging counts in MongoDB
        |  Promote staging variants to active dataset ID
        |  Verify dataset and variant count via Beacon API
        v
Beacon v2 serves variants from dataset (dataset searchable via /api/g_variants)
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
  --base-dir /path/to/beacon
```

Remove intermediate files after a successful run:

```bash
impact-tools beacon liftover \
  --base-dir /path/to/beacon \
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
impact-tools beacon liftover --base-dir /path/to/beacon --workers 8
```

Write logs and metrics to a custom directory (default: `<base-dir>/logs/`):

```bash
impact-tools beacon liftover \
  --base-dir /path/to/beacon \
  -o /path/to/reports
```

Use `--no-report` to skip HTML report generation (metrics JSON is always written).
Use `--force` to skip interactive confirmation prompts.

### Liftover Outputs

Each liftover run writes per sample under `<base-dir>/liftover/`:

| File | Content |
| --- | --- |
| `<sample>.GRCh38.clean.vcf.gz` | Lifted, sorted VCF with obsolete INFO tags removed. |
| `<sample>.GRCh38.clean.vcf.gz.tbi` | Tabix index. |
| `<sample>_liftover.log` | Full stdout/stderr log from bcftools and CrossMap. |

### Prepare and Run the PGx Batch Pipeline

`beacon pgx` runs the pgx_pilot AF/QC pipeline over a cohort of DRAGEN gVCF
files. It performs joint genotyping with GLnexus, validates the resulting
multi-sample VCF, and executes the Snakemake AF/QC and optional PyPGx workflows
inside a container.

#### Configuration

Infrastructure paths live in `~/.impact_tools/extra_config.json` under the
`beacon.pgx` namespace. `ref_fasta`, `output_dir` and `pgx_image` are required;
everything else has defaults.

```json
{
  "beacon": {
    "pgx": {
      "ref_fasta":       "/refs/GRCh38.fa",
      "output_dir":      "/results/pgx",
      "pgx_image":       "/images/pgx_pilot_v1.sif",
      "glnexus_image":   "/images/glnexus_v1.4.1.sif",
      "executor":        "hpc",
      "glnexus_config":  "gatk",
      "snakemake_jobs":  8,
      "pypgx_snakefile": null,
      "slurm": {
        "time_limit": "48:00:00",
        "memory":     "64G",
        "cpus":       16,
        "extra_args": []
      }
    }
  }
}
```

Use `--config-file` to override the persistent config for a single run.

#### Usage

Prepare the workspace and SLURM script, without submitting:

```bash
impact-tools beacon pgx \
  --samples-tsv /data/IMPACT_BATCH_001.samples.tsv \
  --gvcf-dir /data/gvcfs/ \
  --executor hpc \
  --prepare
```

Pass gVCF paths explicitly instead of a directory scan:

```bash
impact-tools beacon pgx \
  --samples-tsv /data/IMPACT_BATCH_001.samples.tsv \
  --gvcf-list /data/batch001.list \
  --prepare
```

Override the output directory for this run only:

```bash
impact-tools beacon pgx \
  --samples-tsv /data/IMPACT_BATCH_001.samples.tsv \
  --gvcf-dir /data/gvcfs/ \
  --output-dir /scratch/pgx_runs \
  --prepare
```

Validate inputs and print the plan without writing anything:

```bash
impact-tools beacon pgx \
  --samples-tsv /data/IMPACT_BATCH_001.samples.tsv \
  --gvcf-dir /data/gvcfs/ \
  --dry-run
```

Exactly one gVCF source is required: `--gvcf-dir` (recursive scan) or
`--gvcf-list` (explicit `sample_id<TAB>path` file).
`--release-id` is auto-derived from the TSV filename when omitted
(e.g. `IMPACT_BATCH_001.samples.tsv` → `IMPACT_BATCH_001`).
`--ref-fasta`, `--output-dir` and `--pgx-image` override their config counterparts
for a single run.
Use `--force` to remove an existing workspace and start fresh.
Use `--pypgx` to enable the optional PyPGx pharmacogenomics sub-workflow.
Use `--no-report` to skip HTML report generation (metrics JSON is always written).
Use `--cleanup` to remove the `results/temp` scratch intermediates after a
successful run (prompts for confirmation unless `--force` is given).

#### What the generated script does

1. **GLnexus joint genotyping** — all gVCFs are merged into a single multi-sample VCF.
2. **Sample validation** — the VCF sample list is diffed against `expected_samples.txt`; the script aborts on mismatch.
3. **Snakemake AF/QC pipeline** — normalisation, masking, allele-frequency calculation and QC tagging, producing a sites-only PASS VCF ready for Beacon ingestion.
4. **PyPGx** (optional, `--pypgx`) — per-sample pharmacogenomic allele, genotype and phenotype calls.
5. **Cleanup** (optional, `--cleanup`) — removes the `results/temp` scratch intermediates. Runs only after the steps above succeed, so failures leave the temp files in place for debugging.

#### pgx_pilot Outputs

| File | Content |
| --- | --- |
| `pgx_runs/<release_id>/config.yaml` | Snakemake config for this release. |
| `pgx_runs/<release_id>/manifests/` | `samples.tsv`, `gvcfs.list`, `expected_samples.txt`, `batch.json`. |
| `pgx_runs/<release_id>/slurm/pgx_<release_id>.sbatch` | Generated SLURM launcher (`.sh` for `--executor local`). |
| `pgx_runs/<release_id>/data/<release_id>.joint.vcf.gz` | GLnexus multi-sample VCF. |
| `pgx_runs/<release_id>/results/<release_id>.sites.pass.vcf.gz` | Sites-only VCF, PASS QC, with population AF. Ready for Beacon ingestion. |
| `pgx_runs/<release_id>/results/<release_id>.sites.all.vcf.gz` | Sites-only VCF, all variants. |
| `pgx_runs/<release_id>/results/intermediate/<release_id>.full_sample_data.vcf.gz` | Per-sample genotypes with QC tags. |
| `pgx_runs/<release_id>/results/pgx/` | PyPGx allele, genotype and phenotype CSVs (when `--pypgx` is used). |
| `pgx_runs/<release_id>/logs/` | SLURM stdout/stderr, metrics JSON and HTML report. |


### Register Beacon Datasets into MongoDB

The `beacon ingest dataset` command registers dataset metadata in the Beacon v2
deployment using direct MongoDB and Beacon API access.

The command:

1. Generates local `datasets.csv` and `datasets.json` artifacts.
2. Imports the dataset document into `db.datasets`.
3. Stores dataset flags in `db.datasetsConf`.
4. Stores dataset permissions in `db.datasetsPermissions`.
5. Injects DUO data-use conditions into `dataUseConditions.duoDataUse` when
   `--duo-code` is provided.
6. Upserts dataset-scoped DUO terms into `db.filtering_terms`.
7. Verifies that the dataset is visible through `/api/datasets`.

It does not require SSH access, remote `podman exec`, remote YAML edits, or an
API container restart.

The command can be run interactively:

```bash
impact-tools beacon ingest dataset
```

The user is prompted for the dataset identifier, display name, optional
description, reference genome build and test/synthetic flags.

The same information can also be provided directly through CLI options:

```bash
impact-tools beacon ingest dataset \
  --base-dir /path/to/beacon_work \
  --dataset-id ISCIII_ES_IMPACT_1 \
  --name "Go-IMPaCT Spain WGS cohort" \
  --description "" \
  --ref-genome GRCh38 \
  --granularity record \
  --is-test y \
  --set-permissions public \
  --duo-code DUO:0000042
```
`--duo-code` is repeatable when more than one DUO term applies.

The command writes dataset-specific working files under `<base-dir>/config/`,
`<base-dir>/work/` and `<base-dir>/inputs/`. It also writes a metrics JSON file
under `<base-dir>/logs/` (or `<output-dir>/logs/` if `-o/--output-dir` is set).
Use `--no-report` to skip HTML report generation.

Use `--dry-run` to validate inputs and build the execution plan without writing
files, metrics, reports or MongoDB records.

Remote Beacon deployment settings are resolved through the standard
impact-tools configuration hierarchy: explicit CLI arguments, an
execution-specific `--config-file`, `~/.impact_tools/extra_config.json`, and
finally the package defaults.

### Ingest Genomic Variants into Beacon

The `beacon ingest variants` command applies genomic variants to an already
registered Beacon dataset using direct MongoDB and Beacon API access.

The target dataset must already exist in the MongoDB `datasets` collection.
When an unknown dataset ID is supplied, the command stops before running
RI-tools.

Exactly one input mode must be selected:

* `--vcf` for one `.vcf` or `.vcf.gz` file.
* `--vcf-dir` for every `.vcf` and `.vcf.gz` file directly contained in one
  directory.

#### Ingest multiple VCF files

```bash
impact-tools beacon ingest \
  --run-profile ws \ # default
  variants \
  --dataset-id ISCIII_ES_IMPACT_1 \
  --vcf-dir /home/user/beacon_work/pgx_ingest \
  --ref-genome GRCh38 # default
```

Input files are selected in deterministic filename order. RI-tools is run once
for every VCF, while processed, inserted and skipped counts are accumulated for
the complete batch.

#### Ingest one VCF file

```bash
impact-tools beacon ingest \
  --run-profile ws \ # default
  variants \
  --dataset-id ISCIII_ES_IMPACT_1 \
  --vcf /home/user/beacon_work/sample.sites.pass.vcf.gz \
  --ref-genome GRCh38
```

#### Safe verify-and-swap workflow

For a normal, non-dry-run execution, the command:

1. Confirms that the active dataset is registered in MongoDB.
2. Creates a unique temporary staging dataset ID.
3. Uploads and processes each selected VCF with Beacon RI-tools.
4. Counts the staging variants in MongoDB.
5. Verifies the count against the total number successfully inserted by
   RI-tools.
6. Renames existing active variants to a timestamped backup dataset ID.
7. Promotes the staging variants to the active dataset ID.
8. Verifies dataset visibility and variant count through the API.
9. Optionally removes the newly created backup with `--cleanup-old`.

Older timestamped backups can also be reviewed and removed interactively after
a successful run.

#### Dry run

```bash
impact-tools beacon ingest \
  --run-profile ws \
  variants \
  --dataset-id ISCIII_ES_IMPACT_1 \
  --vcf-dir /home/user/beacon_work/pgx_ingest \
  --ref-genome GRCh38 \
  --dry-run
```

A variant-ingestion dry run still uploads the selected VCF files and executes
RI-tools using the temporary staging dataset ID. It stops before validating and
promoting the staging data to the active dataset.

Therefore, this mode is intended to validate RI-tools processing and cleanup
without changing the active dataset.

#### Optional controls

```text
--cleanup-old            Delete the backup created during the current swap.
--no-report              Skip HTML report generation (metrics JSON always written).
-o, --output-dir         Write logs and metrics here instead of ./logs/.
--run-profile            Record the execution environment as local, ws or hpc.
```

#### Generated artifacts

Each run writes its audit artifacts under `./logs/` (or `<output-dir>/logs/`):

| File                                            | Content                                                                                             |
| ----------------------------------------------- | --------------------------------------------------------------------------------------------------- |
| `beacon_ingest_variants_manifest_<run_id>.json` | Machine-readable inputs, execution environment, counts, validation results and output paths.        |
| `beacon_ingest_variants_report_<run_id>.html`   | Self-contained execution report with summary cards, validation status, runtime and optional charts. |


When multiple input VCFs contain the same genomic variant, the raw sum of VCF
records can be greater than the number inserted into MongoDB. MongoDB and API
validation therefore use the accumulated RI-tools inserted count.


## Affiliated EGA Workstream

The Affiliated EGA tooling covers the operational steps needed before data can
be managed through the LocalEGA / Federated EGA Affiliate workflow.

Current focus:

1. Encrypt raw sequencing files with Crypt4GH.
2. Generate auditable metrics for encryption runs.
3. Plan HPC/SLURM array jobs for large encryption batches.
4. Upload encrypted `.c4gh` files to the LocalEGA Inbox over SFTP.
5. Generate upload metrics and manifests for traceability.
6. Track successfully encrypted and uploaded content across incoming batches.

### Workflow Overview

```text
Raw sequencing files
        |
        v
impact-tools ega encrypt-upload
        |
        |  .c4gh files
        |  HTML dashboard
        |  manifest JSON
        |  compact metrics TSV
        |  execution logs
        v
LocalEGA Inbox SFTP
        |
        v
LocalEGA / CEGA ingestion, accessioning, release and distribution
```

The standalone `ega encrypt` and `ega upload-inbox` commands remain available
when both stages must be scheduled or operated independently.

### Execution Profiles And End-to-End Runs

Use `--run-profile ws`, `--run-profile local` or `--run-profile hpc` to label
where a run was executed. Every encryption and upload manifest records the
hostname, platform, Python version, CPU model/count, total memory, SLURM
identifiers, stage wall time, CPU time, observed maximum RSS and
coordinator-process I/O. CPU usage includes completed child processes; RSS is
the highest observed Python or child-process lifetime peak, while I/O counters
cover the Python coordinator only.

The `encrypt-upload` wrapper encrypts a batch and then uploads exactly the
outputs from that encryption result. It writes a closed upload input list and a
small source tree of symbolic links, so older `.c4gh` files already present in
the encrypted directory are not included accidentally. Registered encrypted
outputs can be reused without copying their payload and still retain the sample
layout selected for the Inbox upload.

```bash
impact-tools ega encrypt-upload \
  --run-profile ws \
  --input-dir /data/e2e/raw_fastq \
  --encrypted-dir /data/e2e/encrypted_ws \
  --output-dir /data/e2e/reports/ws \
  --recipient-pubkey /secure/localega/service.key.pub \
  --host dcontainers00 \
  --username user@example.org \
  --remote-layout relative \
  --registry-file /secure/impact-tools/ega_registry.sqlite3 \
  --ask-password
```

Run the same command on the HPC with `--run-profile hpc`, HPC-specific paths
and a separate encrypted output directory. The same profile label is
automatically propagated to SLURM encryption tasks.

The comparison command accepts standalone encryption manifests, standalone
Inbox upload manifests and end-to-end wrapper manifests. Compare the same
stage independently when measuring one operation:

```bash
# Encryption-only comparison
impact-tools ega compare-runs \
  /data/e2e/encrypted_ws/encryption_manifest_<run_id>.json \
  /shared/e2e/encrypted_hpc/encryption_manifest_<run_id>.json \
  --output-dir /data/e2e/comparison/encryption

# Upload-only comparison
impact-tools ega compare-runs \
  /data/e2e/uploads_ws/inbox_upload_manifest_<run_id>.json \
  /shared/e2e/uploads_hpc/inbox_upload_manifest_<run_id>.json \
  --output-dir /data/e2e/comparison/upload
```

Compare complete encryption-and-upload workflows:

```bash
impact-tools ega compare-runs \
  /data/e2e/reports/ws/encrypt_upload_manifest_<run_id>.json \
  /shared/e2e/reports/hpc/encrypt_upload_manifest_<run_id>.json \
  --output-dir /data/e2e/comparison/workflow
```

Manifests from different run types can also be supplied together for a single
operational overview. Metrics that do not apply to a run type remain empty
instead of being represented as zero.

Each run produces a self-contained HTML dashboard with summary cards, embedded
charts, execution environment details and per-file results. The comparison
produces one compact TSV and one HTML dashboard containing the available
encryption throughput, upload throughput, stage runtime, end-to-end runtime and
peak memory metrics. JSON manifests remain the complete machine-readable audit
record; no separate PNG directory is needed.

For a fair benchmark, compare the same run type using the same input files and,
for encryption, the same Crypt4GH key. Use separate output directories and no
competing jobs. Disable the processing registry for repeated benchmark
executions, or use separate registry files, so one run is not reported as
already processed. Avoid uploading the same remote filenames twice unless the
Inbox test area has been cleaned or a separate `--remote-dir` is used.

Standalone encryption and upload runs write
`encryption_report_<run_id>.html` and `inbox_upload_report_<run_id>.html`.
Use `--no-plots` on encryption to skip the legacy PNG directory while
keeping charts embedded in the HTML report. Use `--no-charts` when a
text-and-table HTML report is preferred.

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
| `encryption_report_<run_id>.html` | Self-contained visual dashboard with metrics, charts, environment and file results. |
| `encryption_metrics_<run_id>.tsv` | Per-file size, checksum, runtime, throughput, status and error fields. |
| `encryption_summary_<run_id>.txt` | Human-readable batch summary. |
| `encryption_manifest_<run_id>.json` | Machine-readable run manifest. |
| `encryption_<run_id>.log` | Detailed execution log. |
| `plots_<run_id>/` | Optional PNG plots when `matplotlib` is installed. |

### Run Encryption on SLURM

For large batches stored in a shared filesystem such as `/impact_data`, the CLI
can generate a SLURM array job without duplicating the encryption logic.

By default, tasks are grouped by sample directory, so paired WGS files under the
same sample folder are encrypted in the same array task.

```bash
impact-tools ega encrypt-slurm \
  --input-dir /impact_data/raw_data/lega \
  --output-dir /impact_data/raw_data/lega/encrypted_c4gh \
  --recipient-pubkey /path/to/service.key.pub
```

For throughput benchmarks, prefer file-level tasks or small chunks so SLURM can
spread the work across compute nodes while the registry prevents duplicate
processing:

```bash
impact-tools ega encrypt-slurm \
  --input-dir /impact_data/raw_data/lega \
  --output-dir /impact_data/raw_data/lega/encrypted_c4gh \
  --recipient-pubkey /path/to/service.key.pub \
  --registry-file /impact_data/logs/impact-tools/ega/ega_registry.sqlite3 \
  --task-layout file \
  --items-per-task 1 \
  --plan-dir /impact_data/logs/impact-tools/ega/slurm-plans
```

SLURM defaults such as partition, CPUs, memory and time limit are read from
`impact_tools/conf/configuration.json`. Use CLI options only when a particular
run needs to override those defaults.

If `crypt4gh` is not available in the compute-node `PATH`, add:

```bash
--crypt4gh-bin /path/to/env/bin/crypt4gh
```

The generated execution bundle includes:

| File | Content |
| --- | --- |
| `chunks/task_<N>.txt` | Input file list consumed by one SLURM array task. |
| `encryption_slurm_tasks_<run_id>.tsv` | Task-level summary with sample, chunk and size information. |
| `encryption_slurm_files_<run_id>.tsv` | File-level mapping to each task. |
| `encryption_slurm_chunks_<run_id>.txt` | Ordered chunk index used by the array script. |
| `encrypt_localega_array_<run_id>.sbatch` | Reproducible SLURM array script. |
| `report_localega_array_<run_id>.sbatch` | Dependent job that aggregates all task manifests. |
| `aggregate_report/encryption_array_report_<job_id>.html` | Single visual report for the complete logical array run. |
| `aggregate_report/encryption_array_manifest_<job_id>.json` | Machine-readable aggregate manifest for comparisons. |
| `_run_encrypt_localega_array_<run_id>.sh` | Wrapper that submits the array and its `afterany` aggregate report job. |

Submit the generated array and automatic aggregate report job with:

```bash
bash /path/to/plan/_run_encrypt_localega_array_<run_id>.sh
```

The wrapper submits the report with an `afterany` dependency, so one logical
SLURM run produces one aggregate HTML/TSV/JSON report even when individual
tasks fail. Per-task reports remain available for diagnosis. Add `--submit` to
submit both jobs immediately from the CLI.

Runs split across more than one SLURM submission can be aggregated manually by
repeating `--array-job-id`:

```bash
impact-tools ega aggregate-encryption-array \
  --manifest-dir /path/to/encrypted \
  --output-dir /path/to/aggregate-report \
  --array-job-id 12345 \
  --array-job-id 12346 \
  --expected-tasks 6
```

Use `--task-layout file` if each file should become its own schedulable unit.
Use `--setup-command` only when the generated job needs extra environment setup
lines, for example loading a module on a specific HPC environment.

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

### Prevent Duplicate Batch Processing

Successful encryptions and uploads are recorded by default in:

```text
~/.impact_tools/ega_registry.sqlite3
```

The registry identifies raw and encrypted files by their SHA-256 content hashes
rather than their filenames or directories. Therefore, content already
encrypted or uploaded in a previous batch is skipped even if it later appears
under another sample folder or with another filename. R1 and R2 are tracked
independently, so partial samples remain visible instead of being marked as a
single completed unit.

Only completed encryptions and SFTP uploads are recorded. Failed operations and
dry runs never modify the registry. An existing encrypted output that does not
match the registered input content is treated as a conflict and requires
`--force` or another output directory. Existing remote files that were not
uploaded by a registered run are reported as `skipped_existing`, but are not
trusted and added automatically.

Concurrent processes reserve each content hash atomically before encrypting or
uploading it. Other workers report `skipped_in_progress` instead of processing
the same content simultaneously. Reservations are released after success or
failure, and abandoned reservations expire after 24 hours.

Inspect recent successful encryption and upload events:

```bash
impact-tools ega processing-history --limit 50
impact-tools ega processing-history --stage uploaded
```

Use another persistent registry:

```bash
impact-tools ega upload-inbox \
  --registry-file /secure/impact-tools/ega_registry.sqlite3 \
  ...
```

Use `--force` for intentional reprocessing, or `--no-registry` to disable
content tracking for one execution. With the registry enabled, SHA-256 is
still calculated internally when `--no-checksums` is selected because the hash
is required to identify duplicate content.

The registry confirms local encryption and completed SFTP transfer. It does not
replace CEGA/LocalEGA accession, ingestion or dataset-release status.

For SLURM arrays, place the registry on persistent storage with reliable POSIX
file locking that is visible from every compute node. The registry uses a
60 second busy timeout, the SQLite rollback journal compatible with shared
filesystems and short writes so separate array tasks can reserve and complete
files without processing the same content twice.
If the shared filesystem does not support SQLite locking correctly, use
`--no-registry` for the array and perform duplicate control before generating
the task plan.

### Upload Outputs

Each upload run writes:

| File | Content |
| --- | --- |
| `inbox_upload_report_<run_id>.html` | Self-contained visual dashboard with metrics, charts, environment and file results. |
| `inbox_upload_metrics_<run_id>.tsv` | Per-file upload status, size, timing, throughput and checksum. |
| `inbox_upload_summary_<run_id>.txt` | Human-readable batch summary. |
| `inbox_upload_manifest_<run_id>.json` | Machine-readable upload manifest. |
| `inbox_upload_<run_id>.log` | Detailed execution log. |

Per-file statuses distinguish `ok`, `skipped_registered`,
`skipped_in_progress`, `skipped_duplicate_batch`, `skipped_existing` and
`failed`.

## Operational Notes

### Crypt4GH

`impact-tools ega encrypt` needs a working `crypt4gh` executable. If multiple
Python or micromamba environments are available, pass the exact binary with
`--crypt4gh-bin` to avoid using a broken system installation.

### LocalEGA Scope

The upload command transfers encrypted files to the Inbox. The subsequent
LocalEGA ingestion, accessioning, dataset mapping, release, DAC permission
propagation and distribution steps are handled by the LocalEGA / CEGA workflow.

### Parallel execution for Beacon liftover

`beacon liftover` supports `--workers N` to process multiple samples concurrently
using threads. Each worker runs an independent Docker container, so `--workers`
also controls the maximum number of simultaneous containers. The default is 4.
Tune this value to your available CPU, memory, and Docker daemon capacity.

## Development

Run syntax checks:

```bash
python3 -m py_compile \
  impact_tools/__main__.py \
  impact_tools/beacon/liftover.py \
  impact_tools/beacon/pgx.py \
  impact_tools/beacon/mongo.py \
  impact_tools/beacon/ingest.py \
  impact_tools/beacon/ritools.py \
  impact_tools/beacon/html_report.py \
  impact_tools/ega/encrypt.py \
  impact_tools/ega/slurm.py \
  impact_tools/ega/upload_inbox.py
```

Inspect CLI help:

```bash
python3 -m impact_tools --help
python3 -m impact_tools beacon liftover --help
python3 -m impact_tools beacon pgx --help
python3 -m impact_tools beacon ingest --help
python3 -m impact_tools beacon ingest dataset --help
python3 -m impact_tools beacon ingest variants --help
python3 -m impact_tools ega encrypt --help
python3 -m impact_tools ega encrypt-slurm --help
python3 -m impact_tools ega upload-inbox --help
```

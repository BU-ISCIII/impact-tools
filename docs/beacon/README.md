# Beacon tools

Documentation for Beacon preprocessing, ingestion, authentication and query workflows.

## Architecture overview

```mermaid
flowchart LR

CLI["Beacon CLI"]

INGEST["Dataset and variant ingestion"]
DATASET["Register dataset in MongoDB"]
VARIANTS["Ingest variants into a dataset"]
LIFTOVER["VCF liftover"]
PGX["PGx analysis"]

SERVICES["Shared services Remote, MongoDB, RI-tools, Registry"]
RESOURCES["Resources: Snakefile and templates"]
REPORTS["Reports and validation"]

CLI --> |"impact-tools beacon ingest"| INGEST
CLI --> |"impact-tools beacon liftover"| LIFTOVER
CLI --> |"impact-tools beacon pgx"| PGX

INGEST --> |"dataset"| DATASET
INGEST --> |"variants"| VARIANTS

DATASET --> SERVICES
VARIANTS --> SERVICES

LIFTOVER --> RESOURCES
PGX --> RESOURCES

LIFTOVER --> |"GRCh38 clean VCFs"| PGX

DATASET --> REPORTS
VARIANTS --> REPORTS
LIFTOVER --> REPORTS
PGX --> REPORTS
```

---

## Prerequisites

### Python dependencies

`pymongo` and `httpx` are required and declared in `pyproject.toml`. They are installed automatically with the package:

```bash
pip install -e .
```

### External tools

- **beacon2-ri-tools-v2** — installed as a Git dependency (see `pyproject.toml`). The `genomicVariations_vcf` module must be importable from the environment where impact-tools runs.
- **paramiko** — required for the SSH operations that still remain (filtering terms extraction and API restart).

### Environment variables

| Variable | Required | Description |
|---|---|---|
| `IMPACT_TOOLS_BEACON_MONGO_PASSWORD` | Yes | MongoDB password. Takes precedence over `beacon.mongo.password` in the config file. |
| `IMPACT_TOOLS_BEACON_REGISTRY` | No | Override path for the local SQLite registry (default: `~/.impact_tools/beacon_registry.sqlite3`). |

### Configuration

Beacon configuration lives under the `beacon` key in `impact_tools/conf/configuration.json` (or the user-level override at `~/.config/impact-tools/config.yaml`). The relevant sections are:

```json
{
  "beacon": {
    "remote": {
      "host": "<VM IP>",
      "user": "bioinfo",
      "port": 22,
      "identity_file": null,
      "beacon_dir": "/opt/beacon/beacon2-pi-api-isciii",
      "datasets_conf_dir": "/opt/beacon/beacon2-pi-api-isciii/beacon/conf/datasets",
      "datasets_permissions_dir": "/opt/beacon/beacon2-pi-api-isciii/beacon/permissions/datasets"
    },
    "containers": {
      "api": "beaconprod"
    },
    "mongo": {
      "host": "<VM IP>",
      "port": 8085,
      "user": "root",
      "password": null,
      "auth_source": "admin",
      "database": "beacon",
      "direct": true,
      "tls": false
    },
    "api": {
      "base_url": "http://<beacon-api-host>:<port>",
      "timeout_seconds": 30,
      "verify_tls": true
    }
  }
}
```

The MongoDB password must **not** be stored in the config file. Set it through the environment variable instead:

```bash
export IMPACT_TOOLS_BEACON_MONGO_PASSWORD=<password>
```

---

## Connectivity model

impact-tools uses three independent channels to communicate with the Beacon deployment:

| Channel | Protocol | Used for |
|---|---|---|
| **PyMongo** | TCP to MongoDB port | Dataset import, variant staging/swap, reindex, old backup cleanup |
| **Beacon HTTP API** | HTTP/HTTPS | Post-ingest verification (dataset visibility, variant count) |
| **SSH** | paramiko | YAML config file updates, filtering terms extraction (`podman exec`), API restart |

```mermaid
flowchart LR

subgraph local["Local machine"]
    IT["impact-tools CLI"]
    RITOOLS["RI-tools\ngenomic_variations_vcf"]
    IT -- "subprocess" --> RITOOLS
end

subgraph vm["Beacon VM"]
    subgraph podman["Podman containers"]
        MONGO["mongoprod\nMongoDB"]
        API["beaconprod\nBeacon API"]
    end
    YAML["VM filesystem\ndatasets_conf.yml\ndatasets_permissions.yml"]
    API -. "reads on startup" .-> YAML
end

IT -- "PyMongo · :8085\ndataset import · reindex · swap · cleanup" --> MONGO
RITOOLS -- "PyMongo · :8085\nVCF variant insertion" --> MONGO
IT -- "HTTP · :8443\ndataset and count verification" --> API
IT -- "SSH · :22 · SFTP\nupdate YAML config" --> YAML
IT -- "SSH · :22\npodman exec · filtering terms" --> API
IT -- "SSH · :22\npodman-compose · restart" --> API
```

### What uses PyMongo directly

All data operations bypass SSH and talk to MongoDB directly:

- Importing `datasets.json` into the `datasets` collection (`mongo_import_datasets`)
- Counting and verifying dataset documents (`mongo_count_dataset`)
- Running `genomicVariations_vcf` locally with the MongoDB URI injected via `DATABASE_URI` environment variable (`run_genomic_variations_vcf`)
- Counting, renaming and deleting variant documents across staging, active and backup dataset IDs
- Rebuilding MongoDB indexes and auxiliary collections (`mongo_reindex`) — mirrors `beacon.connections.mongo.reindex` but runs locally without container exec

### What still uses SSH

Three operations require SSH because they depend on the VM filesystem or container runtime:

1. **YAML config file updates** — `datasets_conf.yml` and `datasets_permissions.yml` live on the VM filesystem. impact-tools reads the current file over SFTP, merges the new dataset block, writes a timestamped backup, and writes the updated file back.
2. **Filtering terms extraction** — runs `podman exec beaconprod python -m beacon.connections.mongo.extract_filtering_terms` inside the Beacon API container. This step is **non-fatal**: if it fails the workflow logs a warning and continues to the API restart.
3. **API restart** — runs `podman-compose restart beaconprod` in the beacon deployment directory on the VM so the API reloads updated YAML configuration.

---

## Dataset ingestion

Registers a new dataset in the Beacon deployment. Must be run before variant ingestion.

```bash
impact-tools beacon ingest dataset \
  --dataset-id MY_DATASET \
  --name "My dataset" \
  --description "Description" \
  --ref-genome GRCh38 \
  --granularity record \
  --base-dir /path/to/beacon/workdir
```

All parameters are interactive if omitted.

### What it does (step by step)

1. **Validates** `dataset_id` format, reference genome and granularity.
2. **Creates the local directory tree** under `<base-dir>`:
   ```
   config/<dataset_id>/datasets.csv
   config/<dataset_id>/datasets.json   ← generated by csv_to_bff
   config/datasets_conf.yml            ← candidate YAML block
   config/datasets_permissions.yml     ← candidate YAML block
   work/<dataset_id>/
   inputs/<dataset_id>/
   ```
3. **Generates `datasets.json`** by replicating the `csv_to_bff.py` hashing logic from beacon2-ri-tools-v2 (`_id = sha256(dataset_id + id)`).
4. **Imports `datasets.json` into MongoDB** via PyMongo (`$setOnInsert` upsert — existing documents are never overwritten).
5. **Verifies** that the document is present in the `datasets` collection.
6. **Updates the YAML files on the VM** via SSH/SFTP — writes a backup before modifying.
7. **Restarts the Beacon API** via SSH so it reloads the updated YAML.
8. **Verifies dataset visibility** via `GET /api/datasets`.
9. **Records the registration** in the local SQLite registry.
10. **Writes a JSON metrics file and an HTML report** under `<base-dir>/logs/`.

### Dry run

```bash
impact-tools beacon ingest dataset --dataset-id MY_DATASET --dry-run
```

Generates local artifacts (directories, CSV, JSON, YAML blocks) without touching MongoDB, the VM or the API.

---

## Variant ingestion

Ingests genomic variants from one or more VCF files into an existing dataset using a safe stage→verify→swap workflow.

```bash
# Single VCF
impact-tools beacon ingest variants \
  --dataset-id MY_DATASET \
  --vcf path/to/variants.vcf.gz \
  --ref-genome GRCh38

# Directory of VCFs (all .vcf and .vcf.gz files)
impact-tools beacon ingest variants \
  --dataset-id MY_DATASET \
  --vcf-dir path/to/vcf_dir/ \
  --ref-genome GRCh38
```

The dataset must already be registered (`ingest dataset` must have run first).

### Stage → verify → swap workflow (step by step)

1. **Resolve VCF inputs** — validates file existence and extension (`.vcf` / `.vcf.gz`). Counts variants in each file (lines not starting with `#`).
2. **Generate run IDs**:
   - `staging_id = <dataset_id>_staging_<YYYYMMDD_HHMMSS>`
   - `old_id     = <dataset_id>_old_<YYYYMMDD_HHMMSS>`
3. **Validate dataset registration** — queries MongoDB to confirm `dataset_id` exists in the `datasets` collection. Fails with a list of available datasets if not found.
4. **Run RI-tools locally** (`genomicVariations_vcf`) for each VCF against `staging_id`. The MongoDB URI is injected via `DATABASE_URI`. Variants land in a staging collection, not the live one.
5. **Dry-run exit point** — if `--dry-run`, stop here. Artifacts are written to `<base-dir>/logs/` for inspection.
6. **Count staging variants** via PyMongo and verify the count matches the sum of RI-tools-reported insertions.
7. **Atomic dataset ID swap** via PyMongo `update_many`:
   - Renames `<dataset_id>` → `old_id` (backs up the currently active variants)
   - Renames `staging_id` → `<dataset_id>` (promotes staging to active)
8. **Reindex** via PyMongo — rebuilds all MongoDB indexes on `genomicVariations` and `caseLevelData`, and ensures auxiliary collections (`counts`, `synonyms`, `targets`, `similarities`) exist.
9. **Filtering terms extraction** via SSH (`podman exec beaconprod python -m beacon.connections.mongo.extract_filtering_terms`). Non-fatal: failure logs a warning and continues.
10. **API restart** via SSH (`podman-compose restart beaconprod`). Waits 15 seconds for the service to come back.
11. **API verification** — `GET /api/datasets` to confirm visibility and `GET /api/g_variants?datasets=<id>&requestedGranularity=count` to confirm the variant count matches.
12. **Old backup cleanup** (optional) — if `--cleanup-old` is passed, deletes `old_id` variants from MongoDB immediately. Otherwise, offers an interactive prompt after the ingest to delete backups older than 7 days.
13. **Writes a JSON manifest and an HTML report** under `<base-dir>/logs/`.

### Key options

| Option | Description |
|---|---|
| `--dry-run` | Run RI-tools against the staging dataset and stop before the swap. Useful for validating VCF ingestion without affecting the live deployment. |
| `--cleanup-old` | Delete the `_old_` backup immediately after a successful swap. Off by default to allow rollback. |
| `--skip-filtering-terms` | Skip filtering terms extraction. Useful when ingesting multiple datasets in batch — run it once manually at the end. |
| `--no-report-charts` | Generate the HTML report without performance charts. |
| `--run-profile` | Label recorded in metrics (`local`, `ws`, `hpc`). Does not affect execution. |

### Old variant backups

Every successful swap creates a `<dataset_id>_old_<run_id>` backup in MongoDB. These accumulate over time. impact-tools offers an interactive cleanup prompt at the end of each non-dry-run ingestion:

```
Old variant backups found for MY_DATASET:

[1] MY_DATASET_old_20260601_120000
    age: 21 days
    variants: 18863
    default action: delete

Delete backups older than 7 days? [Y/n]:
```

Backups are skipped (not prompted) when stdin is not a TTY (e.g. CI pipelines).

---

## Local SQLite registry

Every dataset registration and variant ingestion is recorded in a local SQLite database at `~/.impact_tools/beacon_registry.sqlite3` (overridable via `IMPACT_TOOLS_BEACON_REGISTRY`).

| Table | Content |
|---|---|
| `dataset_registrations` | One row per dataset: ID, name, genome build, granularity, flags, `base_dir`, timestamp |
| `variant_ingestions` | One row per VCF (keyed by SHA-256): status (`RUNNING`/`OK`/`FAILED`), staging ID, old ID, variant count, timestamps |
| `processing_claims` | Advisory locks for concurrent execution (auto-expire after 12 hours) |

---

## Artifacts written per run

### Dataset ingest

```
<base-dir>/
  config/
    <dataset_id>/
      datasets.csv        ← input to csv_to_bff
      datasets.json       ← imported into MongoDB
    datasets_conf.yml     ← YAML block applied to VM
    datasets_permissions.yml
  work/<dataset_id>/
  inputs/<dataset_id>/
  logs/
    beacon_ingest_dataset_<id>_<timestamp>.metrics.json
    beacon_ingest_dataset_<id>_<timestamp>.report.html
```

### Variant ingest

```
<base-dir>/
  logs/
    beacon_ingest_variants_manifest_<staging_id>.json
    beacon_ingest_variants_report_<staging_id>.html
```

---

## Liftover and PGx

These commands are independent of the ingestion workflow.

```bash
impact-tools beacon liftover ...
impact-tools beacon pgx ...
```

Liftover produces GRCh38-normalised VCF files that can be piped directly into `ingest variants`. PGx analysis operates on the liftover output. Both commands write HTML reports and use local Snakemake resources.

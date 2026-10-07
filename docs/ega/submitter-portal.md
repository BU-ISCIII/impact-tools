# Submitter Portal submission

[EGA workflow map](README.md) · [Encryption and upload](encryption-and-upload.md)

This guide continues the CRAM + VCF batch from the previous guide. Run these
commands on a machine with `impact-tools` and HTTPS access to the **same CEGA
environment** that received the `.c4gh` files. TEST and production are not
interchangeable. Only authorised users should submit real metadata.

## 1. Prepare metadata

Use the exact sample list from encryption. Make a private copy of the
appropriate provider profile and fill in validated values. The profiles in
`impact_tools/conf/ega/submission_profiles/` are templates, not metadata for
a specific study. Supplementary provider metadata can be supplied as CSV,
TSV, or JSON; CSV/TSV rows are matched by `sample_id` or `alias`.

```bash
INPUT_DIR=/data/delivery
RUN_DIR=/data/ega-runs/cohort-01
SAMPLE_LIST="$RUN_DIR/samples.txt"
PROFILE=/secure/ega/cohort-01-profile.yaml
METADATA=/secure/ega/cohort-01-metadata.tsv
DRAFT_DIR="$RUN_DIR/submission_draft"

impact-tools ega prepare-submission \
  --input-dir "$INPUT_DIR" \
  --sample-list "$SAMPLE_LIST" \
  --metadata-file "$METADATA" \
  --profile-file "$PROFILE" \
  --output-dir "$DRAFT_DIR"
```

If no supplementary metadata file is available, omit `--metadata-file`.
Review `evidence.md`, `missing_fields.yaml`, `file_inventory.tsv`, and
`draft_submission.yaml`:

1. Fill in missing fields and check the source of each value in `evidence.md`.
   **Do not** use `--include-examples` for real data.
2. Review title and description, pseudonymised subject, samples, instrument
   model, analysis type, genome, chromosomes, Dataset types, and authorised
   policy. IDs and enum values depend on the target API; they cannot be
   inferred from filenames.
3. Each sample should have a CRAM Run and a VCF Analysis, and the Dataset
   should link them all. Generated draft `files` names use source paths such as
   `SAMPLE_001/SAMPLE_001.cram` and `SAMPLE_001/SAMPLE_001.vcf.gz`, without
   `.c4gh`. `submit-submission` adds that suffix for the Inbox lookup. Include
   the sample directory when `--remote-layout relative` was used.
4. Ensure no `EXAMPLE:` or other fictitious values remain in the draft.

## 2. Generate a plan without submitting

```bash
impact-tools ega submit-submission \
  --draft-file "$DRAFT_DIR/draft_submission.yaml" \
  --output-dir "$RUN_DIR/submission_plan"
```

This writes `submission_plan.json` and JSON files under `payloads/` **without
calling the API**. Check the sequence: Submission → Study → Samples →
Experiments → file resolution → Runs → Analyses → Dataset. A plan does not
prove that files exist in CEGA or that the server will accept the metadata.

## 3. Verify the files in CEGA

Before the first `--execute`, query **each exact Inbox path**. This is a
read-only example. Obtain a token using the authorised procedure for the
target environment and protect its file with restrictive permissions.

```bash
API_BASE='<submitter-portal-api-base>'
TOKEN_FILE=/secure/ega/access_token
REMOTE_FILE=/SAMPLE_001/SAMPLE_001.cram.c4gh

curl --fail-with-body --get --silent --show-error \
  --write-out '\nHTTP %{http_code}\n' \
  --config <(printf 'header = "Authorization: Bearer %s"\n' "$(cat "$TOKEN_FILE")") \
  --data-urlencode 'status=inbox' \
  --data-urlencode "prefix=$REMOTE_FILE" \
  "$API_BASE/files"
```

Check for HTTP 200 and **one exact path match** with `inbox` status, size,
and encrypted checksum matching the upload manifest. An empty list is not
success: check the leading slash in `prefix` and whether the token has expired.
Repeat for every sample's CRAM and VCF. Do not share the token or paste
sensitive responses into public issues.

This check matters because `submit-submission` resolves files **after**
creating the first metadata objects. If file resolution fails, the submission
may be left partially created.

## 4. Submit and preserve state

Only after reviewing the draft, plan, and remote files:

```bash
impact-tools ega submit-submission \
  --draft-file "$DRAFT_DIR/draft_submission.yaml" \
  --output-dir "$RUN_DIR/submission_execute" \
  --api-base "$API_BASE" \
  --token-file "$TOKEN_FILE" \
  --execute
```

Keep `submission_state.json`, `submission_plan.json`, `payloads/`, and
`responses/` in a private, persistent directory. In the portal, verify the
provisional IDs, Runs, Analyses, and Dataset, which should remain **open**.
Do not repeat execution in a new directory as if this were a new batch:
that could create duplicate objects.

If execution fails, inspect the state and the portal first. Keep the draft
**unchanged** (its hash is checked when resuming), then use:

```bash
impact-tools ega submit-submission \
  --draft-file "$DRAFT_DIR/draft_submission.yaml" \
  --output-dir "$RUN_DIR/submission_resume" \
  --api-base "$API_BASE" \
  --token-file "$TOKEN_FILE" \
  --resume-state-file "$RUN_DIR/submission_execute/submission_state.json" \
  --execute
```

If the connection dropped immediately after a `POST`, check in the portal
whether the object was created before retrying: a timeout does not guarantee
the operation failed on the server. Do not edit state IDs to force a retry.

## Manual boundary and FASTQ variant

The tool **does not finalise or release** the Dataset. An authorised person
manages finalisation and the release date manually in the portal. Ingestion,
accessions, permissions, and Distribution downloads must be checked later;
an open Dataset does not prove that a file is in the Vault or downloadable.

`prepare-submission --sample-list` generates CRAM Runs and VCF Analyses. For
a FASTQ test, `encrypt-upload` can encrypt and upload files selected with an
appropriate pattern, but the metadata draft must be prepared **separately**
with a `fastq` Run. Do not simply relabel a CRAM + VCF draft: its format,
metadata, and file links must correspond to the uploaded FASTQ.

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

## 3. Obtain or refresh an API token

The SFTP upload in the previous guide uses an interactive password, not this
API token. Obtain a token before checking files or running
`submit-submission --execute`, and refresh it if the API returns HTTP 401.
Use the IdP token URL and OAuth client ID approved for the **same environment**
as the Inbox and Submitter Portal API. The API base URL is not the browser URL
of the Submitter Portal.

Run this example with Bash after replacing the three values in angle brackets.
It prompts for the password without echoing it or passing it in a `curl`
argument. It writes only `access_token` to a private file, replacing an old
token only after a successful response. Do not commit or share the file.

```bash
bash <<'BASH'
set -euo pipefail

EGA_TOKEN_URL='<approved-idp-token-url>'
EGA_CLIENT_ID='<approved-oauth-client-id>'
EGA_USER='<ega-account-email>'
TOKEN_DIR="$HOME/.impact_tools"
TOKEN_FILE="$TOKEN_DIR/ega_access_token"

umask 077
mkdir -p "$TOKEN_DIR"
chmod 700 "$TOKEN_DIR"
TOKEN_TMP=$(mktemp "$TOKEN_DIR/.ega_access_token.XXXXXX")
trap 'rm -f "$TOKEN_TMP"' EXIT

read -r -s -p 'EGA password: ' EGA_PASSWORD </dev/tty
printf '\n'
printf '%s' "$EGA_PASSWORD" |
  curl --fail-with-body --silent --show-error \
    --connect-timeout 10 --max-time 60 \
    --data-urlencode 'password@-' \
    --data-urlencode "username=$EGA_USER" \
    --data 'grant_type=password' \
    --data-urlencode "client_id=$EGA_CLIENT_ID" \
    "$EGA_TOKEN_URL" |
  python3 -c 'import json, sys; print(json.load(sys.stdin)["access_token"], end="")' \
  > "$TOKEN_TMP"
unset EGA_PASSWORD

test -s "$TOKEN_TMP"
chmod 600 "$TOKEN_TMP"
mv -f "$TOKEN_TMP" "$TOKEN_FILE"
trap - EXIT
printf 'Token saved: %s\n' "$TOKEN_FILE"
BASH
```

If authentication fails, confirm the account, environment, client ID, and
credentials with your EGA administrator. Do not paste token responses or
passwords into support requests. The following `curl` example expects the
token file to contain **only the token string**, not the entire JSON response.

## 4. Verify the files in CEGA

Before the first `--execute`, query **each exact Inbox path**. This is a
read-only example. Use a token for the same CEGA environment as the files.

```bash
API_BASE='<submitter-portal-api-base>'
TOKEN_FILE="$HOME/.impact_tools/ega_access_token"
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

## 5. Submit and preserve state

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

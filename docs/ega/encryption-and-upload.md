# Encryption and Inbox upload

[EGA workflow map](README.md) · [Continue to submission](submitter-portal.md)

This walkthrough uses a CRAM + VCF batch. Run the commands where the original
files reside (a workstation or HPC environment) with SFTP access to the Inbox,
**not** inside a LocalEGA container.

## 1. Prepare the batch

You need `impact-tools`, a working `crypt4gh` executable, an Inbox account,
and the **current Crypt4GH public key of the receiving service**. Confirm its
fingerprint with the node administrator: an outdated key may still let you
encrypt and upload files but prevent their subsequent ingestion.

Example input layout:

```text
/data/delivery/
├── SAMPLE_001/
│   ├── SAMPLE_001.cram
│   └── SAMPLE_001.vcf.gz
└── SAMPLE_002/
    ├── SAMPLE_002.cram
    └── SAMPLE_002.vcf.gz
```

Create a selection file with one sample ID per line:

```bash
mkdir -p /data/ega-runs/cohort-01
printf '%s\n' SAMPLE_001 SAMPLE_002 > /data/ega-runs/cohort-01/samples.txt
```

With `--sample-list`, the tool requires exactly one CRAM and one VCF per
sample. It stops before processing if files are missing or empty, or if
multiple candidates match. Use **the same list** when preparing metadata.

Check that source files are readable and that the output filesystem has room
for another copy of the batch plus reports. For large CRAM files, avoid
`/tmp` and use persistent storage. Do not put patient information in shared
filenames or examples.

## 2. Encrypt and upload only the selected files

Replace the example paths and connection settings. The LocalEGA private key
is **not** used on this machine.

```bash
INPUT_DIR=/data/delivery
RUN_DIR=/data/ega-runs/cohort-01
SAMPLE_LIST="$RUN_DIR/samples.txt"
RECIPIENT_PUBKEY=/secure/localega/service.key.pub
INBOX_HOST='<inbox-host>'
INBOX_PORT=2222
EGA_USER='<ega-user>'
REGISTRY="$RUN_DIR/ega_registry.sqlite3"

impact-tools ega encrypt-upload \
  --input-dir "$INPUT_DIR" \
  --sample-list "$SAMPLE_LIST" \
  --encrypted-dir "$RUN_DIR/encrypted_c4gh" \
  --output-dir "$RUN_DIR/workflow_reports" \
  --recipient-pubkey "$RECIPIENT_PUBKEY" \
  --crypt4gh-bin "$(command -v crypt4gh)" \
  --host "$INBOX_HOST" \
  --port "$INBOX_PORT" \
  --username "$EGA_USER" \
  --remote-dir / \
  --remote-layout relative \
  --registry-file "$REGISTRY" \
  --ask-password
```

The password is requested interactively: do not put it in arguments or
version-controlled files. Verify the Inbox SSH host-key fingerprint before
trusting it. To reject unknown host keys, set `ega.inbox.host_key_policy` to
`reject` in your user configuration; `encrypt-upload` reads that setting.
If the fingerprint changes unexpectedly, stop and confirm it with the
administrator.

`--remote-layout relative` preserves the sample directory. The expected
remote CRAM path is `/SAMPLE_001/SAMPLE_001.cram.c4gh`; it must match the
submission draft. The combined command uploads only outputs selected for
this batch, not older `.c4gh` files in the output directory.

## 3. Review the results

Keep `workflow_reports/`, its manifests, and the SQLite registry on persistent
storage. Review the HTML report and upload manifest:

- Expect two entries per sample (CRAM and VCF), with no `failed` status.
- Compare remote path, size, and encrypted SHA-256 for each `.c4gh` file.
- `skipped_existing` only means a remote file already exists; it **does not**
  prove that the file is correct. Check that `skipped_registered` refers to
  previously registered content at the same destination.

An SFTP transfer is not ingestion. Before submitting metadata, check in the
portal or its API that each file is in `inbox` status, with the exact path
(including its leading slash) and matching checksum. The local registry
records encryption and upload operations, not CEGA accessions or release.

## 4. Encrypt and upload on different machines

If the source files and the SFTP client are on different machines, run only
encryption on the source machine. Use the same sample list and recipient key
checks described above:

```bash
INPUT_DIR=/data/delivery
RUN_DIR=/data/ega-runs/cohort-01

impact-tools ega encrypt \
  --input-dir "$INPUT_DIR" \
  --sample-list "$RUN_DIR/samples.txt" \
  --output-dir "$RUN_DIR/encrypted_c4gh" \
  --recipient-pubkey /secure/localega/service.key.pub \
  --crypt4gh-bin "$(command -v crypt4gh)" \
  --registry-file "$RUN_DIR/encryption_registry.sqlite3"
```

Review the encryption manifest, then transfer the selected `.c4gh` files to
the upload machine using an approved secure method. Preserve their relative
`SAMPLE_001/...` paths. Do not transfer a private Crypt4GH key. On the upload
machine, create an explicit list of the encrypted files for this batch:

```bash
RUN_DIR=/data/ega-runs/cohort-01
ENCRYPTED_DIR="$RUN_DIR/encrypted_c4gh"
UPLOAD_LIST="$RUN_DIR/upload_inputs.txt"
INBOX_HOST='<inbox-host>'
INBOX_PORT=2222
EGA_USER='<ega-user>'

printf '%s\n' \
  'SAMPLE_001/SAMPLE_001.cram.c4gh' \
  'SAMPLE_001/SAMPLE_001.vcf.gz.c4gh' \
  'SAMPLE_002/SAMPLE_002.cram.c4gh' \
  'SAMPLE_002/SAMPLE_002.vcf.gz.c4gh' > "$UPLOAD_LIST"

impact-tools ega upload-inbox \
  --input-dir "$ENCRYPTED_DIR" \
  --input-list "$UPLOAD_LIST" \
  --output-dir "$RUN_DIR/upload_reports" \
  --host "$INBOX_HOST" \
  --port "$INBOX_PORT" \
  --username "$EGA_USER" \
  --remote-dir / \
  --remote-layout relative \
  --registry-file "$RUN_DIR/upload_registry.sqlite3" \
  --ask-password
```

Replace the listed filenames with those in the encryption manifest and
confirm each file exists under `ENCRYPTED_DIR` before uploading. The upload
machine must reach the Inbox for the same CEGA environment as the recipient
key. Verify its SSH host-key fingerprint as in section 2. Never scan a shared
directory containing previous batches without `--input-list`.

## 5. Recover without duplicating work

| Stage and symptom | Check first | Safe next step |
| --- | --- | --- |
| Sample selection or encryption fails | Names, one CRAM and VCF per ID, `crypt4gh`, recipient key, free space, and encryption manifest | Fix the input or environment, then retry with the same output directory and registry; do not force re-encryption of verified files. |
| SFTP authentication or connection fails | Account status, password, network, and verified SSH host key | Correct the connection and retry only the selected batch. |
| Upload stops or reports `skipped_existing` | Compare each exact remote path, size, and encrypted checksum with the manifest through the CEGA API | Retry missing files; stop and investigate any mismatch. An existing path is not proof of a complete upload. |
| File is absent from `/files` | HTTP status, token environment, leading `/` in `prefix`, and Inbox notification | Wait or investigate the file registration before submitting metadata; do not assume SFTP success means API visibility. |
| API submission fails or times out | Saved state and objects visible in the portal | Follow the [submission resumption procedure](submitter-portal.md#5-submit-and-preserve-state); do not start a fresh submission. |

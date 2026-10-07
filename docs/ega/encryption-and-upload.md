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

| Symptom | Check |
| --- | --- |
| Samples not found | Names, directories, and one CRAM + one VCF per ID in `samples.txt` |
| Encryption fails | `crypt4gh` executable, current public key, and free space |
| SFTP fails | Account, password, user status, and SSH host-key fingerprint |
| Remote file already exists | Path, size, and checksum; `skipped_existing` is not verified success |
| File absent from `/files` | Token, HTTP response, leading `/` in prefix, and Inbox notification |

If the stages run on different machines, use `ega encrypt` and
`ega upload-inbox` with an explicit file selection. Do not run `upload-inbox`
on a directory containing previous batches without restricting the selection.

# EGA: from input files to submission

These guides take a CRAM + VCF batch from a workstation or HPC environment to
the Submitter Portal. They are not LocalEGA deployment instructions.

First, install the environment as described in the [Quick Start](../../README.md#quick-start).
Run `impact-tools ega --help` and `crypt4gh --help` **in that same environment**.
API submission also requires `httpx`, which is a package dependency; a
different Python environment may not have it installed.

| Phase | Run where / required access | Credential or input | Evidence to keep |
| --- | --- | --- | --- |
| Encrypt | Machine with the source files and enough persistent space | Current recipient public key, `crypt4gh` | Encryption manifest and encrypted checksums |
| Upload | Machine with the `.c4gh` files and SFTP access to the Inbox | Inbox account and interactive authentication | Upload manifest with exact remote paths |
| Prepare and plan | Machine with reviewed metadata and `impact-tools`; no CEGA connection needed | Sample list and private metadata profile | Draft, evidence, missing fields, and plan |
| Verify and submit | Machine with HTTPS access to the API for the **same environment** | API bearer token | File-query results, responses, and submission state |
| Finalise | Submitter Portal, by an authorised person | Portal access | Finalisation and subsequent ingestion checks |

Encryption and upload may run on different machines; see the
[separate-stage example](encryption-and-upload.md#4-encrypt-and-upload-on-different-machines).
The SFTP password and API token serve different purposes. Keep TEST and
production endpoints, credentials, and recipient keys separate.

1. [Encryption and Inbox upload](encryption-and-upload.md): sample selection,
   recipient public key, transfer, and per-file evidence.
2. [Submitter Portal submission](submitter-portal.md): metadata profile, draft,
   review, plan, API token, execution, and resumption. The token is needed for
   API verification and submission, not for the SFTP upload.

```mermaid
flowchart LR
    A["CRAM + VCF and sample list"] --> B["encrypt-upload"]
    B --> C["Inbox: .c4gh files"]
    A --> D["prepare-submission"]
    M["Reviewed metadata and profile"] --> D
    D --> E["Submission draft"]
    C --> F["Verify files through the API"]
    E --> G["Review submission plan"]
    F --> H["submit-submission --execute"]
    G --> H
    H --> I["Open Dataset"]
    I -.-> J["Manual finalisation in the portal"]
    J --> K["Ingestion and distribution"]
```

**Tool boundary:** `submit-submission` creates metadata objects and leaves the
Dataset open. It does not finalise the submission, release the Dataset, grant
download permissions, or verify ingestion into the Vault.

Names such as `SAMPLE_001`, `/data/...` paths, usernames, and endpoints in
these guides are illustrative. Replace them with approved values for your
environment. Never commit credentials, tokens, private keys, or sensitive
metadata to this repository.

| Use case | Tools |
| --- | --- |
| CRAM + VCF batch | `encrypt-upload` → `prepare-submission` → `submit-submission` |
| Separate encryption and upload | `encrypt` and `upload-inbox` |
| FASTQ test | Encrypt/upload, then prepare a separate metadata draft |

`prepare-submission` is specialised for CRAM + VCF; it does not infer metadata
from a FASTQ. For command options, run `impact-tools ega <command> --help`.

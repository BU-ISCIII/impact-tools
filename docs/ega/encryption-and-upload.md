# Cifrado y subida al Inbox

[Mapa EGA](README.md) · [Continuar con la submission](submitter-portal.md)

Este recorrido usa un lote CRAM + VCF. Los comandos se ejecutan donde están
los originales (estación de trabajo o HPC), con acceso SFTP al Inbox; **no**
dentro del contenedor LocalEGA.

## 1. Preparar el lote

Necesitas `impact-tools`, un ejecutable `crypt4gh` funcional, una cuenta de
Inbox y la **clave pública Crypt4GH vigente del servicio receptor**. Verifica
su huella con el administrador: una clave antigua puede permitir cifrar y subir,
pero impedir la ingestión posterior.

Estructura ilustrativa:

```text
/data/delivery/
├── SAMPLE_001/
│   ├── SAMPLE_001.cram
│   └── SAMPLE_001.vcf.gz
└── SAMPLE_002/
    ├── SAMPLE_002.cram
    └── SAMPLE_002.vcf.gz
```

Prepara el fichero de selección con una línea por muestra:

```bash
mkdir -p /data/ega-runs/cohort-01
printf '%s\n' SAMPLE_001 SAMPLE_002 > /data/ega-runs/cohort-01/samples.txt
```

Con `--sample-list`, la herramienta exige
exactamente un CRAM y un VCF por muestra y detiene el lote si faltan ficheros,
están vacíos o hay candidatos ambiguos. Reutiliza **esa misma lista** al
preparar los metadatos.

Comprueba que los originales son legibles y que el directorio de salida tiene
espacio para otra copia del lote, además de informes. Para CRAM grandes, evita
`/tmp` y usa almacenamiento persistente. No incluyas datos de pacientes en
nombres o ejemplos compartidos.

## 2. Cifrar y subir solo lo seleccionado

Sustituye las rutas y parámetros de este ejemplo. La clave privada de LocalEGA
**no** se usa en esta máquina.

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

La contraseña se solicita interactivamente: no la pongas en argumentos o
ficheros versionados. Verifica la huella SSH del Inbox antes de confiar en él.
Para rechazar claves desconocidas, configura `ega.inbox.host_key_policy:
reject` en la configuración de usuario; `encrypt-upload` lee esa opción de
allí. Ante un cambio inesperado, detente y confirma la huella con el
administrador.

`--remote-layout relative` conserva la carpeta de muestra. La ruta remota
esperada del CRAM es `/SAMPLE_001/SAMPLE_001.cram.c4gh`; debe coincidir con
el borrador de submission. El comando combinado sube únicamente los resultados
seleccionados para este lote, no cualquier `.c4gh` antiguo del directorio.

## 3. Revisar el resultado

Guarda `workflow_reports/`, sus manifiestos y el registro SQLite en un lugar
persistente. Revisa el informe HTML y el manifiesto de subida:

- Debe haber dos entradas por muestra (CRAM y VCF), sin estado `failed`.
- Contrasta ruta remota, tamaño y SHA-256 cifrado de cada `.c4gh`.
- `skipped_existing` solo significa que ya había un fichero remoto: **no**
  prueba que sea el correcto. `skipped_registered` debe corresponder al mismo
  contenido y destino registrado previamente.

La transferencia SFTP no equivale a ingestión. Antes de enviar metadatos,
comprueba en el portal o su API que cada fichero está en estado `inbox`, con
ruta exacta (incluida la barra inicial) y checksum concordante. El registro
local acredita operaciones de cifrado/subida, no accesiones o release en CEGA.

| Síntoma | Qué comprobar |
| --- | --- |
| No encuentra muestras | Nombres, carpetas y un CRAM + un VCF por ID de `samples.txt` |
| Falla el cifrado | Ejecutable `crypt4gh`, clave pública vigente y espacio libre |
| Falla SFTP | Cuenta, contraseña, estado del usuario y huella SSH |
| Ya existe en remoto | Ruta, tamaño y checksum; no asumas que `skipped_existing` equivale a éxito |
| No aparece en `/files` | Token, respuesta HTTP y prefijo con `/` inicial; espera la notificación del Inbox |

Si separas las etapas entre máquinas, utiliza `ega encrypt` y `ega
upload-inbox` con una selección explícita de ficheros. No lances
`upload-inbox` contra una carpeta con lotes anteriores sin restringirla.

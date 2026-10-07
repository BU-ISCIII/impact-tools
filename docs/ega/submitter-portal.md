# Submission al Submitter Portal

[Mapa EGA](README.md) · [Cifrado y subida](encryption-and-upload.md)

Esta guía continúa el lote CRAM + VCF anterior. Ejecuta los comandos en una
máquina con `impact-tools` y acceso HTTPS al **mismo entorno CEGA** que recibió
los `.c4gh`. TEST y producción no son intercambiables. Solo una persona
autorizada debe enviar metadatos reales.

## 1. Preparar los metadatos

Usa exactamente la lista de muestras del cifrado. Crea una copia privada del
perfil apropiado para el proveedor y completa sus valores validados; los
perfiles de `impact_tools/conf/ega/submission_profiles/` son plantillas, no
metadatos de un estudio concreto. Los metadatos complementarios se pueden
aportar como CSV, TSV o JSON; las filas CSV/TSV se identifican por `sample_id`
o `alias`.

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

Si no hay fichero complementario, omite `--metadata-file`. Revisa después
`evidence.md`, `missing_fields.yaml`, `file_inventory.tsv` y
`draft_submission.yaml`:

1. Completa los campos pendientes y confirma el origen de los valores en
   `evidence.md`. **No** uses `--include-examples` con datos reales.
2. Revisa título y descripción, sujeto seudonimizado, muestras, modelo de
   instrumento, tipo de análisis, genoma, cromosomas, tipos de Dataset y
   política autorizada. Los IDs y enumeraciones dependen de la API de destino;
   no se deducen de los nombres de fichero.
3. Cada muestra debe tener un Run para su CRAM y un Analysis para su VCF; el
   Dataset debe enlazarlos todos. Los nombres de `files` deben corresponder a
   los `.c4gh` subidos, incluida la carpeta de muestra si se usó
   `--remote-layout relative`.
4. Comprueba que no quedan `EXAMPLE:` ni valores ficticios en el borrador.

## 2. Generar un plan sin enviar

```bash
impact-tools ega submit-submission \
  --draft-file "$DRAFT_DIR/draft_submission.yaml" \
  --output-dir "$RUN_DIR/submission_plan"
```

Esto escribe `submission_plan.json` y los JSON de `payloads/` **sin llamar a la
API**. Comprueba Submission → Study → Samples → Experiments → resolución de
ficheros → Runs → Analyses → Dataset. El plan no demuestra que los ficheros
existan en CEGA ni que el servidor acepte los metadatos.

## 3. Comprobar los ficheros en CEGA

Antes del primer `--execute`, consulta **cada ruta exacta** del Inbox. Este
ejemplo es de solo lectura; obtén el token por el procedimiento autorizado para
el entorno de destino y protégelo con permisos restrictivos.

```bash
API_BASE='<submitter-portal-api-base>'
TOKEN_FILE=/secure/ega/access_token
REMOTE_FILE=/SAMPLE_001/SAMPLE_001.cram.c4gh

curl --fail-with-body --get --silent --show-error \
  -H "Authorization: Bearer $(cat "$TOKEN_FILE")" \
  --data-urlencode 'status=inbox' \
  --data-urlencode "prefix=$REMOTE_FILE" \
  "$API_BASE/files"
```

Comprueba HTTP 200 y **una coincidencia exacta** de ruta, estado `inbox`,
tamaño y checksum cifrado frente al manifiesto de subida. Una lista vacía no
es éxito: revisa la barra inicial de `prefix` y la vigencia del token. Repite
para CRAM y VCF de todas las muestras. No compartas el token ni pegues
respuestas sensibles en incidencias públicas.

La comprobación previa importa porque `submit-submission` resuelve los
ficheros **después** de crear los primeros objetos de metadatos. Si falla esa
resolución, la submission puede quedar parcialmente creada.

## 4. Enviar y guardar el estado

Solo después de revisar el borrador, el plan y los ficheros remotos:

```bash
impact-tools ega submit-submission \
  --draft-file "$DRAFT_DIR/draft_submission.yaml" \
  --output-dir "$RUN_DIR/submission_execute" \
  --api-base "$API_BASE" \
  --token-file "$TOKEN_FILE" \
  --execute
```

Guarda `submission_state.json`, `submission_plan.json`, `payloads/` y
`responses/` en un directorio privado y persistente. Verifica en el portal los
IDs provisionales, los Runs, los Analyses y el Dataset, que debe seguir
**abierto**. No repitas el envío con un directorio nuevo como si fuera otro
lote: podrías duplicar los objetos.

Si hay un fallo, revisa el estado y el portal. Mantén el borrador **sin
modificar** (se comprueba su hash al reanudar) y utiliza:

```bash
impact-tools ega submit-submission \
  --draft-file "$DRAFT_DIR/draft_submission.yaml" \
  --output-dir "$RUN_DIR/submission_resume" \
  --api-base "$API_BASE" \
  --token-file "$TOKEN_FILE" \
  --resume-state-file "$RUN_DIR/submission_execute/submission_state.json" \
  --execute
```

Si se perdió la conexión justo tras un `POST`, comprueba en el portal si el
objeto se creó antes de reintentar: un timeout no garantiza que la operación
haya fallado en el servidor. No cambies IDs del estado para forzar el reintento.

## Límite manual y variante FASTQ

La herramienta **no finaliza ni libera** el Dataset. La finalización y la
fecha de liberación se gestionan manualmente en el portal por una persona
autorizada. Ingestión, accesiones, permisos y descarga por Distribution se
comprueban después; un Dataset abierto no demuestra que el fichero esté en el
Vault o sea descargable.

`prepare-submission --sample-list` genera Runs de CRAM y Analyses de VCF. Para
una prueba FASTQ, se puede usar `encrypt-upload` con el patrón adecuado, pero
el borrador de metadatos se prepara **aparte** con un Run `fastq`. No reutilices
superficialmente un borrador CRAM + VCF: formato, metadatos y enlaces a ficheros
deben corresponder al FASTQ subido.

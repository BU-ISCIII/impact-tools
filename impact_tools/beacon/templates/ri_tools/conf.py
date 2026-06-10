#### Input and Output files config parameters ####
csv_folder = './csv/'
output_docs_folder = './output_docs/'
entry_type = 'all'

#### VCF Conversion config parameters ####
only_process_reads_with_allele_frequency = True
populations_by_allele_counts = True
reference_genome = "{reference_genome}"
datasetId = "{dataset_id}"
case_level_data = False
num_rows = 15000000
verbosity = False

### Update record ###
record_type = 'genomicVariation'
collection_name = 'genomicVariations'

### MongoDB parameters ###
database_host = 'mongo'
database_port = 27017
database_user = 'root'
database_password = 'example'
database_name = 'beacon'
database_auth_source = 'admin'
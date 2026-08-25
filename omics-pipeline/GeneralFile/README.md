# GeneralFile

Shared annotation and reference resources used by the analysis scripts.

Scripts resolve this directory as `Script/../GeneralFile` unless you set `--general_file`.

## Expected contents

| Item | Used by |
|------|---------|
| `CellMarker/cellMarker_Hs.txt`, `cellMarker_Mm.txt` | RNA-seq, microarray, scRNA |
| `gene_length_Hs.txt`, `gene_length_Mm.txt` | RNA-seq (TPM) |
| `GEOMods.R` | Microarray GPL helpers |
| `scType/` (`ScTypeDB_full.xlsx`, helper `.R`) | scRNA ScType; `tissueType` values for `--tissue` / `tissue` / `tissue_type` |
| `SingleR/HumanPrimaryCellAtlasData.rds` | scRNA annotation (human) |
| `SingleR/MouseRNAseqData.rds` | scRNA annotation (mouse) |
| `ssGSEA/ssGSEA_Hs.rds` | Human ssGSEA (optional; skipped if missing) |
| `ref_genome/` | Upstream RNA-seq (FASTA, GTF, HISAT2 index) |

Download FASTA into `ref_genome/` ([ref_genome/README.md](ref_genome/README.md)), then build indexes. See [USAGE.md](../USAGE.md) for file names and commands.

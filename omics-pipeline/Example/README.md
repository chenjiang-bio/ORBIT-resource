# Example datasets

Prepared inputs for each pipeline. Paths are relative to the `omics-pipeline/` directory.

| Pipeline | Expression input | Example command |
|----------|------------------|-----------------|
| RNA-seq | `{GSE}.{org}.ExprMatrix.txt` (raw counts) | `Rscript Script/run_RNA_seq.R --work_dir Example/RNA_seq --organism hsa --gse GSE111082` |
| Microarray | `{GSE}.{org}.ExprMatrix.txt` (Symbol × samples) | `Rscript Script/run_MicroArray.R --work_dir Example/MicroArray --organism hsa --gse GSE9196` |
| scRNA-seq | `ExprMatrix/` (10x / h5 / txt) | `Rscript Script/run_scRNA_seq.R --work_dir Example/scRNA_seq --organism mmu --gse GSE223368 --mode auto --tissue Intestine` |

See [USAGE.md](../USAGE.md) for column requirements and options.

## Contents

- `samples_info.txt`, `comparisons.txt`, `gse_list*.txt`
- expression matrices under each GSE folder / `ExprMatrix/`

# Omics Pipeline

Batch analysis pipelines for public GEO organoid / tissue omics datasets (human, *Homo sapiens*; mouse, *Mus musculus*):

- **Bulk RNA-seq** - upstream (download → align → count) and downstream (DEG → enrichment → GSEA → GSVA)
- **Microarray** - GEO prepare + limma DEG + enrichment / GSEA / GSVA
- **scRNA-seq** - Seurat QC / clustering / annotation, optional multi-group DE and pseudobulk enrichment

Species codes: `hsa` (human, *Homo sapiens*), `mmu` (mouse, *Mus musculus*).

This module is part of [ORBIT-resource](https://github.com/chenjiang-bio/ORBIT-resource).

## Documentation


| Document                                               | Contents                                                  |
| ------------------------------------------------------ | --------------------------------------------------------- |
| [USAGE.md](USAGE.md) | Install, references, inputs, and how to run each pipeline |
| [Example/README.md](Example/README.md)                 | Example inputs                                            |
| [GeneralFile/README.md](GeneralFile/README.md)         | Annotation and reference data layout                      |




## Quick start

```bash
# Install R / Bioconductor dependencies
Rscript Script/install_deps.R --type all

# Bulk RNA-seq (downstream)
Rscript Script/run_RNA_seq.R \
  --work_dir Example/RNA_seq --organism hsa --gse GSE111082

# Microarray
Rscript Script/run_MicroArray.R \
  --work_dir Example/MicroArray --organism hsa --gse GSE9196

# scRNA-seq
Rscript Script/run_scRNA_seq.R \
  --work_dir Example/scRNA_seq --organism mmu --gse GSE223368 \
  --mode auto --tissue Intestine
```



## Layout

```text
omics-pipeline/
  Script/        # pipeline entry points and helpers
  GeneralFile/   # gene sets and annotations
  Example/       # prepared inputs
```


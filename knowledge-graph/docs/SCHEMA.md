# ORBIT knowledge-graph schema

The graph is built from curated ORBIT sample tables plus linked transcriptomic and gene-annotation layers. Construction is driven by [`config/kg_mapping.json`](../config/kg_mapping.json); edit that file rather than hard-coding column names in Python.

## Design principles

1. **Sample-centric.** Biological evidence is anchored on `:Sample` nodes (`sample_id`, e.g. `KM-14955`).
2. **Source-linked.** Publications, omics accessions, comparison groups and annotation records keep stable identifiers.
3. **Role-aware omics.** `HAS_OMICS` distinguishes `role='analyzed'` (ORBIT re-analysis present) from `role='cited'` (accession mentioned only).
4. **Comparison-resolved molecular results.** Differential genes, cluster markers and pathway terms hang off `:GroupInfo`, not directly off `:Sample`.

## Core path used in manuscript case studies

```text
(:Sample)-[:HAS_PHENOTYPE]->(:Phenotype)
(:Sample)-[:HAS_GROUP_INFO]->(:GroupInfo)
(:GroupInfo)-[:HAS_CLUSTER_MARKER]->(:ClusterMarkers)
(:ClusterMarkers)-[:HAS_GENE_ANNOTATION]->(:GeneAnnotation)
(:Sample)-[:HAS_OMICS]->(:Omics)-[:USES_PLATFORM]->(:Platform)
(:Sample)-[:MODELS_DISEASE]->(:DiseaseModel)
(:Sample)-[:REPORTED_IN]->(:Publication)
```

## Important field notes

| Node / edge | Field | Note |
|-------------|-------|------|
| `Sample` | `sample_id` | Public identifier used in the portal and manuscript |
| `Omics` | `name` / `omics_id` | Dataset accession (`GSE…`); do not use a legacy `accession` property |
| `HAS_OMICS` | `role` | `analyzed` vs `cited` |
| `GroupInfo` | `id` | Unique comparison key; do not aggregate on `name` alone |
| `GroupInfo` | `group` | Full contrast string (includes control arm) |
| `GroupInfo` | `GSE_ID` | Source dataset for the contrast |
| `ClusterMarkers` | `symbol`, `cluster` | Membership only; no effect size stored |
| `GeneAnnotation` | `string_rows`, `crispick_rows` | Database-derived partners / sgRNA designs |

## Scale (2026-08-07 build)

| Quantity | Value |
|----------|------:|
| Nodes | 4,557,354 |
| Relationships | 6,816,859 |
| Node labels | 34 |
| Relationship types | 37 |

Offline SQLite exports are checksum-matched to Neo4j for label and relationship-type counts in the release audit.

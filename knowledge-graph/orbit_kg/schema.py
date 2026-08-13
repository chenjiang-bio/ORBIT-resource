"""High-level schema constants for the ORBIT knowledge graph.

Counts reflect the 2026-08-07 release build reported in the manuscript
(34 node labels, 37 relationship types). The lists below are the labels and
relationship types used by the public case-study queries; the live graph may
contain additional types as the mapping config evolves.
"""

from __future__ import annotations

NODE_LABELS = (
    "Sample",
    "Organism",
    "Organ",
    "System",
    "Source",
    "DiseaseModel",
    "Application",
    "ApplicationStrategy",
    "Publication",
    "Gene",
    "CellFactor",
    "Technology",
    "Drug",
    "Infection",
    "Biomarker",
    "Phenotype",
    "Test",
    "Omics",
    "Platform",
    "Composition",
    "CultureProtocol",
    "CultureTechnique",
    "GroupInfo",
    "GroupDEGs",
    "ClusterMarkers",
    "IntraClusterDEGs",
    "GSVA",
    "GSEA",
    "GeneAnnotation",
)

RELATIONSHIP_TYPES = (
    "FROM_ORGANISM",
    "FROM_ORGAN",
    "BELONGS_TO_SYSTEM",
    "DERIVED_FROM",
    "MODELS_DISEASE",
    "HAS_APPLICATION",
    "HAS_STRATEGY",
    "REPORTED_IN",
    "TARGETS_GENE",
    "USES_FACTOR",
    "USES_TECHNOLOGY",
    "USES_TECHNIQUE",
    "USES_PROTOCOL",
    "USES_COCULTURE_PROTOCOL",
    "SCREENS_DRUG",
    "HAS_INFECTION",
    "HAS_BIOMARKER",
    "HAS_PHENOTYPE",
    "HAS_TEST",
    "HAS_OMICS",
    "USES_PLATFORM",
    "HAS_COMPOSITION",
    "HAS_GROUP_INFO",
    "HAS_DEG",
    "HAS_CLUSTER_MARKER",
    "HAS_INTRACLUSTER_DEG",
    "HAS_GSVA_PATHWAY",
    "HAS_GSEA_PATHWAY",
    "HAS_GENE_ANNOTATION",
    "ASSOCIATED_WITH_DISEASE",
)

# Omics.role values retained on HAS_OMICS edges / Omics nodes
OMICS_ROLE_ANALYZED = "analyzed"
OMICS_ROLE_CITED = "cited"

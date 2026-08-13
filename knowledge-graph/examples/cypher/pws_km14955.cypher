// Prader-Willi arcuate organoid case study (KM-14955 / Figure 6D)
// Run against a loaded ORBIT Neo4j database.

// Q1. One-hop neighbourhood
MATCH (s:Sample {sample_id:'KM-14955'})-[r]->(x)
RETURN type(r) AS rel, count(*) AS n
ORDER BY n DESC;

// Q2. Curated phenotypes
MATCH (s:Sample {sample_id:'KM-14955'})-[:HAS_PHENOTYPE]->(p:Phenotype)
RETURN p.id AS node, p.name AS phenotype;

// Q3. DLK1 as a cluster marker
MATCH (s:Sample {sample_id:'KM-14955'})-[:HAS_GROUP_INFO]->(g:GroupInfo)
      -[:HAS_CLUSTER_MARKER]->(c:ClusterMarkers)
WHERE toUpper(coalesce(c.symbol, c.name, '')) = 'DLK1'
RETURN count(DISTINCT g) AS groups, count(DISTINCT c) AS marker_nodes, count(*) AS edges;

// Q3b. Radial-glial marker gene set in GSE164102
MATCH (g:GroupInfo {GSE_ID:'GSE164102'})-[:HAS_CLUSTER_MARKER]->(c:ClusterMarkers)
WHERE c.cluster = 'Radial glial cells'
RETURN count(DISTINCT toUpper(coalesce(c.symbol, c.name, ''))) AS n_marker_genes;

// Q4. Gene annotation for DLK1 (STRING + CRISPick)
MATCH (s:Sample {sample_id:'KM-14955'})-[:HAS_GROUP_INFO]->(g:GroupInfo)
      -[:HAS_CLUSTER_MARKER]->(c:ClusterMarkers)-[:HAS_GENE_ANNOTATION]->(a:GeneAnnotation)
WHERE toUpper(coalesce(c.symbol, c.name, '')) = 'DLK1'
RETURN DISTINCT a.gene_annotation_entrez_id, a.gene_annotation_gene_name,
       a.organism_species, a.gene_annotation_pfam, a.gene_annotation_prosite,
       size(a.string_rows) AS string_partners, size(a.crispick_rows) AS crispick_designs;

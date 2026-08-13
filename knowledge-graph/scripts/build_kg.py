#!/usr/bin/env python3
"""
build_kg.py — Build an organoid culture knowledge graph from a MySQL database

Reference: Knowledge graph construction methodology from MOF-ChemUnity (JACS 2025)

Features:
  1. Connect to MySQL database and automatically explore table structure
  2. Map structured data to knowledge graph nodes and edges
  3. Output JSON format graph file (universal format, downloadable for anyone)
  4. Output SQLite format graph file (supports local SQL queries)

Usage:
  # Explore database structure
  python build_kg.py --host localhost --database organoid_db --user root --password xxx --explore

  # Build knowledge graph (JSON + SQLite)
  python build_kg.py --host localhost --database organoid_db --user root --password xxx \\
      --output-dir ./output --format json sqlite
"""

import json
import sqlite3
import argparse
import sys
import ast
import os
import hashlib
from datetime import datetime
from typing import Dict, List, Optional, Tuple, Any
from collections import defaultdict

# =============================================================================
# Configuration — loaded from kg_mapping.json (edit the JSON, not this file)
# =============================================================================

def _load_mapping_config(config_path: str = None):
    """Load KG mapping configuration from JSON file and set module-level globals.

    Called at module import time with the default JSON.  Can be re-called at
    runtime with a custom path (via --mapping) to override the defaults before
    building.

    The JSON uses descriptive object keys for readability; this function converts
    them to the tuple/list formats expected by the rest of the code.
    """
    global DEFAULT_TABLE_MAPPING, FOCUS_TABLES, MANUAL_TABLE_TYPES
    global WIDE_TABLE_ENTITY_MAPPING, COMPOUND_ENTITY_CONFIG
    global INFERRED_RELATIONS, JSON_ENTITY_NAME_KEYS, COCULTURE_COLUMNS
    global GENE_ANNOTATION_DRUG_LOOKUP
    global MATERIAL_INFO_CONFIG, MATERIAL_SIMILARITY_CONFIG, CORE_ROW_FILTER

    if config_path is None:
        # Prefer module-root config/; fall back to scripts/ for local copies.
        here = os.path.dirname(os.path.abspath(__file__))
        candidates = [
            os.path.join(here, '..', 'config', 'kg_mapping.json'),
            os.path.join(here, 'kg_mapping.json'),
        ]
        config_path = next((p for p in candidates if os.path.isfile(p)), candidates[0])
        config_path = os.path.normpath(config_path)

    with open(config_path, 'r', encoding='utf-8') as f:
        cfg = json.load(f)

    DEFAULT_TABLE_MAPPING = cfg["table_mapping"]
    FOCUS_TABLES = cfg["focus_tables"]
    MANUAL_TABLE_TYPES = {k: tuple(v) for k, v in cfg.get("manual_table_types", {}).items()}

    # wide_table_entity_mapping: descriptive objects → tuples (indexed access in build logic)
    wide_mapping = {}
    for col, entry in cfg["wide_table_entity_mapping"].items():
        parts = [entry["node_type"], entry["id_prefix"], entry["relation"], entry["edge_from"]]
        if entry.get("is_json"):
            parts.append("json")
        wide_mapping[col] = tuple(parts)
    WIDE_TABLE_ENTITY_MAPPING = wide_mapping

    COMPOUND_ENTITY_CONFIG = cfg["compound_entity_config"]

    # inferred_relations: array of objects → list of tuples
    INFERRED_RELATIONS = [
        (r["source_column"], r["target_column"], r["source_type"], r["target_type"], r["relation"])
        for r in cfg.get("inferred_relations", [])
    ]

    JSON_ENTITY_NAME_KEYS = cfg["json_entity_name_keys"]
    COCULTURE_COLUMNS = set(cfg["coculture_columns"])

    GENE_ANNOTATION_DRUG_LOOKUP = cfg.get("gene_annotation_drug_lookup", {})

    MATERIAL_INFO_CONFIG = cfg.get("material_info_config", {})
    MATERIAL_SIMILARITY_CONFIG = cfg.get("material_similarity_config", {})
    CORE_ROW_FILTER = cfg.get("core_row_filter", {})

    print(f"[INFO] KG mapping loaded from: {config_path}")
    if CORE_ROW_FILTER:
        for _t, _f in CORE_ROW_FILTER.items():
            print(f"[INFO] Core row filter: {_t}.{_f['column']} IN {_f['include_values']}")

# Load defaults at module import time
_load_mapping_config()

# Truncation limit for GroupInfo sub-node queries (GroupDEGs, IntraClusterDEGs,
# ClusterMarkers, GSVA, GSEA). Max rows fetched per GSE_ID per sub-table.
# Set to None for unlimited. Default 50 — keeps the top rows per GSE_ID,
# drastically reducing KG build time while preserving the most relevant entries.
GROUPINFO_SUB_NODE_MAX_ROWS = 100

# Max STRING interaction partners retained per gene, ranked by combined_score.
# STRING returns hundreds to thousands of partners per gene; embedding all of them
# in GeneAnnotation properties inflates the exported snapshot by an order of
# magnitude without adding usable evidence. Partners are deduplicated by symbol
# (keeping the best score) before truncation. Set to None to keep everything.
STRING_PARTNERS_PER_GENE = 50

# Max material items to extract per protocol for MaterialInfo nodes.
# Protocols may list dozens of materials; taking the first N per protocol
# is sufficient for coverage while bounding node count and similarity matching.
MATERIAL_INFO_MAX_ITEMS = 0

# Truncation limit for gene annotation table queries (Step G).
# Max rows fetched per batch of symbols for each annotation table.
# STRING tables can have 11M+ rows — this limit prevents massive scans.
# Set to None for unlimited. Default 5000.
GENE_ANNOTATION_MAX_ROWS = 0


class MySQLKnowledgeGraphBuilder:
    """Build a knowledge graph from a MySQL database"""

    def __init__(self, host: str, port: int, user: str, password: str,
                 database: str, focus_tables: List[str] = None,
                 mapping_path: str = None):
        self.connection_params = {
            "host": host, "port": port, "user": user,
            "password": password, "database": database
        }
        self.mapping_path = mapping_path
        self.focus_tables = focus_tables if focus_tables is not None else (FOCUS_TABLES or None)
        self.table_mapping = DEFAULT_TABLE_MAPPING

        # Apply table filtering (focused tables)
        if self.focus_tables:
            filtered = {}
            for table, cfg in self.table_mapping.items():
                for focus in self.focus_tables:
                    if focus.lower() in table.lower():
                        filtered[table] = cfg
                        break
            if filtered:
                self.table_mapping = filtered

        self.conn = None

    # -------------------------------------------------------------------------
    # Database connection and exploration
    # -------------------------------------------------------------------------

    def connect(self):
        """Establish MySQL connection"""
        try:
            import pymysql
            self.conn = pymysql.connect(
                **self.connection_params,
                charset='utf8mb4',
                cursorclass=pymysql.cursors.DictCursor
            )
            print(f"[OK] Connected to MySQL: {self.connection_params['host']}/{self.connection_params['database']}")
        except ImportError:
            print("[ERROR] pymysql not installed. Run: pip install pymysql")
            sys.exit(1)
        except Exception as e:
            print(f"[ERROR] Failed to connect to MySQL: {e}")
            sys.exit(1)

    def _filter_tables(self, all_tables: List[str]) -> List[str]:
        """Filter table names based on self.focus_tables (substring match)"""
        if not self.focus_tables:
            return all_tables
        filtered = []
        for table in all_tables:
            for focus in self.focus_tables:
                if focus.lower() in table.lower():
                    filtered.append(table)
                    break
        skipped = set(all_tables) - set(filtered)
        if skipped:
            print(f"[INFO] Focus mode: processing {len(filtered)}/{len(all_tables)} tables "
                  f"(skipped: {', '.join(sorted(skipped))})")
        return filtered

    def explore(self) -> dict:
        """Explore database table structure, return table list and field info (with annotations)"""
        self.connect()
        schema_info = {"tables": {}, "foreign_keys": []}

        with self.conn.cursor() as cursor:
            # Get all tables
            cursor.execute("SHOW TABLES")
            all_tables = [row[list(row.keys())[0]] for row in cursor.fetchall()]
            tables = self._filter_tables(all_tables)

            for table in tables:
                # Get table comment
                table_comment = ""
                try:
                    cursor.execute(f"SHOW TABLE STATUS WHERE Name = '{table}'")
                    status = cursor.fetchone()
                    if status and status.get("Comment"):
                        table_comment = status["Comment"]
                except Exception:
                    pass

                # Get column info (with comments)
                cursor.execute(f"SHOW FULL COLUMNS FROM `{table}`")
                columns = cursor.fetchall()

                # Get row count
                cursor.execute(f"SELECT COUNT(*) as cnt FROM `{table}`")
                row_count = cursor.fetchone()["cnt"]

                schema_info["tables"][table] = {
                    "comment": table_comment,
                    "columns": [
                        {
                            "name": col["Field"],
                            "type": col["Type"],
                            "null": col["Null"],
                            "key": col["Key"],
                            "default": col["Default"],
                            "comment": col.get("Comment", ""),
                        }
                        for col in columns
                    ],
                    "row_count": row_count
                }

            # Get foreign key relationships
            for table in tables:
                cursor.execute(f"""
                    SELECT COLUMN_NAME, REFERENCED_TABLE_NAME, REFERENCED_COLUMN_NAME
                    FROM INFORMATION_SCHEMA.KEY_COLUMN_USAGE
                    WHERE TABLE_SCHEMA = '{self.connection_params['database']}'
                      AND TABLE_NAME = '{table}'
                      AND REFERENCED_TABLE_NAME IS NOT NULL
                """)
                for row in cursor.fetchall():
                    schema_info["foreign_keys"].append({
                        "source_table": table,
                        "source_column": row["COLUMN_NAME"],
                        "target_table": row["REFERENCED_TABLE_NAME"],
                        "target_column": row["REFERENCED_COLUMN_NAME"]
                    })

        self.conn.close()
        return schema_info

    def print_explore(self, schema_info: dict):
        """Print database structure (with annotations); add column grouping for single wide table"""
        print("\n" + "=" * 70)
        print(f"Database: {self.connection_params['database']}")
        print("=" * 70)

        is_wide_table = len(schema_info["tables"]) == 1 and len(schema_info["foreign_keys"]) == 0

        for table, info in schema_info["tables"].items():
            pk_cols = [c["name"] for c in info["columns"] if c["key"] == "PRI"]
            comment_str = f"  -- {info['comment']}" if info.get("comment") else ""
            print(f"\n┌─ {table} ({len(info['columns'])} columns, {info['row_count']} rows){comment_str}")

            if is_wide_table:
                # Single wide table mode: display columns grouped by semantics
                columns = info["columns"]
                groups = self._group_columns(columns)
                for group_name, group_cols in groups.items():
                    print(f"│")
                    print(f"│   ┌─ [{group_name}] ({len(group_cols)} columns)")
                    for col in group_cols:
                        markers = []
                        if col["key"] == "PRI": markers.append("PK")
                        if col["key"] == "MUL": markers.append("FK")
                        marker_str = f" ({', '.join(markers)})" if markers else ""
                        col_comment = f"  // {col['comment']}" if col.get("comment") else ""
                        print(f"│   │   ├── {col['name']:28s} {col['type']:18s}{marker_str}{col_comment}")
                print(f"│")
                print(f"│   Tip: This is a single wide table. Edit WIDE_TABLE_ENTITY_MAPPING")
                print(f"│        in the script config to customize entity extraction from columns.")
            else:
                # Multi-table mode: simple listing
                for col in info["columns"]:
                    markers = []
                    if col["key"] == "PRI": markers.append("PK")
                    if col["key"] == "MUL": markers.append("FK")
                    marker_str = f" ({', '.join(markers)})" if markers else ""
                    col_comment = f"  // {col['comment']}" if col.get("comment") else ""
                    print(f"│   ├── {col['name']:30s} {col['type']:20s}{marker_str}{col_comment}")

        if schema_info["foreign_keys"]:
            print(f"\nForeign Keys:")
            for fk in schema_info["foreign_keys"]:
                print(f"  {fk['source_table']}.{fk['source_column']} → {fk['target_table']}.{fk['target_column']}")

    @staticmethod
    def _group_columns(columns: List[dict]) -> Dict[str, List[dict]]:
        """Group wide-table columns by semantics for easier understanding of table structure"""
        groups = {
            "Identity & Key": [],
            "Organoid Identity": [],
            "Culture & Protocol": [],
            "Co-culture": [],
            "Biomarkers & Characterization": [],
            "Drug & Phenotype": [],
            "Application & Disease": [],
            "Genomics & Omics": [],
            "Reference & Source": [],
            "Metadata & System": [],
            "Other": [],
        }

        # Keyword → group mapping
        keyword_groups = {
            "Identity & Key": ["sample_id", "accession", "is_analyzed", "is_organoid", "deleted_at"],
            "Organoid Identity": ["organoid", "organ", "source", "organism", "canonical_name",
                                   "maturity", "characteristics", "functions", "complexity"],
            "Culture & Protocol": ["culture_technique", "cultivation_protocol", "cultivation_material",
                                    "culture_days", "culture_condition", "cell_factors", "techologies",
                                    "time_anchors", "composition"],
            "Co-culture": ["coculture", "coculture_cultivation", "coculture_condition",
                            "coculture_days", "coculture_cell_factors", "coculture_technique",
                            "read_out"],
            "Biomarkers & Characterization": ["biomarker", "endpoints"],
            "Drug & Phenotype": ["drug_screening", "phenotype_identification", "test",
                                  "infection_list"],
            "Application & Disease": ["application", "application_strategy", "disease_modeling"],
            "Genomics & Omics": ["omics_id", "gene_name", "sgrna", "platform"],
            "Reference & Source": ["reference", "doi", "search_content"],
            "Metadata & System": ["system"],
        }

        for col in columns:
            col_name = col["name"].lower()
            placed = False
            for group, keywords in keyword_groups.items():
                if any(kw in col_name for kw in keywords):
                    groups[group].append(col)
                    placed = True
                    break
            if not placed:
                groups["Other"].append(col)

        # Remove empty groups
        return {k: v for k, v in groups.items() if v}

    @staticmethod
    @staticmethod
    def _table_name_to_node_type(table_name: str) -> str:
        """Convert table name to CamelCase node type name"""
        cleaned = table_name.replace("_table", "").replace("_tbl", "")
        parts = cleaned.split("_")
        return "".join(p.capitalize() for p in parts)

    # -------------------------------------------------------------------------
    # Knowledge graph construction
    # -------------------------------------------------------------------------

    def build(self) -> Tuple[List[dict], List[dict]]:
        """Extract data from MySQL wide table and build knowledge graph nodes and edges

        From the public_general_2026 single table: Sample core node + JSON column entity extraction + inferred relations.
        See WIDE_TABLE_ENTITY_MAPPING, INFERRED_RELATIONS for configuration.
        """
        # Reload mapping if a custom config path was provided
        if self.mapping_path:
            _load_mapping_config(self.mapping_path)

        self.connect()
        print(f"[INFO] Using WIDE_TABLE_ENTITY_MAPPING to extract entities from columns")
        result = self._build_from_wide_table()
        self.conn.close()
        return result

    # -------------------------------------------------------------------------
    # Wide table mode construction
    # -------------------------------------------------------------------------

    def _build_from_wide_table(self) -> Tuple[List[dict], List[dict]]:
        """Build knowledge graph from single wide table (public_general_2026)"""
        nodes = []
        edges = []
        # Entity registry: (node_type, entity_name) -> node_id
        entity_registry = {}
        # For inferred relations: sample_id -> set of entity node IDs
        sample_entities = defaultdict(lambda: defaultdict(set))
        # Organ registry: organ_name -> organ_node_id
        organ_registry = {}

        # Determine the wide table name
        table_name = list(self.table_mapping.keys())[0]
        mapping = self.table_mapping[table_name]
        core_type = mapping["node_type"]  # "Sample"
        core_prefix = mapping["id_prefix"]  # "smp"

        with self.conn.cursor() as cursor:
            # The wide table also holds non-organoid records (cell lines, 2D
            # iPSC-derived neurons, tumourspheres). ORBIT is defined as an organoid
            # resource, so the graph must be restricted to organoid samples; reading the
            # table unfiltered admitted 5,165 non-organoid samples alongside the 11,890
            # organoid ones. The filter is config-driven via core_row_filter in
            # kg_mapping.json.
            row_filter = (CORE_ROW_FILTER or {}).get(table_name)
            if row_filter:
                fcol = row_filter["column"]
                fvals = row_filter["include_values"]
                placeholders = ",".join(["%s"] * len(fvals))
                cursor.execute(f"SELECT COUNT(*) AS c FROM `{table_name}`")
                _total = cursor.fetchone()["c"]
                cursor.execute(
                    f"SELECT * FROM `{table_name}` WHERE `{fcol}` IN ({placeholders})", fvals)
                all_rows = cursor.fetchall()
                print(f"[INFO] Row filter on {table_name}: `{fcol}` IN {fvals} → "
                      f"{len(all_rows)} of {_total} rows retained "
                      f"({_total - len(all_rows)} excluded)")
            else:
                cursor.execute(f"SELECT * FROM `{table_name}`")
                all_rows = cursor.fetchall()
                print(f"[WARN] No core_row_filter configured for {table_name}; "
                      f"reading all rows")
            print(f"[INFO] Processing {len(all_rows)} rows from {table_name}")

            # ---- DEBUG: techologies column data exploration ----
            tech_debug = {"total_nonnull": 0, "col_missing_or_null": 0,
                          "parsed_ok": 0, "parsed_empty": 0,
                          "entity_extracted": 0, "samples": [],
                          "col_in_first_row": False, "first_row_cols": []}

            # ---- DEBUG: json_structured columns (CultureCondition, CoCultureTechnique) ----
            js_debug = {}
            for js_col in ["culture_condition", "coculture_technique"]:
                js_debug[js_col] = {"null_or_missing": 0, "parse_failed": 0,
                                    "non_dict_data": 0, "empty_data": 0,
                                    "extracted_ok": 0, "samples": []}
            # ---- DEBUG: test column ----
            test_debug = {"null_or_missing": 0, "parse_empty": 0,
                          "entity_extracted": 0, "samples": []}
            # ---- DEBUG: coculture technique from raw_json ----
            cct_debug = {"lookup_attempts": 0, "raw_found": 0, "raw_not_found": 0,
                         "key_found": 0, "key_not_found": 0,
                         "node_created": 0, "node_dedup": 0,
                         "extraction_failed": 0, "samples": []}

            # Global edge dedup: shared entities across rows (Organ, etc.) produce duplicate edges
            global_edge_keys = set()

            # ---- Preload public_general_extraction_raw table (for infection_list / cell_factors use) ----
            infection_raw_lookup = {}
            infection_debug = {"loaded": 0, "matched": 0, "found_infection": 0,
                               "extracted_items": 0,
                               "sample_keys": [], "no_match_samples": []}
            cf_debug = {"primary": 0, "coculture": 0,
                        "primary_items": 0, "coculture_items": 0,
                        "samples_checked": 0, "sample_keys": []}
            try:
                cursor.execute("SELECT sample_id, raw_json FROM public_general_extraction_raw")
                for ext_row in cursor.fetchall():
                    sid = ext_row.get("sample_id")
                    if sid:
                        infection_raw_lookup[str(sid)] = ext_row.get("raw_json")
                infection_debug["loaded"] = len(infection_raw_lookup)
                print(f"[INFO] Pre-loaded infection raw data for {len(infection_raw_lookup)} samples "
                      f"from public_general_extraction_raw")
                # Print sample raw_json keys for first 3 entries to verify structure
                sample_count = 0
                for sid, rj in infection_raw_lookup.items():
                    if sample_count >= 3:
                        break
                    try:
                        if isinstance(rj, str):
                            rj_parsed = json.loads(rj)
                        else:
                            rj_parsed = rj
                        if isinstance(rj_parsed, dict):
                            top_keys = sorted(rj_parsed.keys())
                            # Also check for culture_technique_coculture at any level
                            ctc_found = MySQLKnowledgeGraphBuilder._find_key_recursive(rj_parsed, "culture_technique_coculture", 4)
                            print(f"[INFO] raw_json sample {sid}: top_keys={top_keys}, "
                                  f"culture_technique_coculture={'FOUND' if ctc_found is not None else 'NOT FOUND'}")
                        else:
                            print(f"[INFO] raw_json sample {sid}: type={type(rj_parsed).__name__} (not a dict)")
                    except Exception:
                        print(f"[INFO] raw_json sample {sid}: failed to parse")
                    sample_count += 1
            except Exception as e:
                print(f"[WARN] Could not load public_general_extraction_raw: {e}")

            # ---- Preload Material_name_similarity_candidates_enhanced (for MaterialInfo enrichment) ----
            material_similarity_rows = []
            if MATERIAL_SIMILARITY_CONFIG.get("enabled", False):
                sim_table = MATERIAL_SIMILARITY_CONFIG["table"]
                try:
                    cursor.execute(f"SELECT * FROM `{sim_table}`")
                    for sim_row in cursor.fetchall():
                        row_dict = {}
                        for k, v in sim_row.items():
                            if v is not None:
                                row_dict[k] = v
                        if row_dict.get("similar_names", "").strip():
                            material_similarity_rows.append(row_dict)
                    print(f"[INFO] Pre-loaded {len(material_similarity_rows)} rows from {sim_table}")
                except Exception as e:
                    print(f"[WARN] Could not load {sim_table}: {e}")

            # ---- Preload public_GDS_omics → GSE_ID lookup ----
            # sample_id column is comma-separated: "id1,id2,id3"
            gds_lookup = {}     # {sample_id → [GSE_ID, ...]}
            pathway_lookup = {}  # {GSE_ID → [row_dict, ...]}
            gds_debug = {"rows": 0, "parsed_sample_ids": 0, "unique_gse_ids": 0}
            pw_debug = {"rows": 0, "with_factor": 0, "unique_gse_ids": 0}
            try:
                cursor.execute(
                    "SELECT sample_id, GSE_ID FROM public_GDS_omics "
                    "WHERE sample_id IS NOT NULL AND sample_id != ''"
                )
                for gds_row in cursor.fetchall():
                    gds_debug["rows"] += 1
                    raw_ids = str(gds_row.get("sample_id", ""))
                    gse_id = str(gds_row.get("GSE_ID", "")).strip()
                    if raw_ids and gse_id:
                        for sid in raw_ids.split(","):
                            sid = sid.strip()
                            if sid:
                                gds_debug["parsed_sample_ids"] += 1
                                if sid not in gds_lookup:
                                    gds_lookup[sid] = []
                                if gse_id not in gds_lookup[sid]:
                                    gds_lookup[sid].append(gse_id)
                gds_debug["unique_gse_ids"] = len(set(
                    gse for gse_list in gds_lookup.values() for gse in gse_list
                ))
                print(f"[INFO] Pre-loaded GDS_omics: {gds_debug['rows']} rows → "
                      f"{len(gds_lookup)} unique sample_ids → {gds_debug['unique_gse_ids']} unique GSE_IDs")

                # ---- Preload public_search_pathway → GroupInfo data ----
                cursor.execute(
                    "SELECT * FROM public_search_pathway "
                    "WHERE GSE_ID IS NOT NULL AND GSE_ID != '' AND factor IS NOT NULL AND factor != ''"
                )
                for pw_row in cursor.fetchall():
                    pw_debug["rows"] += 1
                    gse_id = str(pw_row.get("GSE_ID", "")).strip()
                    factor_val = str(pw_row.get("factor", "")).strip() if pw_row.get("factor") else ""
                    if gse_id:
                        pw_debug["with_factor"] += 1
                        if gse_id not in pathway_lookup:
                            pathway_lookup[gse_id] = []
                        pathway_lookup[gse_id].append(pw_row)
                pw_debug["unique_gse_ids"] = len(pathway_lookup)
                print(f"[INFO] Pre-loaded search_pathway: {pw_debug['rows']} rows "
                      f"(with factor: {pw_debug['with_factor']}) → {pw_debug['unique_gse_ids']} unique GSE_IDs")
            except Exception as e:
                print(f"[WARN] Could not load GDS_omics or search_pathway: {e}")
                gds_lookup = {}
                pathway_lookup = {}

            # ---- Sub-node config for GroupInfo enrichment (queried on-demand in Step F) ----
            # Use module-level GROUPINFO_SUB_NODE_MAX_ROWS (override via --groupinfo-max-rows CLI arg)

            # order_by / order_cols make the per-comparison truncation deterministic:
            # rows are taken by statistical significance, with a final tie-break on the
            # primary key so that repeated builds of the same snapshot are identical.
            # Without an explicit ORDER BY, "top N" is whatever order the storage engine
            # happens to return, and the exported snapshot is not reproducible.
            GROUPINFO_SUB_NODE_CONFIG = [
                {
                    "node_type": "GroupDEGs", "id_prefix": "gde",
                    "table": "public_rna_seq_differ_genes",
                    "fields": ["data", "group", "symbol", "regulation", "pseudobulk_cell_type"],
                    "name_field": "symbol",
                    "order_by": "adjusted_p_value ASC, ABS(log2fc) DESC, id ASC",
                    "order_cols": ["adjusted_p_value", "log2fc", "id"],
                },
                {
                    "node_type": "IntraClusterDEGs", "id_prefix": "icd",
                    "table": "public_scrna_seq_cluster_differ_genes",
                    "fields": ["data", "group", "symbol", "cluster"],
                    "name_field": "symbol",
                    "order_by": "adjusted_p_value ASC, ABS(log2fc) DESC, id ASC",
                    "order_cols": ["adjusted_p_value", "log2fc", "id"],
                },
                {
                    "node_type": "ClusterMarkers", "id_prefix": "clm",
                    "table": "public_scrna_seq_cluster_marker_genes",
                    "fields": ["data", "group", "symbol", "cluster"],
                    "name_field": "symbol",
                    "order_by": "adjusted_p_value ASC, ABS(log2fc) DESC, id ASC",
                    "order_cols": ["adjusted_p_value", "log2fc", "id"],
                },
                {
                    "node_type": "GSVA", "id_prefix": "gsv",
                    "table": "public_rna_seq_gsva_differ_pathways",
                    "fields": ["data", "group", "term", "regulation", "pseudobulk_cell_type"],
                    "name_field": "term",
                    "order_by": "adjusted_p_value ASC, ABS(log2fc) DESC, id ASC",
                    "order_cols": ["adjusted_p_value", "log2fc", "id"],
                },
                {
                    "node_type": "GSEA", "id_prefix": "gse",
                    "table": "public_rna_seq_gsea_enriched_pathways",
                    "fields": ["data", "group", "terms", "pathway_id"],
                    "name_field": "terms",
                    "order_by": "adjusted_p_value ASC, ABS(nes) DESC, id ASC",
                    "order_cols": ["adjusted_p_value", "nes", "id"],
                },
            ]

            # ---- Gene Annotation table groups for Step G ----
            # Each group maps to human/mouse tables queried by gene symbol.
            # multi_row=True means results are stored as JSON arrays.
            GENE_ANNOTATION_TABLE_GROUPS = [
                {
                    "group_name": "gene_annotation",
                    "human_table": "Gene_Annotation_Human",
                    "mouse_table": "Gene_Annotation_Mouse",
                    "match_column": "symbol",
                    "fields": ["entrez_id", "gene_name", "pfam", "prosite", "drug_id"],
                },
                {
                    # UniProt Gene_Names holds a whitespace-separated alias list
                    # (e.g. "RBM8 RBM8A Y14"), so matching the column for equality
                    # against a single symbol misses most rows: 17.5% of site_human and
                    # 71.0% of disease_human values carry more than one alias.
                    # multi_alias reads the table once and matches per alias token.
                    "group_name": "site",
                    "human_table": "site_human",
                    "mouse_table": "site_mouse",
                    "match_column": "Gene_Names",
                    "multi_alias": True,
                    "fields": ["Entry", "Site", "Binding_site", "Active_site"],
                },
                {
                    "group_name": "disease",
                    "human_table": "disease_human",
                    "mouse_table": "disease_mouse",
                    "match_column": "Gene_Names",
                    "multi_alias": True,
                    "fields": ["Involvement_in_disease", "Mutagenesis"],
                },
                {
                    "group_name": "string",
                    "human_table": "STRING_Combine_Human",
                    "mouse_table": "STRING_Combine_Mouse",
                    "match_column": "GeneID_x",
                    "match_column_alt": "GeneID_y",
                    "fields": ["GeneID_x", "GeneID_y", "GeneSymbol_x", "GeneSymbol_y", "combined_score"],
                },
                {
                    "group_name": "crispick",
                    "human_table": "CRISPick_Combine_Human",
                    "mouse_table": "CRISPick_Combine_Mouse",
                    "match_column": "Input",
                    "fields": ["CRISPR Mechanism", "PAM Policy", "sgRNA Sequence",
                               "sgRNA Context Sequence", "Combined Rank"],
                },
            ]

            for row_idx, row in enumerate(all_rows):
                # ---- Step A: Create Sample core node ----
                sample_node = self._row_to_node_wide(row, table_name, core_type, core_prefix)
                if not sample_node:
                    continue
                nodes.append(sample_node)
                sample_id = sample_node["id"]

                # ---- Step B: Extract entities from regular columns ----
                for col, entry in WIDE_TABLE_ENTITY_MAPPING.items():
                    entity_type, prefix, relation, edge_from = entry[0], entry[1], entry[2], entry[3]
                    is_json = len(entry) > 4 and entry[4] == "json"
                    if is_json:
                        continue  # JSON columns handled in Step C

                    if col not in row or row[col] is None or row[col] == '':
                        continue

                    raw_value = row[col]
                    entity_name = str(raw_value).strip()

                    # Special handling: reference → use doi as ID
                    if entity_type == "Publication":
                        doi = row.get("doi", "")
                        entity_name = str(doi).strip() if doi else entity_name

                    # Gene columns may contain semicolon-separated multi-gene values
                    # (e.g. "RNF31;TP53") — split into individual gene entities.
                    if entity_type == "Gene" and entity_name:
                        entity_names = [g.strip() for g in entity_name.split(";") if g.strip()]
                    else:
                        entity_names = [entity_name] if entity_name else []

                    for single_name in entity_names:
                        # Create or retrieve entity node
                        entity_node, is_new = self._get_or_create_entity(
                            entity_registry, entity_type, prefix, single_name
                        )
                        if is_new:
                            nodes.append(entity_node)

                        # Create edge
                        edge_context = "coculture" if col in COCULTURE_COLUMNS else "primary"
                        if edge_from == "sample":
                            edge = self._make_edge(sample_id, entity_node["id"], relation,
                                                   {"source_column": col, "context": edge_context})
                        elif edge_from == "organ":
                            edge = self._make_edge(None, entity_node["id"], relation,
                                                   {"source_column": col})
                            self._defer_organ_edge = getattr(self, '_defer_organ_edge', [])
                            self._defer_organ_edge.append((col, entity_node["id"], relation))
                        else:
                            edge = self._make_edge(sample_id, entity_node["id"], relation,
                                                   {"source_column": col})

                        if edge_from == "sample" and edge:
                            ek = (edge["source"], edge["target"], edge["relation"])
                            if ek not in global_edge_keys:
                                global_edge_keys.add(ek)
                                edges.append(edge)

                        # Record entity ownership
                        sample_entities[sample_id][entity_type].add(entity_node["id"])
                        if entity_type == "Organ":
                            organ_registry[single_name] = entity_node["id"]

                # ---- Step B.6: Extract compound entity nodes (merged, profile) ----
                for primary_col, config in COMPOUND_ENTITY_CONFIG.items():
                    extraction_type = config["extraction_type"]

                    if extraction_type == "merged":
                        merged_node = self._extract_merged_node(row, config)
                        if merged_node is None:
                            continue

                        # Dedup by content hash via (node_type, node_id) registry key
                        node_id = merged_node["id"]
                        registry_key = (config["node_type"], node_id)
                        if registry_key in entity_registry:
                            entity_node = entity_registry[registry_key]
                        else:
                            entity_registry[registry_key] = merged_node
                            entity_node = merged_node
                            nodes.append(merged_node)

                        # Enrich CoCultureProtocol material_sources from raw_json
                        if config["node_type"] == "CoCultureProtocol":
                            sample_id_raw = str(row.get("sample_id", ""))
                            raw_json_val = infection_raw_lookup.get(sample_id_raw)
                            ms_list = self._extract_coculture_material_sources_from_raw(raw_json_val)
                            if ms_list:
                                existing = entity_node["properties"].get("material_sources")
                                if existing:
                                    # Merge: append unique new sources
                                    existing_list = ([existing] if isinstance(existing, str)
                                                     else (existing if isinstance(existing, list) else [str(existing)]))
                                    for ms in ms_list:
                                        if ms not in existing_list:
                                            existing_list.append(ms)
                                    entity_node["properties"]["material_sources"] = existing_list
                                else:
                                    entity_node["properties"]["material_sources"] = ms_list

                        # For CultureProtocol/CoCultureProtocol: extract components as CellFactor nodes
                        if config["node_type"] in ("CultureProtocol", "CoCultureProtocol"):
                            component_fields = [
                                ("growth_factor_names", "growth_factor"),
                                ("small_molecule_names", "small_molecule"),
                                ("supplement_names", "supplement"),
                                ("media_used", "medium"),
                            ]
                            for prop_key, component_type in component_fields:
                                items = entity_node["properties"].get(prop_key, [])
                                if not items:
                                    continue
                                for item_name in items:
                                    if not item_name or not str(item_name).strip():
                                        continue
                                    cf_node, cf_is_new = self._get_or_create_entity(
                                        entity_registry, "CellFactor", "cf", str(item_name).strip()
                                    )
                                    if cf_is_new:
                                        cf_node["properties"]["component_type"] = component_type
                                        nodes.append(cf_node)
                                    # Edge: Protocol -[:USES_COMPONENT]-> CellFactor
                                    cf_edge = self._make_edge(entity_node["id"], cf_node["id"],
                                                              "USES_COMPONENT",
                                                              {"component_type": component_type,
                                                               "source": "protocol_parsing"})
                                    if cf_edge:
                                        ek = (cf_edge["source"], cf_edge["target"], cf_edge["relation"])
                                        if ek not in global_edge_keys:
                                            global_edge_keys.add(ek)
                                            edges.append(cf_edge)

                        # ---- Extract MaterialInfo nodes from material_sources ----
                        if (MATERIAL_INFO_CONFIG
                                and config["node_type"] in MATERIAL_INFO_CONFIG.get("parent_node_types", [])):
                            src_prop = MATERIAL_INFO_CONFIG.get("source_property", "material_sources")
                            ms_value = entity_node["properties"].get(src_prop)
                            if ms_value:
                                material_items = self._parse_material_sources(ms_value)
                                # Take only the first N items per protocol (configurable)
                                if MATERIAL_INFO_MAX_ITEMS is not None and len(material_items) > MATERIAL_INFO_MAX_ITEMS:
                                    print(f"  [INFO] limiting {len(material_items)} material items → {MATERIAL_INFO_MAX_ITEMS} "
                                          f"for {entity_node['id']}")
                                    material_items = material_items[:MATERIAL_INFO_MAX_ITEMS]
                                json_props = MATERIAL_INFO_CONFIG.get("json_properties", [])
                                enrich_fields = MATERIAL_INFO_CONFIG.get("enrichment_fields", [])
                                name_fallback = MATERIAL_INFO_CONFIG.get("name_fallback_field", "material_name")

                                for item in material_items:
                                    if not isinstance(item, dict):
                                        continue
                                    mat_name_raw = str(item.get("material_name", "")).strip()
                                    if not mat_name_raw:
                                        continue

                                    # Enrich from similarity table
                                    enriched = self._match_material_similarity(
                                        mat_name_raw, material_similarity_rows
                                    ) or {}

                                    # Build dedup dict for content-hash
                                    dedup_dict = {}
                                    for p in json_props:
                                        dedup_dict[p] = (str(item.get(p, "")).strip()
                                                         if item.get(p) is not None else "")
                                    for f in enrich_fields:
                                        dedup_dict[f] = enriched.get(f, "")

                                    content_hash = self._content_hash(dedup_dict)
                                    mat_node_id = f"{MATERIAL_INFO_CONFIG['id_prefix']}_{content_hash}"

                                    # Dedup MaterialInfo node by content hash
                                    mat_registry_key = ("MaterialInfo", mat_node_id)
                                    if mat_registry_key in entity_registry:
                                        mat_node = entity_registry[mat_registry_key]
                                    else:
                                        mat_node = {
                                            "id": mat_node_id,
                                            "type": "MaterialInfo",
                                            "properties": {
                                                "name": enriched.get("standard_name")
                                                        or item.get(name_fallback, ""),
                                            },
                                            "_provenance": {
                                                "source_type": "material_extraction",
                                                "parent_node_type": config["node_type"],
                                                "extracted_at": datetime.now().isoformat()
                                            }
                                        }
                                        # Populate json_properties
                                        for p in json_props:
                                            val = item.get(p)
                                            if val is not None and str(val).strip():
                                                mat_node["properties"][p] = str(val).strip()
                                        # Populate enrichment fields
                                        for f in enrich_fields:
                                            if f in enriched and enriched[f]:
                                                mat_node["properties"][f] = enriched[f]

                                        entity_registry[mat_registry_key] = mat_node
                                        nodes.append(mat_node)

                                    # Edge: Protocol -[:USES_MATERIAL]-> MaterialInfo
                                    mat_edge = self._make_edge(
                                        entity_node["id"], mat_node_id,
                                        MATERIAL_INFO_CONFIG["relation"],
                                        {"source": "material_sources_parsing",
                                         "protocol_type": config["node_type"]}
                                    )
                                    if mat_edge:
                                        ek = (mat_edge["source"], mat_edge["target"], mat_edge["relation"])
                                        if ek not in global_edge_keys:
                                            global_edge_keys.add(ek)
                                            edges.append(mat_edge)

                        # Create edge from Sample
                        if config["edge_from"] == "sample":
                            edge = self._make_edge(sample_id, entity_node["id"], config["relation"],
                                                   {"source_columns": config["source_columns"]})
                            if edge:
                                ek = (edge["source"], edge["target"], edge["relation"])
                                if ek not in global_edge_keys:
                                    global_edge_keys.add(ek)
                                    edges.append(edge)
                            sample_entities[sample_id][config["node_type"]].add(entity_node["id"])

                    elif extraction_type == "profile":
                        merged_node = self._extract_merged_node(row, config)
                        if merged_node is None:
                            continue

                        node_id = merged_node["id"]
                        registry_key = (config["node_type"], node_id)
                        if registry_key in entity_registry:
                            entity_node = entity_registry[registry_key]
                        else:
                            entity_registry[registry_key] = merged_node
                            entity_node = merged_node
                            nodes.append(merged_node)

                        # Create edge based on config.edge_from
                        if config["edge_from"] == "sample":
                            edge = self._make_edge(sample_id, entity_node["id"], config["relation"],
                                                   {"source_columns": config["source_columns"]})
                            if edge:
                                ek = (edge["source"], edge["target"], edge["relation"])
                                if ek not in global_edge_keys:
                                    global_edge_keys.add(ek)
                                    edges.append(edge)
                        sample_entities[sample_id][config["node_type"]].add(entity_node["id"])

                # ---- Step C: Extract entities from JSON columns ----
                RAW_JSON_COLUMNS = {"infection_list", "cell_factors", "coculture_cell_factors"}
                # cell_factors extraction cache: sample_id_raw -> (primary_list, coculture_list)
                cell_factors_cache = {}

                for col, (entity_type, prefix, relation, edge_from, *rest) in WIDE_TABLE_ENTITY_MAPPING.items():
                    is_json = len(rest) > 0 and rest[0] == "json"
                    if not is_json:
                        continue
                    if col not in row or row[col] is None:
                        # For the following columns, the data source is in raw_json, not in the current row
                        if col in RAW_JSON_COLUMNS:
                            pass
                        else:
                            # DEBUG: techologies column missing or NULL
                            if col == "techologies":
                                tech_debug["col_missing_or_null"] += 1
                                if row_idx == 0:
                                    tech_debug["first_row_cols"] = sorted(row.keys())
                                    tech_debug["col_in_first_row"] = col in row
                            # DEBUG: test column missing or NULL
                            if col == "test":
                                test_debug["null_or_missing"] += 1
                            continue

                    # infection_list gets data from public_general_extraction_raw table
                    if col == "infection_list":
                        sample_id_raw = str(row.get("sample_id", ""))
                        raw_json_val = infection_raw_lookup.get(sample_id_raw)
                        infection_debug["matched"] += 1

                        if raw_json_val is None:
                            if len(infection_debug["no_match_samples"]) < 5:
                                infection_debug["no_match_samples"].append(sample_id_raw)
                            continue
                        if isinstance(raw_json_val, str):
                            try:
                                raw_json_val = json.loads(raw_json_val)
                            except json.JSONDecodeError:
                                continue

                        # Traverse cultures[].story.infection_list, aggregate infection data from all cultures
                        json_data = self._extract_infection_data(raw_json_val, max_depth=4)
                        if json_data is None:
                            continue

                        infection_debug["found_infection"] += 1
                        infection_debug["extracted_items"] += len(json_data)
                        if len(infection_debug["sample_keys"]) < 3:
                            top_keys = (list(raw_json_val.keys()) if isinstance(raw_json_val, dict)
                                        else f"type={type(raw_json_val).__name__}")
                            infection_debug["sample_keys"].append({
                                "sample_id": sample_id_raw,
                                "raw_json_type": type(raw_json_val).__name__,
                                "top_keys": str(top_keys)[:300],
                                "infection_data_type": type(json_data).__name__,
                                "infection_data_len": len(json_data),
                                "infection_data_preview": str(json_data)[:500],
                            })

                    # cell_factors / coculture_cell_factors extracted from raw_json, split by if_co_culture
                    elif col in ("cell_factors", "coculture_cell_factors"):
                        sample_id_raw = str(row.get("sample_id", ""))
                        if sample_id_raw not in cell_factors_cache:
                            raw_json_val = infection_raw_lookup.get(sample_id_raw)
                            if raw_json_val is None:
                                cell_factors_cache[sample_id_raw] = ([], [])
                            else:
                                if isinstance(raw_json_val, str):
                                    try:
                                        raw_json_val = json.loads(raw_json_val)
                                    except json.JSONDecodeError:
                                        cell_factors_cache[sample_id_raw] = ([], [])
                                        continue
                                # Collect diagnostic info for first 3 samples
                                extract_debug = {} if len(cf_debug["sample_keys"]) < 3 else None
                                cell_factors_cache[sample_id_raw] = \
                                    self._extract_cell_factors_from_raw(raw_json_val, max_depth=4,
                                                                        debug_info=extract_debug)
                                if extract_debug:
                                    extract_debug["sample_id"] = sample_id_raw
                                    extract_debug["top_keys"] = (list(raw_json_val.keys())[:200]
                                        if isinstance(raw_json_val, dict)
                                        else f"type={type(raw_json_val).__name__}")
                                    cf_debug["sample_keys"].append(extract_debug)

                        primary_list, coculture_list = cell_factors_cache[sample_id_raw]

                        if col == "cell_factors":
                            json_data = primary_list
                            cf_debug["samples_checked"] += 1
                            if primary_list:
                                cf_debug["primary"] += 1
                                cf_debug["primary_items"] += len(primary_list)
                        else:  # coculture_cell_factors
                            json_data = coculture_list
                            if coculture_list:
                                cf_debug["coculture"] += 1
                                cf_debug["coculture_items"] += len(coculture_list)

                        if not json_data:
                            continue

                    else:
                        json_data = row[col]

                    items = self._parse_json_column(json_data)
                    if not items:
                        # DEBUG: record techologies empty parse
                        if col == "techologies":
                            tech_debug["total_nonnull"] += 1
                            tech_debug["parsed_empty"] += 1
                        # DEBUG: record test empty parse
                        if col == "test":
                            test_debug["parse_empty"] += 1
                            if len(test_debug["samples"]) < 3:
                                test_debug["samples"].append({
                                    "reason": "parse returned empty",
                                    "raw_type": type(json_data).__name__,
                                    "raw_preview": str(json_data)[:200],
                                })
                        continue

                    # DEBUG: record techologies parse status
                    if col == "techologies":
                        tech_debug["total_nonnull"] += 1
                        tech_debug["parsed_ok"] += 1
                        if len(tech_debug["samples"]) < 3:
                            tech_debug["samples"].append({
                                "row_idx": row_idx,
                                "raw_type": type(json_data).__name__,
                                "raw_preview": str(json_data)[:200],
                                "items_count": len(items),
                                "item_preview": [str(it)[:120] for it in items[:3]],
                            })
                        # techologies has nested objects; use recursive flatten to extract leaf strings
                        items = self._flatten_json_values(json_data)

                    # test column may have nested structures; flatten to leaf strings
                    if col == "test":
                        items = self._flatten_json_values(json_data)

                    edge_context = "coculture" if col in COCULTURE_COLUMNS else "primary"

                    # omics_id 列不在此处理。Omics 层需要跨样本聚合（同一数据集被多个
                    # 样本以不同 omics_subtype 引用）、需要过滤 "not specified" 之类的
                    # 占位值、还需要把 accession/omics_datasets 列里 omics_id 列漏收的
                    # 数据集补回来——这些都不是逐行逐项的通用循环能表达的。
                    # 统一由 _build_omics_layer() 在行循环结束后整层生成。
                    if col == "omics_id":
                        continue

                    for item in items:
                        entity_name = self._extract_entity_name(item, col)
                        if not entity_name:
                            # DEBUG: techologies entity name extraction failed
                            if col == "techologies" and len(tech_debug["samples"]) < 5:
                                tech_debug["samples"].append({
                                    "row_idx": row_idx, "fail_reason": "entity_name is None",
                                    "item_type": type(item).__name__,
                                    "item_preview": str(item)[:200],
                                })
                            continue
                        if col == "techologies":
                            tech_debug["entity_extracted"] += 1
                        if col == "test":
                            test_debug["entity_extracted"] += 1

                        # Create or retrieve entity node
                        entity_node, is_new = self._get_or_create_entity(
                            entity_registry, entity_type, prefix, entity_name
                        )
                        if is_new:
                            # Extract additional properties from JSON entry
                            if isinstance(item, dict):
                                for k, v in item.items():
                                    if k not in ("name", "Name", "phenotype") and v is not None:
                                        entity_node["properties"][k] = v
                            nodes.append(entity_node)

                        # Create edge
                        if edge_from == "sample":
                            edge = self._make_edge(sample_id, entity_node["id"], relation,
                                                   {"source_column": col,
                                                    "context": edge_context})
                            if edge:
                                ek = (edge["source"], edge["target"], edge["relation"])
                                if ek not in global_edge_keys:
                                    global_edge_keys.add(ek)
                                    edges.append(edge)
                        elif edge_from == "organ":
                            self._defer_organ_edge = getattr(self, '_defer_organ_edge', [])
                            self._defer_organ_edge.append((col, entity_node["id"], relation,
                                                            {"source_column": col}))

                        sample_entities[sample_id][entity_type].add(entity_node["id"])

                        # drug_screening entries pair a drug with the disease it was
                        # screened against, in the source JSON itself. Record the pairing
                        # so the graph can carry an exact edge instead of a within-sample
                        # cartesian product over all drugs x all disease models.
                        if col == "drug_screening" and isinstance(item, dict):
                            _dis = str(item.get("disease", "") or "").strip()
                            if _dis and _dis.lower() not in ("not specified", "none", "na", "n/a"):
                                if not hasattr(self, "_explicit_drug_disease"):
                                    self._explicit_drug_disease = []
                                self._explicit_drug_disease.append({
                                    "sample_id": sample_id,
                                    "drug_node_id": entity_node["id"],
                                    "disease_name": _dis,
                                    "drug_response": str(item.get("drug_response", "") or "").strip(),
                                })

                # ---- Step C.5: Extract JSON-structured entity nodes ----
                for col, config in COMPOUND_ENTITY_CONFIG.items():
                    if config["extraction_type"] != "json_structured":
                        continue
                    if col not in row or row[col] is None or row[col] == '':
                        if col in js_debug:
                            js_debug[col]["null_or_missing"] += 1
                        continue

                    config_with_source = dict(config)
                    config_with_source["_source_column"] = col
                    structured_node = self._extract_json_structured_node(row[col], config_with_source)
                    if structured_node is None:
                        if col in js_debug:
                            # Try to determine WHY extraction failed
                            val = row[col]
                            if isinstance(val, str):
                                try:
                                    parsed = json.loads(val)
                                except json.JSONDecodeError:
                                    js_debug[col]["parse_failed"] += 1
                                    if len(js_debug[col]["samples"]) < 3:
                                        js_debug[col]["samples"].append({
                                            "reason": "JSON decode error",
                                            "raw_preview": val[:200],
                                        })
                                    continue
                            elif isinstance(val, (dict, list)):
                                parsed = val
                            else:
                                js_debug[col]["parse_failed"] += 1
                                if len(js_debug[col]["samples"]) < 3:
                                    js_debug[col]["samples"].append({
                                        "reason": f"unexpected type: {type(val).__name__}",
                                        "raw_preview": str(val)[:200],
                                    })
                                continue

                            if not parsed:
                                js_debug[col]["empty_data"] += 1
                                if len(js_debug[col]["samples"]) < 3:
                                    js_debug[col]["samples"].append({
                                        "reason": "empty data (after parse)",
                                        "parsed_type": type(parsed).__name__,
                                        "raw_preview": str(row[col])[:200],
                                    })
                            elif not isinstance(parsed, dict):
                                js_debug[col]["non_dict_data"] += 1
                                if len(js_debug[col]["samples"]) < 3:
                                    js_debug[col]["samples"].append({
                                        "reason": f"non-dict type: {type(parsed).__name__}",
                                        "parsed_type": type(parsed).__name__,
                                        "raw_preview": str(row[col])[:200],
                                        "parsed_preview": str(parsed)[:200],
                                    })
                            else:
                                js_debug[col]["empty_data"] += 1  # dict but empty
                        continue

                    if col in js_debug:
                        js_debug[col]["extracted_ok"] += 1

                    node_id = structured_node["id"]
                    registry_key = (config["node_type"], node_id)
                    if registry_key in entity_registry:
                        entity_node = entity_registry[registry_key]
                    else:
                        entity_registry[registry_key] = structured_node
                        entity_node = structured_node
                        nodes.append(structured_node)

                    if config["edge_from"] == "sample":
                        edge = self._make_edge(sample_id, entity_node["id"], config["relation"],
                                               {"source_column": col})
                        if edge:
                            ek = (edge["source"], edge["target"], edge["relation"])
                            if ek not in global_edge_keys:
                                global_edge_keys.add(ek)
                                edges.append(edge)
                        sample_entities[sample_id][config["node_type"]].add(entity_node["id"])

                # ---- Step C.5.5: Extract CoCultureTechnique from raw_json ----
                # Data lives at 'culture_technique_coculture' key in raw_json, not in main table column
                sample_id_raw = str(row.get("sample_id", ""))
                cct_debug["lookup_attempts"] += 1
                raw_json_val = infection_raw_lookup.get(sample_id_raw)
                if raw_json_val is not None:
                    cct_debug["raw_found"] += 1
                    cct_data = self._extract_coculture_technique_from_raw(raw_json_val)
                    if cct_data is not None:
                        cct_debug["key_found"] += 1
                        if len(cct_debug["samples"]) < 3:
                            cct_debug["samples"].append({
                                "sample_id": sample_id_raw,
                                "cct_data_type": type(cct_data).__name__,
                                "cct_data_len": len(cct_data) if isinstance(cct_data, list) else 1,
                                "cct_data_preview": str(cct_data)[:300],
                            })
                        # cct_data is a list of technique strings from cultures[] entries
                        if not isinstance(cct_data, list):
                            cct_data = [cct_data]
                        cct_config = {
                            "node_type": "CoCultureTechnique", "id_prefix": "cct",
                            "_source_column": "culture_technique_coculture",
                        }
                        for technique_val in cct_data:
                            if not technique_val or not str(technique_val).strip():
                                continue
                            cct_node = self._extract_json_structured_node(technique_val, cct_config)
                            if cct_node is not None:
                                node_id = cct_node["id"]
                                registry_key = ("CoCultureTechnique", node_id)
                                if registry_key in entity_registry:
                                    cct_entity = entity_registry[registry_key]
                                    cct_debug["node_dedup"] += 1
                                else:
                                    entity_registry[registry_key] = cct_node
                                    cct_entity = cct_node
                                    nodes.append(cct_node)
                                    cct_debug["node_created"] += 1
                                edge = self._make_edge(sample_id, cct_entity["id"],
                                                       "USES_COCULTURE_TECHNIQUE",
                                                       {"source_column": "culture_technique_coculture"})
                                if edge:
                                    ek = (edge["source"], edge["target"], edge["relation"])
                                    if ek not in global_edge_keys:
                                        global_edge_keys.add(ek)
                                        edges.append(edge)
                                sample_entities[sample_id]["CoCultureTechnique"].add(cct_entity["id"])
                            else:
                                cct_debug["extraction_failed"] += 1
                                if len(cct_debug["samples"]) < 5:
                                    cct_debug["samples"].append({
                                        "sample_id": sample_id_raw,
                                        "fail_reason": "_extract_json_structured_node returned None",
                                        "technique_val_type": type(technique_val).__name__,
                                        "technique_val_preview": str(technique_val)[:200],
                                    })
                    else:
                        cct_debug["key_not_found"] += 1
                else:
                    cct_debug["raw_not_found"] += 1
                    if len(cct_debug["samples"]) < 3 and cct_debug["raw_not_found"] <= 3:
                        cct_debug["samples"].append({
                            "sample_id": sample_id_raw,
                            "fail_reason": "raw_json not found in infection_raw_lookup",
                        })

                # ---- Step C.6: Extract Platform node (Omics → Platform two-level edge) ----
                platform_config = COMPOUND_ENTITY_CONFIG.get("platform")
                if platform_config:
                    platform_val = row.get("platform")
                    if platform_val is not None and str(platform_val).strip():
                        platform_node = self._extract_platform_node(str(platform_val))
                        if platform_node:
                            node_id = platform_node["id"]
                            registry_key = ("Platform", node_id)
                            if registry_key in entity_registry:
                                entity_node = entity_registry[registry_key]
                            else:
                                entity_registry[registry_key] = platform_node
                                entity_node = platform_node
                                nodes.append(platform_node)

                            # 注：Omics → Platform 边不再在此延迟生成。样本的 platform 列
                            # 只描述「该样本被重分析的那个数据集」的平台，而一个样本可引用
                            # 多个数据集；旧写法在 Step D.5 里用 list(ids)[0] 随机挑一个
                            # Omics 节点并把样本级 platform 写到共享节点上。现改为在建
                            # Omics 节点时由该数据集自己的 omics_subtype 派生（见 Step C.4）。
                            # Platform 节点本身仍在此登记，保持取值域不变。
                            sample_entities[sample_id]["Platform"].add(entity_node["id"])

                # ---- Step D: Resolve deferred Organ→System edges ----
                deferred_organ = getattr(self, '_defer_organ_edge', [])
                organ_id = organ_registry.get(
                    str(row.get("organ", "")).strip(), None)
                if organ_id:
                    for item in deferred_organ:
                        if len(item) == 4:
                            col, tgt_id, rel, props = item
                        else:
                            col, tgt_id, rel = item
                            props = {"source_column": col}
                        edge = self._make_edge(organ_id, tgt_id, rel, props)
                        if edge:
                            ek = (edge["source"], edge["target"], edge["relation"])
                            if ek not in global_edge_keys:
                                global_edge_keys.add(ek)
                                edges.append(edge)
                self._defer_organ_edge = []

                # ---- Step D.5: (已停用) 旧的 Omics → Platform 延迟解析 ----
                # 2026-08-03 移除。原实现有两个缺陷：
                #   1) list(ids)[0] 在样本对应多个数据集时随机挑一个 Omics 节点；
                #   2) 把样本级 platform 写到被多样本共享的数据集节点上，后写覆盖先写。
                # 现在 Omics → Platform 在 Step C.4 建 Omics 节点时按该数据集自己的
                # omics_subtype 生成，见 OMICS_SUBTYPE_TO_PLATFORM。
                self._defer_omics_edge = []

                # ---- Enrich sgrna onto Gene node ----
                sgrna_val = row.get("sgrna")
                if sgrna_val is not None and str(sgrna_val).strip():
                    gene_name_raw = str(row.get("gene_name", "")).strip() if row.get("gene_name") else None
                    if gene_name_raw:
                        # gene_name may be semicolon-separated (e.g. "RNF31;TP53")
                        individual_genes = [g.strip() for g in gene_name_raw.split(";") if g.strip()]
                        for single_gene in individual_genes:
                            gene_key = ("Gene", single_gene.lower())
                            if gene_key in entity_registry:
                                gene_node = entity_registry[gene_key]
                                gene_node["properties"]["sgrna"] = str(sgrna_val).strip()
                                gene_node["properties"]["editing_method"] = gene_node["properties"].get(
                                    "editing_method", "CRISPR-Cas9")

                # ---- Enrich organism/species onto Gene node ----
                gene_name_val = str(row.get("gene_name", "")).strip() if row.get("gene_name") else None
                organism_val = str(row.get("organism", "")).strip() if row.get("organism") else None
                if gene_name_val and organism_val:
                    # gene_name may be semicolon-separated (e.g. "RNF31;TP53")
                    individual_genes = [g.strip() for g in gene_name_val.split(";") if g.strip()]
                    for single_gene in individual_genes:
                        gene_key = ("Gene", single_gene.lower())
                        if gene_key in entity_registry:
                            gene_node = entity_registry[gene_key]
                            organisms = gene_node["properties"].get("organisms", [])
                            if organism_val not in organisms:
                                organisms.append(organism_val)
                                gene_node["properties"]["organisms"] = organisms
                            species = MySQLKnowledgeGraphBuilder._detect_species(organism_val)
                            if species:
                                species_list = gene_node["properties"].get("species", [])
                                if species not in species_list:
                                    species_list.append(species)
                                    gene_node["properties"]["species"] = sorted(species_list)

                # ---- (已移除) 把样本级 accession 富化到 Omics 节点 ----
                # 2026-08-03 移除。Omics 节点现按登记号建，omics_id 本身即登记号，
                # 该属性已冗余；且节点被多样本共享，按样本写会后写覆盖先写——正是
                # 「GEO 节点 omics_id=GSE277637 而 accession=GSE277544」的来源。
                # 样本自己的数据集现由 HAS_OMICS.role='analyzed' 标识。

                # ---- Extract GroupInfo from GDS_omics → search_pathway lookup chain ----
                sample_id_raw = str(row.get("sample_id", ""))
                gse_ids = gds_lookup.get(sample_id_raw, [])
                for gse_id in gse_ids:
                    pw_rows = pathway_lookup.get(gse_id, [])
                    for pw_row in pw_rows:
                        factor_val = str(pw_row.get("factor", "")).strip() if pw_row.get("factor") else None
                        if not factor_val:
                            continue

                        # Collect all relevant fields for content-hash dedup
                        groupinfo_fields = [
                            "Data_Type", "Organism", "group", "condition", "additional_condition",
                            "factor", "organ_control", "organ_condition",
                            "organ_system_control", "organ_system_condition",
                            "comparison_control", "comparison_condition",
                            "model_control", "model_condition",
                            "time_control", "time_condition",
                            "source_control", "source_condition",
                            "category", "path", "GSE_ID", "cell_type"
                        ]
                        merged_data = {}
                        for field in groupinfo_fields:
                            val = pw_row.get(field)
                            if val is not None and str(val).strip():
                                merged_data[field] = str(val).strip()

                        if not merged_data.get("factor"):
                            continue

                        content_hash = self._content_hash(merged_data)
                        node_id = f"gin_{content_hash}"
                        registry_key = ("GroupInfo", node_id)

                        if registry_key in entity_registry:
                            entity_node = entity_registry[registry_key]
                        else:
                            entity_node = {
                                "id": node_id,
                                "type": "GroupInfo",
                                "properties": {
                                    "name": merged_data.get("factor", ""),
                                    **merged_data
                                },
                                "_provenance": {
                                    "source_type": "cross_table_lookup",
                                    "source_tables": ["public_GDS_omics", "public_search_pathway"],
                                    "gse_id": gse_id,
                                    "lookup_key": sample_id_raw,
                                    "extracted_at": datetime.now().isoformat()
                                }
                            }
                            entity_registry[registry_key] = entity_node
                            nodes.append(entity_node)

                        # Create edge: Sample -[:HAS_GROUP_INFO]-> GroupInfo
                        edge = self._make_edge(sample_id, entity_node["id"], "HAS_GROUP_INFO",
                                               {"source": "public_GDS_omics→public_search_pathway",
                                                "gse_id": gse_id,
                                                "factor": merged_data.get("factor", "")})
                        if edge:
                            ek = (edge["source"], edge["target"], edge["relation"])
                            if ek not in global_edge_keys:
                                global_edge_keys.add(ek)
                                edges.append(edge)

                        sample_entities[sample_id]["GroupInfo"].add(entity_node["id"])

                if (row_idx + 1) % 1000 == 0:
                    print(f"  ... processed {row_idx + 1}/{len(all_rows)} rows, "
                          f"{len(nodes)} nodes, {len(edges)} edges")

            # ---- DEBUG: print techologies exploration results ----
            print(f"\n[DEBUG] techologies column analysis:")
            print(f"  column in row[0] keys:   {tech_debug['col_in_first_row']}")
            if tech_debug['first_row_cols']:
                # Filter column names containing 'tech' for location assistance
                tech_like = [c for c in tech_debug['first_row_cols'] if 'tech' in c.lower()]
                print(f"  columns matching 'tech':  {tech_like}")
            print(f"  col missing or NULL:     {tech_debug['col_missing_or_null']}")
            print(f"  non-null & parsed OK:    {tech_debug['parsed_ok']}")
            print(f"  non-null & parsed empty: {tech_debug['parsed_empty']}")
            print(f"  entity names extracted:  {tech_debug['entity_extracted']}")
            if tech_debug["samples"]:
                print(f"  sample raw values (first {len(tech_debug['samples'])}):")
                for s in tech_debug["samples"]:
                    for k, v in s.items():
                        print(f"    {k}: {v}")
            print()

            # ---- DEBUG: infection_list exploration results ----
            print(f"[DEBUG] infection_list column analysis:")
            print(f"  raw samples loaded:      {infection_debug['loaded']}")
            print(f"  samples checked:         {infection_debug['matched']}")
            print(f"  infection data found:    {infection_debug['found_infection']} "
                  f"(total items extracted: {infection_debug['extracted_items']})")
            if infection_debug["no_match_samples"]:
                print(f"  no-match sample_ids (first 5): {infection_debug['no_match_samples'][:5]}")
            if infection_debug["sample_keys"]:
                print(f"  sample raw_json structure (first {len(infection_debug['sample_keys'])}):")
                for s in infection_debug["sample_keys"]:
                    for k, v in s.items():
                        print(f"    {k}: {v}")
            print()

            # ---- DEBUG: cell_factors exploration results ----
            print(f"[DEBUG] cell_factors / coculture_cell_factors analysis (from raw_json):")
            print(f"  samples checked:           {cf_debug['samples_checked']}")
            print(f"  samples with primary CF:   {cf_debug['primary']} "
                  f"(total items: {cf_debug['primary_items']})")
            print(f"  samples with coculture CF: {cf_debug['coculture']} "
                  f"(total items: {cf_debug['coculture_items']})")
            if cf_debug["sample_keys"]:
                print(f"  diagnostic info (first {len(cf_debug['sample_keys'])} samples):")
                for s in cf_debug["sample_keys"]:
                    for k, v in s.items():
                        print(f"    {k}: {v}")
            print()

            # ---- DEBUG: json_structured columns analysis ----
            print(f"[DEBUG] json_structured column analysis (CultureCondition, CoCultureTechnique):")
            for js_col in ["culture_condition", "coculture_technique"]:
                dbg = js_debug.get(js_col, {})
                print(f"  [{js_col}]:")
                print(f"    null_or_missing:   {dbg.get('null_or_missing', 0)}")
                print(f"    parse_failed:      {dbg.get('parse_failed', 0)}")
                print(f"    non_dict_data:     {dbg.get('non_dict_data', 0)}")
                print(f"    empty_data:        {dbg.get('empty_data', 0)}")
                print(f"    extracted_ok:      {dbg.get('extracted_ok', 0)}")
                if dbg.get("samples"):
                    print(f"    sample failures (first {len(dbg['samples'])}):")
                    for s in dbg["samples"]:
                        print(f"      reason={s.get('reason')}, parsed_type={s.get('parsed_type', '?')}")
                        print(f"      raw: {s.get('raw_preview', '?')[:150]}")
            print()

            # ---- DEBUG: coculture technique from raw_json ----
            print(f"[DEBUG] CoCultureTechnique extraction from raw_json:")
            print(f"  lookup_attempts:     {cct_debug.get('lookup_attempts', 0)}")
            print(f"  raw_found:           {cct_debug.get('raw_found', 0)}")
            print(f"  raw_not_found:       {cct_debug.get('raw_not_found', 0)}")
            print(f"  key_found:           {cct_debug.get('key_found', 0)}")
            print(f"  key_not_found:       {cct_debug.get('key_not_found', 0)}")
            print(f"  node_created:        {cct_debug.get('node_created', 0)}")
            print(f"  node_dedup:          {cct_debug.get('node_dedup', 0)}")
            print(f"  extraction_failed:   {cct_debug.get('extraction_failed', 0)}")
            if cct_debug.get("samples"):
                print(f"  sample details (first {len(cct_debug['samples'])}):")
                for s in cct_debug["samples"]:
                    for k, v in s.items():
                        print(f"    {k}: {v}")
            print()

            # ---- DEBUG: test column analysis ----
            print(f"[DEBUG] test column analysis:")
            print(f"  null_or_missing:     {test_debug.get('null_or_missing', 0)}")
            print(f"  parse_empty:         {test_debug.get('parse_empty', 0)}")
            print(f"  entity_extracted:    {test_debug.get('entity_extracted', 0)}")
            if test_debug.get("samples"):
                print(f"  sample failures (first {len(test_debug['samples'])}):")
                for s in test_debug["samples"]:
                    for k, v in s.items():
                        print(f"    {k}: {v}")
            print()

            # ---- Step D.9: Omics 层整层生成（2026-08-03 修复）----
            print("[INFO] Building Omics layer (keyed on dataset accession)...")
            _om_nodes, _om_edges = self._build_omics_layer(
                all_rows, entity_registry, sample_entities)
            nodes.extend(_om_nodes)
            for e in _om_edges:
                ek = (e["source"], e["target"], e["relation"])
                if ek not in global_edge_keys:
                    global_edge_keys.add(ek)
                    edges.append(e)
            print("[INFO] Omics layer: %d nodes, %d edges"
                  % (len(_om_nodes), len(_om_edges)))

            # ---- Step E: Explicit source pairings, then co-occurrence inference ----
            print(f"[INFO] Building explicit drug-disease edges from source pairings...")
            explicit_edges = self.build_explicit_drug_disease_edges(entity_registry, nodes)
            for e in explicit_edges:
                ek = (e["source"], e["target"], e["relation"])
                if ek not in global_edge_keys:
                    global_edge_keys.add(ek)
                    edges.append(e)
            print(f"[INFO] Added {len(explicit_edges)} explicit edges")

            print(f"[INFO] Generating inferred relationships...")
            inferred_edges = self._infer_relationships(
                sample_entities, entity_registry, nodes, edges)
            edges.extend(inferred_edges)
            print(f"[INFO] Added {len(inferred_edges)} inferred edges")

            # ---- Step F: Create sub-nodes from GroupInfo (DEGs, markers, pathways) ----
            # Collect unique GSE_IDs + organisms from GroupInfo, then query each
            # sub-table on-demand with only the needed columns.
            print(f"[INFO] Creating sub-nodes from GroupInfo via GSE_ID cross-reference...")
            sub_node_counts = {}
            sub_edge_counts = {}

            # {gse_id: {"ginodes": [(node_id, organism, group), ...], "organisms": set(),
            #           "groups": set()}}
            # The GroupInfo's own `group` is retained so that Step F can attach each
            # sub-node to the comparison it actually came from, instead of to every
            # GroupInfo sharing the GSE_ID.
            gse_info = {}
            for (etype, node_id), ginode in entity_registry.items():
                if etype != "GroupInfo":
                    continue
                gse_id = str(ginode["properties"].get("GSE_ID", "")).strip()
                if not gse_id:
                    continue
                organism = str(ginode["properties"].get("Organism", "")).strip()
                group_name = str(ginode["properties"].get("group", "")).strip()
                if gse_id not in gse_info:
                    gse_info[gse_id] = {"ginodes": [], "organisms": set(), "groups": set()}
                gse_info[gse_id]["ginodes"].append((node_id, organism, group_name))
                if organism:
                    gse_info[gse_id]["organisms"].add(organism)
                if group_name:
                    gse_info[gse_id]["groups"].add(group_name)

            if not gse_info:
                print(f"  No GroupInfo nodes with GSE_ID found, skipping sub-node creation.")
            else:
                all_gse_ids = sorted(gse_info.keys())
                total_ginodes = sum(len(v["ginodes"]) for v in gse_info.values())
                print(f"  Collected {len(all_gse_ids)} unique GSE_IDs from {total_ginodes} GroupInfo nodes")

                BATCH_SIZE = 256

                # Relation name per sub-node type (Step F edges: GroupInfo → sub-node)
                _SUB_RELATION_MAP = {
                    "GroupDEGs": "HAS_DEG",
                    "IntraClusterDEGs": "HAS_INTRACLUSTER_DEG",
                    "ClusterMarkers": "HAS_CLUSTER_MARKER",
                    "GSVA": "HAS_GSVA_PATHWAY",
                    "GSEA": "HAS_GSEA_PATHWAY",
                }

                for cfg in GROUPINFO_SUB_NODE_CONFIG:
                    nt = cfg["node_type"]
                    table = cfg["table"]
                    fields = cfg["fields"]
                    name_field = cfg["name_field"]
                    nt_new_nodes = 0
                    nt_new_edges = 0

                    # ---- DEBUG for specific node types ----
                    is_debug = (nt == "GSEA")
                    if is_debug:
                        print(f"  [{datetime.now().strftime('%H:%M:%S')}] [DEBUG:{nt}] "
                              f"table={table}, fields={fields}, name_field={name_field}, "
                              f"total GSE_IDs={len(all_gse_ids)}, max_rows={GROUPINFO_SUB_NODE_MAX_ROWS}")

                    order_by = cfg.get("order_by")
                    order_cols = cfg.get("order_cols", [])

                    # Only SELECT needed columns (not SELECT *). Ordering columns must be
                    # part of the SELECT list because the query uses DISTINCT; they are not
                    # copied into node properties, which are built from `fields` only.
                    needed_cols = list(dict.fromkeys(fields + ["data"] + order_cols))
                    col_list = ", ".join(f"`{c}`" for c in needed_cols)
                    order_clause = f" ORDER BY {order_by}" if order_by else ""

                    # Query rows, group by data (=GSE_ID).
                    # When GROUPINFO_SUB_NODE_MAX_ROWS is set, query each GSE_ID
                    # individually with LIMIT to avoid fetching unnecessary rows.
                    # Keyed by (gse_id, group): the truncation limit must apply per
                    # comparison, not per GSE_ID. A dataset with 36 comparisons would
                    # otherwise return only the first N rows overall, leaving all but
                    # one comparison with no sub-nodes at all.
                    temp_lookup = {}  # {(gse_id, group): [row_dict, ...]}
                    total_rows = 0
                    total_gse = len(all_gse_ids)
                    try:
                        if GROUPINFO_SUB_NODE_MAX_ROWS is not None:
                            # Per-(GSE_ID, group) query with LIMIT (MySQL 5.x / 8.x compatible).
                            # `group` is a reserved word and must stay backquoted.
                            base_sql = (f"SELECT DISTINCT {col_list} FROM `{table}` "
                                        f"WHERE data = %s AND `group` = %s"
                                        f"{order_clause} "
                                        f"LIMIT {GROUPINFO_SUB_NODE_MAX_ROWS}")
                            for gse_idx, gse_id in enumerate(all_gse_ids):
                                batch_rows = 0
                                for grp in sorted(gse_info[gse_id]["groups"]):
                                    cursor.execute(base_sql, (gse_id, grp))
                                    for sub_row in cursor.fetchall():
                                        temp_lookup.setdefault((gse_id, grp), []).append(sub_row)
                                        batch_rows += 1
                                total_rows += batch_rows
                                if (gse_idx + 1) % BATCH_SIZE == 0 or (gse_idx + 1) == total_gse:
                                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{nt}] "
                                          f"GSE_IDs {gse_idx + 1}/{total_gse}: "
                                          f"{total_rows} rows cumul., {len(temp_lookup)} keys")
                        else:
                            # Original batch query (no per-comparison limit)
                            total_batches = (total_gse + BATCH_SIZE - 1) // BATCH_SIZE
                            for batch_idx in range(total_batches):
                                i = batch_idx * BATCH_SIZE
                                batch = all_gse_ids[i:i + BATCH_SIZE]
                                placeholders = ",".join(["%s"] * len(batch))
                                sql = f"SELECT DISTINCT {col_list} FROM `{table}` WHERE data IN ({placeholders})"
                                cursor.execute(sql, batch)
                                batch_rows = 0
                                for sub_row in cursor.fetchall():
                                    key = str(sub_row.get("data", "")).strip()
                                    grp = str(sub_row.get("group", "")).strip()
                                    if key and key in gse_info:
                                        temp_lookup.setdefault((key, grp), []).append(sub_row)
                                        batch_rows += 1
                                total_rows += batch_rows
                                print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{nt}] "
                                      f"batch {batch_idx + 1}/{total_batches} "
                                      f"({len(batch)} GSE_IDs): {batch_rows} rows "
                                      f"(cumulative: {total_rows} rows, {len(temp_lookup)} keys)")
                    except Exception as e:
                        print(f"  [WARN] Could not query {table}: {e}")
                        if is_debug:
                            import traceback
                            print(f"  [DEBUG:{nt}] Full traceback:\n{traceback.format_exc()}")
                        continue

                    if is_debug:
                        sample_keys = list(temp_lookup.keys())[:3]
                        sample_info = []
                        for k in sample_keys:
                            rows = temp_lookup[k]
                            sample_info.append(f"{k}: {len(rows)} rows, "
                                               f"first_row_cols={list(rows[0].keys()) if rows else 'N/A'}, "
                                               f"first_row_preview={ {c: rows[0].get(c) for c in (fields + ['data'])} if rows else 'N/A'}")
                        print(f"  [DEBUG:{nt}] query done: {total_rows} total rows, "
                              f"{len(temp_lookup)} keys; sample: {' | '.join(sample_info)}")

                    # Pre-resolve sub-node IDs per (gse_id, organism) for each row
                    # to avoid redundant _content_hash calls in the edge-creation loop.
                    # gse_row_nodes: {(gse_id, group): [sub_node_id, ...]}
                    gse_row_nodes = {}
                    total_keys = len(temp_lookup)
                    processed_keys = 0
                    for (gse_id, grp), rows in temp_lookup.items():
                        info = gse_info.get(gse_id)
                        if not info:
                            continue
                        node_ids_for_gse = []
                        seen_in_gse = set()  # dedup within this (gse, group)

                        for row in rows:
                            # Build base data from table fields
                            base_data = {}
                            for f in fields:
                                val = row.get(f)
                                if val is not None and str(val).strip():
                                    base_data[f] = str(val).strip()

                            name_val = base_data.get(name_field)
                            if not name_val:
                                continue

                            # One sub-node per unique (base_data + organism) combo
                            organisms = info["organisms"] if info["organisms"] else {""}
                            for organism in organisms:
                                merged_data = dict(base_data)
                                if organism:
                                    merged_data["organism"] = organism

                                content_hash = self._content_hash(merged_data)
                                sub_node_id = f"{cfg['id_prefix']}_{content_hash}"
                                sub_registry_key = (nt, sub_node_id)

                                if sub_registry_key not in entity_registry:
                                    sub_node = {
                                        "id": sub_node_id,
                                        "type": nt,
                                        "properties": {
                                            "name": name_val,
                                            **merged_data
                                        },
                                        "_provenance": {
                                            "source_type": "cross_table_lookup",
                                            "source_tables": [table],
                                            "gse_id": gse_id,
                                            "extracted_at": datetime.now().isoformat()
                                        }
                                    }
                                    entity_registry[sub_registry_key] = sub_node
                                    nodes.append(sub_node)
                                    nt_new_nodes += 1

                                if sub_node_id not in seen_in_gse:
                                    seen_in_gse.add(sub_node_id)
                                    node_ids_for_gse.append(sub_node_id)

                        gse_row_nodes[(gse_id, grp)] = node_ids_for_gse
                        processed_keys += 1
                        if processed_keys % 100 == 0 or processed_keys == total_keys:
                            print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{nt}] nodes: {processed_keys}/{total_keys} keys done, "
                                  f"{nt_new_nodes} new nodes so far")

                    # Create edges: each GroupInfo → only the sub-nodes of its own
                    # comparison. Matching on GSE_ID alone produced a cartesian
                    # fan-out (every comparison of a dataset received every sub-node).
                    total_gses = len(gse_row_nodes)
                    processed_gses = 0
                    for (gse_id, grp), node_ids in gse_row_nodes.items():
                        info = gse_info.get(gse_id)
                        if not info or not node_ids:
                            continue
                        for ginode_id, organism, ginode_group in info["ginodes"]:
                            if ginode_group != grp:
                                continue
                            for sub_node_id in node_ids:
                                edge = self._make_edge(ginode_id, sub_node_id, _SUB_RELATION_MAP.get(nt, "HAS"),
                                                       {"source_table": table, "gse_id": gse_id})
                                if edge:
                                    ek = (edge["source"], edge["target"], edge["relation"])
                                    if ek not in global_edge_keys:
                                        global_edge_keys.add(ek)
                                        edges.append(edge)
                                        nt_new_edges += 1
                        processed_gses += 1
                        if processed_gses % 100 == 0 or processed_gses == total_gses:
                            print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{nt}] edges: {processed_gses}/{total_gses} GSE_IDs done, "
                                  f"{nt_new_edges} new edges so far")

                    sub_node_counts[nt] = nt_new_nodes
                    sub_edge_counts[nt] = nt_new_edges
                    print(f"  [{nt}] {nt_new_nodes} new nodes, {nt_new_edges} new edges "
                          f"(from {table})")
                    if is_debug:
                        print(f"  [DEBUG:{nt}] summary: queried {total_rows} rows from {len(temp_lookup)} (GSE_ID, group) keys, "
                              f"created {nt_new_nodes} nodes + {nt_new_edges} edges; "
                              f"temp_lookup_keys={sorted(temp_lookup.keys())[:5]}{'...' if len(temp_lookup) > 5 else ''}")

            # ---- Step G: Create GeneAnnotation nodes for GroupDEGs/IntraClusterDEGs/ClusterMarkers ----
            print(f"[INFO] Creating GeneAnnotation nodes from gene symbols...")
            TARGET_TYPES = {"GroupDEGs", "IntraClusterDEGs", "ClusterMarkers"}

            # Phase 1: Collect unique (symbol, species) pairs from target sub-nodes
            # symbol_map: {(symbol_lower, species): {"symbol": orig, "species": species, "source_nodes": [(nt, node_id), ...]}}
            symbol_map = {}
            unknown_organisms = set()
            for (etype, node_id), node in entity_registry.items():
                if etype not in TARGET_TYPES:
                    continue
                symbol = str(node["properties"].get("symbol", "")).strip()
                organism = str(node["properties"].get("organism", "")).strip()
                if not symbol:
                    continue
                species = MySQLKnowledgeGraphBuilder._detect_species(organism)
                if not species:
                    if organism:
                        unknown_organisms.add(organism)
                    continue
                key = (symbol.lower(), species)
                if key not in symbol_map:
                    symbol_map[key] = {"symbol": symbol, "species": species, "source_nodes": []}
                symbol_map[key]["source_nodes"].append((etype, node_id))

            # Phase 1.5: Supplement symbol_map with symbols from Gene nodes
            # Gene nodes are created from the main table gene_name column (Step B).
            # GeneAnnotation is driven by DEG sub-nodes, so genes mentioned in the
            # literature but absent from RNA-seq results were previously missing
            # GeneAnnotation entries.  This pass adds every Gene with a recognised
            # species into symbol_map (with empty source_nodes) so they still
            # benefit from the annotation-table lookup and get a GeneAnnotation
            # node, which Phase 5 then links via HAS_ANNOTATION.
            gene_supplement_count = 0
            for (etype, node_id), node in entity_registry.items():
                if etype != "Gene":
                    continue
                gene_name = str(node["properties"].get("name", "")).strip()
                if not gene_name:
                    continue
                species_list = node["properties"].get("species", [])
                # Fallback: derive species from organisms list when species list is empty
                if not species_list:
                    for org in node["properties"].get("organisms", []):
                        sp = MySQLKnowledgeGraphBuilder._detect_species(str(org))
                        if sp and sp not in species_list:
                            species_list.append(sp)
                for sp in species_list:
                    key = (gene_name.lower(), sp)
                    if key not in symbol_map:
                        symbol_map[key] = {"symbol": gene_name, "species": sp, "source_nodes": []}
                        gene_supplement_count += 1

            if gene_supplement_count:
                print(f"  [INFO] Supplemented {gene_supplement_count} (symbol, species) pairs from Gene nodes")

            if unknown_organisms:
                print(f"  [INFO] Skipped {len(unknown_organisms)} unrecognized organism(s): "
                      f"{sorted(unknown_organisms)[:10]}")

            if not symbol_map:
                print(f"  No gene symbols with recognized organism found, skipping GeneAnnotation creation.")
            else:
                print(f"  Collected {len(symbol_map)} unique (symbol, species) pairs "
                      f"from {sum(len(v['source_nodes']) for v in symbol_map.values())} source nodes")

                GA_BATCH = 256
                gene_annotation_data = {}  # {(symbol_lower, species): {group_name: {...}}}
                # entrez_id lookup cache for crispick: {(symbol_lower, species): [entrez_id, ...]}
                symbol_entrez = {}

                # Pre-build reverse lookup index: species → symbol_lower → [(key, info), ...]
                # This avoids O(n_symbols) linear scan per database row (the main bottleneck).
                _symbol_lookup = {"human": {}, "mouse": {}}
                for _mk, _info in symbol_map.items():
                    _sp = _info["species"]
                    _sym_lower = _info["symbol"].lower()
                    _bucket = _symbol_lookup[_sp]
                    if _sym_lower not in _bucket:
                        _bucket[_sym_lower] = []
                    _bucket[_sym_lower].append(_mk)

                def _build_entrez_lookup():
                    """Rebuild entrez_id → [(sk, sp)] index (called after gene_annotation populates symbol_entrez)."""
                    _el = {"human": {}, "mouse": {}}
                    for (_sk, _sp), _eids in symbol_entrez.items():
                        for _eid in _eids:
                            if _eid not in _el[_sp]:
                                _el[_sp][_eid] = []
                            _el[_sp][_eid].append((_sk, _sp))
                    return _el

                _entrez_lookup_built = False  # lazy-init after gene_annotation populates symbol_entrez

                for group_cfg in GENE_ANNOTATION_TABLE_GROUPS:
                    group_name = group_cfg["group_name"]
                    match_col = group_cfg["match_column"]
                    match_col_alt = group_cfg.get("match_column_alt")
                    fields = group_cfg["fields"]
                    is_crispick = (group_name == "crispick")
                    is_multi_alias = bool(group_cfg.get("multi_alias"))

                    for species in ("human", "mouse"):
                        table_name = group_cfg[f"{species}_table"]
                        symbols_for_species = [
                            info["symbol"] for key, info in symbol_map.items()
                            if info["species"] == species
                        ]
                        if not symbols_for_species:
                            continue

                        try:
                            # Check table exists
                            cursor.execute(f"SHOW TABLES LIKE '{table_name}'")
                            if not cursor.fetchone():
                                print(f"  [WARN] Table '{table_name}' does not exist, "
                                      f"skipping {group_name}/{species}")
                                continue

                            if is_crispick or group_name == "string":
                                # crispick/string use entrez_id (from gene_annotation lookup)
                                # → Input / GeneID_x+GeneID_y columns.
                                # Collect entrez_ids for all symbols of this species.
                                entrez_ids = []
                                for (sym_lower, sp), info in symbol_map.items():
                                    if sp != species:
                                        continue
                                    eids = symbol_entrez.get((sym_lower, sp), [])
                                    entrez_ids.extend(eids)
                                # Dedup
                                entrez_ids = sorted(set(eids for eids in entrez_ids if eids))
                                lookup_values = entrez_ids
                                lookup_label = f"{len(entrez_ids)} entrez_ids"
                            else:
                                lookup_values = symbols_for_species
                                lookup_label = f"{len(symbols_for_species)} symbols"

                            if not lookup_values:
                                if is_crispick or group_name == "string":
                                    print(f"  [{group_name}/{species}] no entrez_ids resolved, skipping")
                                continue

                            # Build select columns: always include match column(s) so
                            # row.get(match_col) can read them (they may differ from
                            # the stored fields). Dedup with set to preserve DISTINCT
                            # semantics.
                            _select_cols = list(fields)
                            if match_col not in _select_cols:
                                _select_cols.append(match_col)
                            if match_col_alt and match_col_alt not in _select_cols:
                                _select_cols.append(match_col_alt)
                            col_list = ", ".join(f"`{c}`" for c in _select_cols)
                            match_col_q = f"`{match_col}`"
                            total_fetched = 0

                            # STRING tables can be huge (11M+ rows); use smaller batches
                            # and split OR into two queries to avoid MySQL lock-table overflow.
                            _is_string = bool(match_col_alt)
                            _batch_size = 64 if _is_string else GA_BATCH
                            _limit_clause = f" LIMIT {GENE_ANNOTATION_MAX_ROWS}" if GENE_ANNOTATION_MAX_ROWS is not None else ""

                            # multi_alias tables are read once in full rather than probed
                            # per symbol, because the match column holds an alias list.
                            _chunks = ([None] if is_multi_alias
                                       else [lookup_values[i:i + _batch_size]
                                             for i in range(0, len(lookup_values), _batch_size)])

                            for batch in _chunks:
                                placeholders = ",".join(["%s"] * len(batch)) if batch else ""

                                if is_multi_alias:
                                    sql = (f"SELECT DISTINCT {col_list} FROM `{table_name}`"
                                           f"{_limit_clause}")
                                    cursor.execute(sql)
                                    all_rows = cursor.fetchall()
                                elif match_col_alt:
                                    # STRING: query each column separately to reduce lock pressure,
                                    # then union results in Python via row-level dedup.
                                    alt_col_q = f"`{match_col_alt}`"
                                    sql_x = (f"SELECT DISTINCT {col_list} FROM `{table_name}` "
                                             f"WHERE {match_col_q} IN ({placeholders}){_limit_clause}")
                                    sql_y = (f"SELECT DISTINCT {col_list} FROM `{table_name}` "
                                             f"WHERE {alt_col_q} IN ({placeholders}){_limit_clause}")
                                    cursor.execute(sql_x, batch)
                                    rows_x = cursor.fetchall()
                                    cursor.execute(sql_y, batch)
                                    rows_y = cursor.fetchall()
                                    # Dedup by row values (tuples), preserving order
                                    seen = set()
                                    all_rows = []
                                    for r in rows_x + rows_y:
                                        rt = tuple(r.items())
                                        if rt not in seen:
                                            seen.add(rt)
                                            all_rows.append(r)
                                else:
                                    sql = (f"SELECT DISTINCT {col_list} FROM `{table_name}` "
                                           f"WHERE {match_col_q} IN ({placeholders}){_limit_clause}")
                                    params = batch
                                    cursor.execute(sql, params)
                                    all_rows = cursor.fetchall()

                                batch_rows = 0
                                # Build entrez reverse lookup on first entrez-match use (after gene_annotation populated symbol_entrez)
                                if (is_crispick or group_name == "string") and not _entrez_lookup_built:
                                    _entrez_lookup = _build_entrez_lookup()
                                    _entrez_lookup_built = True

                                for row in all_rows:
                                    # Determine which symbols this row matches (O(1) via reverse index)
                                    matched_keys = []
                                    raw_match = str(row.get(match_col, "")).strip()

                                    if is_crispick:
                                        # Match by entrez_id → Input column (O(1) lookup)
                                        matched_keys.extend(_entrez_lookup[species].get(raw_match, []))
                                    elif is_multi_alias:
                                        # Gene_Names is an alias list; match each token.
                                        _seen_ma = set()
                                        for _tok in raw_match.replace(";", " ").replace(",", " ").split():
                                            for sym in _symbol_lookup[species].get(_tok.lower(), []):
                                                if sym not in _seen_ma:
                                                    _seen_ma.add(sym)
                                                    matched_keys.append(sym)
                                    elif match_col_alt:
                                        # Dual column match (STRING or other multi-col groups)
                                        raw_alt = str(row.get(match_col_alt, "")).strip()
                                        seen = set()
                                        if is_crispick or group_name == "string":
                                            # STRING entrez match: use _entrez_lookup (no case-folding)
                                            for sym in _entrez_lookup[species].get(raw_match, []):
                                                if sym not in seen:
                                                    seen.add(sym)
                                                    matched_keys.append(sym)
                                            if raw_match != raw_alt:
                                                for sym in _entrez_lookup[species].get(raw_alt, []):
                                                    if sym not in seen:
                                                        seen.add(sym)
                                                        matched_keys.append(sym)
                                        else:
                                            # Symbol-based dual match (case-insensitive)
                                            for sym in _symbol_lookup[species].get(raw_match.lower(), []):
                                                if sym not in seen:
                                                    seen.add(sym)
                                                    matched_keys.append(sym)
                                            if raw_match.lower() != raw_alt.lower():
                                                for sym in _symbol_lookup[species].get(raw_alt.lower(), []):
                                                    if sym not in seen:
                                                        seen.add(sym)
                                                        matched_keys.append(sym)
                                    else:
                                        # Simple single-column match (O(1) lookup)
                                        matched_keys.extend(_symbol_lookup[species].get(raw_match.lower(), []))

                                    for mk in matched_keys:
                                        if mk not in gene_annotation_data:
                                            gene_annotation_data[mk] = {}
                                        if group_name not in gene_annotation_data[mk]:
                                            gene_annotation_data[mk][group_name] = []

                                        row_data = {}
                                        for f in fields:
                                            val = row.get(f)
                                            if val is not None and str(val).strip():
                                                row_data[f] = str(val).strip()
                                        if row_data:
                                            gene_annotation_data[mk][group_name].append(row_data)
                                    batch_rows += 1
                                total_fetched += batch_rows

                            print(f"  [{datetime.now().strftime('%H:%M:%S')}] [{group_name}/{species}] "
                                  f"queried {lookup_label} → {total_fetched} rows from {table_name}")

                            # For gene_annotation, cache entrez_id for crispick lookup
                            if group_name == "gene_annotation":
                                for mk, groups in gene_annotation_data.items():
                                    if mk[1] != species:
                                        continue
                                    ga_rows = groups.get("gene_annotation", [])
                                    for ga_row in ga_rows:
                                        eid = ga_row.get("entrez_id", "").strip()
                                        if eid:
                                            if mk not in symbol_entrez:
                                                symbol_entrez[mk] = []
                                            if eid not in symbol_entrez[mk]:
                                                symbol_entrez[mk].append(eid)

                        except Exception as e:
                            print(f"  [WARN] Error querying {table_name}: {e}")
                            continue

                # Phase 2.4: Cap STRING partners per gene, ranked by combined_score.
                # Also deduplicates partners that appear twice because the source table
                # is queried on both GeneID_x and GeneID_y.
                if STRING_PARTNERS_PER_GENE is not None:
                    trimmed_genes = 0
                    rows_before = rows_after = 0
                    for mk, groups in gene_annotation_data.items():
                        rows = groups.get("string")
                        if not rows:
                            continue
                        gene_sym = str(mk[0]).strip().upper()
                        best = {}
                        for r in rows:
                            sx = str(r.get("GeneSymbol_x", "")).strip()
                            sy = str(r.get("GeneSymbol_y", "")).strip()
                            partner = sy if sx.upper() == gene_sym else sx
                            if not partner:
                                partner = sy or sx
                            try:
                                score = float(r.get("combined_score", 0) or 0)
                            except (TypeError, ValueError):
                                score = 0.0
                            prev = best.get(partner)
                            if prev is None or score > prev[0]:
                                best[partner] = (score, r)
                        ordered = [r for _score, r in
                                   sorted(best.values(), key=lambda t: (-t[0], str(t[1])))]
                        rows_before += len(rows)
                        groups["string"] = ordered[:STRING_PARTNERS_PER_GENE]
                        rows_after += len(groups["string"])
                        if len(rows) != len(groups["string"]):
                            trimmed_genes += 1
                    print(f"  [{datetime.now().strftime('%H:%M:%S')}] [string] capped to top "
                          f"{STRING_PARTNERS_PER_GENE} partners per gene: "
                          f"{rows_before} → {rows_after} rows across {trimmed_genes} trimmed genes")

                # Phase 2.5: Build drug lookup from public_drug table
                # Config driven by kg_mapping.json → gene_annotation_drug_lookup
                drug_lookup = {}
                if GENE_ANNOTATION_DRUG_LOOKUP:
                    drug_table = GENE_ANNOTATION_DRUG_LOOKUP["table"]
                    drug_match_col = GENE_ANNOTATION_DRUG_LOOKUP["match_column"]
                    drug_target_field = GENE_ANNOTATION_DRUG_LOOKUP["target_field"]
                    drug_source_field = GENE_ANNOTATION_DRUG_LOOKUP.get("source_field", "drug_id")

                    # Collect all unique drug_id values from gene_annotation data
                    all_drug_ids = set()
                    for (sym_key, species), groups in gene_annotation_data.items():
                        ga_rows = groups.get("gene_annotation", [])
                        for row in ga_rows:
                            drug_id_val = str(row.get(drug_source_field, "")).strip()
                            if drug_id_val:
                                all_drug_ids.add(drug_id_val)

                    if all_drug_ids:
                        drug_ids_list = sorted(all_drug_ids)
                        DRUG_BATCH = 500
                        try:
                            with self.conn.cursor() as cursor:
                                for i in range(0, len(drug_ids_list), DRUG_BATCH):
                                    batch = drug_ids_list[i:i + DRUG_BATCH]
                                    placeholders = ",".join(["%s"] * len(batch))
                                    sql = (f"SELECT {drug_match_col}, {drug_target_field} "
                                           f"FROM {drug_table} "
                                           f"WHERE {drug_match_col} IN ({placeholders})")
                                    cursor.execute(sql, batch)
                                    for row in cursor.fetchall():
                                        db_id = str(row.get(drug_match_col, "")).strip()
                                        target_val = str(row.get(drug_target_field, "")).strip()
                                        if db_id and target_val:
                                            drug_lookup[db_id] = target_val
                            print(f"  [{drug_table}] Loaded {len(drug_lookup)} drug names "
                                  f"from {len(all_drug_ids)} unique {drug_source_field}s")
                        except Exception as e:
                            print(f"  [WARN] Could not query {drug_table} table: {e}")

                # Phase 3: Build GeneAnnotation nodes
                ga_new_nodes = 0
                ga_new_edges = 0

                for (sym_key, species), info in symbol_map.items():
                    merged_props = {
                        "name": info["symbol"],
                        "organism_species": species,
                    }

                    ga_data = gene_annotation_data.get((sym_key, species), {})

                    for group_cfg in GENE_ANNOTATION_TABLE_GROUPS:
                        group_name = group_cfg["group_name"]
                        rows = ga_data.get(group_name, [])
                        if not rows:
                            continue
                        if len(rows) == 1:
                            # Single row: merge flat with group prefix
                            for k, v in rows[0].items():
                                merged_props[f"{group_name}_{k}"] = v
                        else:
                            # Multi-row: store as JSON array
                            merged_props[f"{group_name}_rows"] = rows

                    # Resolve drug_id → preferred_name via public_drug lookup (config-driven)
                    if GENE_ANNOTATION_DRUG_LOOKUP:
                        drug_source_field = GENE_ANNOTATION_DRUG_LOOKUP.get("source_field", "drug_id")
                        drug_output_prop = GENE_ANNOTATION_DRUG_LOOKUP["output_property"]
                        # Single-row case: replace gene_annotation_{source_field} with output_property
                        old_prop_key = f"gene_annotation_{drug_source_field}"
                        if old_prop_key in merged_props:
                            drug_id_val = merged_props.pop(old_prop_key)
                            drug_name = drug_lookup.get(drug_id_val)
                            if drug_name:
                                merged_props[drug_output_prop] = drug_name
                        # Multi-row case: enrich each row in gene_annotation_rows
                        if "gene_annotation_rows" in merged_props:
                            for row in merged_props["gene_annotation_rows"]:
                                if drug_source_field in row:
                                    drug_id_val = row.pop(drug_source_field)
                                    drug_name = drug_lookup.get(drug_id_val)
                                    if drug_name:
                                        row["drug_name"] = drug_name

                    # Also store entrez_ids at top level for convenience
                    eids = symbol_entrez.get((sym_key, species), [])
                    if eids:
                        merged_props["entrez_ids"] = eids

                    # Dedup by content hash
                    content_hash = self._content_hash(merged_props)
                    ga_node_id = f"gan_{content_hash}"
                    ga_registry_key = ("GeneAnnotation", ga_node_id)

                    if ga_registry_key in entity_registry:
                        ga_node = entity_registry[ga_registry_key]
                    else:
                        ga_node = {
                            "id": ga_node_id,
                            "type": "GeneAnnotation",
                            "properties": merged_props,
                            "_provenance": {
                                "source_type": "cross_table_lookup",
                                "source_tables": [
                                    f"{cfg['human_table']}/{cfg['mouse_table']}"
                                    for cfg in GENE_ANNOTATION_TABLE_GROUPS
                                ],
                                "extracted_at": datetime.now().isoformat()
                            }
                        }
                        entity_registry[ga_registry_key] = ga_node
                        nodes.append(ga_node)
                        ga_new_nodes += 1

                    # Phase 4: Create edges from source sub-nodes → GeneAnnotation
                    for (st, sn_id) in info["source_nodes"]:
                        edge = self._make_edge(sn_id, ga_node_id, "HAS_GENE_ANNOTATION",
                                               {"source": "gene_annotation_lookup", "species": species})
                        if edge:
                            ek = (edge["source"], edge["target"], edge["relation"])
                            if ek not in global_edge_keys:
                                global_edge_keys.add(ek)
                                edges.append(edge)
                                ga_new_edges += 1

                sub_node_counts["GeneAnnotation"] = ga_new_nodes
                sub_edge_counts["GeneAnnotation"] = ga_new_edges
                print(f"  [GeneAnnotation] {ga_new_nodes} new nodes, {ga_new_edges} new edges "
                      f"(from {len(symbol_map)} unique symbols)")

                # Phase 5: Create Gene → GeneAnnotation edges
                # Build lookup: (symbol_lower, species) → ga_node_id
                ga_lookup = {}
                for (etype, node_id), node in entity_registry.items():
                    if etype == "GeneAnnotation":
                        sym = str(node["properties"].get("name", "")).strip().lower()
                        sp = str(node["properties"].get("organism_species", "")).strip()
                        if sym and sp:
                            ga_lookup[(sym, sp)] = node_id

                # ---- DEBUG: Phase 5 diagnostics ----
                _dbg_ga_species = set()
                for (_s, _sp) in ga_lookup:
                    _dbg_ga_species.add(_sp)
                print(f"  [Phase5 DEBUG] ga_lookup: {len(ga_lookup)} entries, "
                      f"species values: {sorted(_dbg_ga_species)}")
                # Sample the first 5 ga_lookup keys
                for _i, _k in enumerate(sorted(ga_lookup.keys())[:5]):
                    print(f"    ga_lookup sample[{_i}]: {_k} → {ga_lookup[_k]}")

                gene_ga_edges = 0
                _dbg_gene_total = 0
                _dbg_gene_with_species = 0
                _dbg_gene_with_organisms = 0
                _dbg_gene_species_values = set()
                _dbg_gene_attempted = 0
                _dbg_gene_miss_reasons = defaultdict(int)
                _dbg_miss_samples = []  # (gene_name, species_list, organisms)
                for (etype, node_id), node in entity_registry.items():
                    if etype != "Gene":
                        continue
                    _dbg_gene_total += 1
                    gene_name = str(node["properties"].get("name", "")).strip().lower()
                    species_list = node["properties"].get("species", [])
                    organisms_list = node["properties"].get("organisms", [])
                    if species_list:
                        _dbg_gene_with_species += 1
                        for s in species_list:
                            _dbg_gene_species_values.add(s)
                    if organisms_list:
                        _dbg_gene_with_organisms += 1
                    # Fallback: derive species from organisms list when species is empty
                    if not species_list:
                        for org in organisms_list:
                            sp = MySQLKnowledgeGraphBuilder._detect_species(str(org))
                            if sp and sp not in species_list:
                                species_list.append(sp)
                    for sp in species_list:
                        _dbg_gene_attempted += 1
                        ga_key = (gene_name, sp)
                        ga_node_id = ga_lookup.get(ga_key)
                        if ga_node_id:
                            edge = self._make_edge(node["id"], ga_node_id, "HAS_ANNOTATION",
                                                   {"source": "gene_to_annotation_lookup",
                                                    "species": sp})
                            if edge:
                                ek = (edge["source"], edge["target"], edge["relation"])
                                if ek not in global_edge_keys:
                                    global_edge_keys.add(ek)
                                    edges.append(edge)
                                    gene_ga_edges += 1
                        else:
                            _dbg_gene_miss_reasons["no_ga_match"] += 1
                            if len(_dbg_miss_samples) < 5:
                                _dbg_miss_samples.append(
                                    (gene_name, species_list, organisms_list))
                    if not species_list:
                        _dbg_gene_miss_reasons["no_species"] += 1
                        if len(_dbg_miss_samples) < 10:
                            _dbg_miss_samples.append(
                                (gene_name, [], organisms_list))

                print(f"  [Phase5 DEBUG] Gene nodes: total={_dbg_gene_total}, "
                      f"with_species={_dbg_gene_with_species}, "
                      f"with_organisms={_dbg_gene_with_organisms}, "
                      f"species_values_in_genes={sorted(_dbg_gene_species_values)}")
                print(f"  [Phase5 DEBUG] Attempted matches: {_dbg_gene_attempted}, "
                      f"successful: {gene_ga_edges}")
                print(f"  [Phase5 DEBUG] Miss reasons: {dict(_dbg_gene_miss_reasons)}")
                if _dbg_miss_samples:
                    print(f"  [Phase5 DEBUG] Miss samples (gene_name, species_list, organisms):")
                    for _ms in _dbg_miss_samples[:10]:
                        print(f"    {_ms}")

                if gene_ga_edges:
                    sub_node_counts["GeneToAnnotation"] = gene_ga_edges
                    print(f"  [Gene→GeneAnnotation] {gene_ga_edges} new HAS_ANNOTATION edges")
                else:
                    print(f"  [Gene→GeneAnnotation] WARNING: 0 HAS_ANNOTATION edges created!")

        print(f"[OK] Wide-table build complete: {len(nodes)} nodes, {len(edges)} edges")
        return nodes, edges

    def _row_to_node_wide(self, row: dict, table: str, node_type: str,
                           id_prefix: str) -> Optional[dict]:
        """Wide table row → core node (excluding columns already extracted as entity nodes)"""
        props = {}
        extracted_cols = set(WIDE_TABLE_ENTITY_MAPPING.keys())
        # Also exclude doi (already handled via reference column)
        extracted_cols.add("doi")
        # v3.0: exclude compound entity columns (extracted as independent nodes)
        for config_key, config in COMPOUND_ENTITY_CONFIG.items():
            extracted_cols.add(config_key)
            for sc in config.get("source_columns", []):
                extracted_cols.add(sc)
        # v3.0: exclude enrichment columns (enriched onto other nodes, not Sample props)
        extracted_cols.update({"sgrna", "accession", "deleted_at", "is_organoid"})

        for col, val in row.items():
            if col in extracted_cols:
                continue
            if val is None:
                continue
            if isinstance(val, datetime):
                props[col] = val.isoformat()
            elif isinstance(val, (str, int, float, bool, list, dict)):
                props[col] = val
            elif isinstance(val, bytes):
                try:
                    props[col] = val.decode('utf-8')
                except UnicodeDecodeError:
                    props[col] = val.hex()
            else:
                props[col] = str(val)

        pk_val = row.get("sample_id", list(row.values())[0])
        node_id = f"{id_prefix}_{pk_val}"

        return {
            "id": node_id,
            "type": node_type,
            "properties": props,
            "_provenance": {
                "source_type": "mysql",
                "source_database": self.connection_params["database"],
                "source_table": table,
                "source_row_id": str(pk_val),
                "extracted_at": datetime.now().isoformat()
            }
        }

    def _get_or_create_entity(self, registry: dict, entity_type: str, prefix: str,
                               name: str, extra_props: dict = None) -> Tuple[dict, bool]:
        """Get or create entity node (dedup by name)"""
        key = (entity_type, name.lower().strip())
        if key in registry:
            return registry[key], False

        # Use first 8 chars of MD5 as hash
        name_hash = hashlib.md5(name.lower().strip().encode()).hexdigest()[:8]
        node_id = f"{prefix}_{name_hash}"

        props = {"name": name}

        node = {
            "id": node_id,
            "type": entity_type,
            "properties": props,
            "_provenance": {
                "source_type": "column_extraction",
                "extracted_at": datetime.now().isoformat()
            }
        }
        registry[key] = node
        return node, True

    def _parse_json_column(self, value) -> Optional[list]:
        """Parse JSON column, return list; if already a list, return directly"""
        if value is None:
            return None
        if isinstance(value, list):
            return value
        if isinstance(value, str):
            try:
                parsed = json.loads(value)
                return parsed if isinstance(parsed, list) else [parsed]
            except json.JSONDecodeError:
                return [value]
        if isinstance(value, dict):
            return [value]
        return [str(value)]

    def _extract_entity_name(self, item, col: str) -> Optional[str]:
        """Extract entity name from JSON entry"""
        if isinstance(item, str):
            return item.strip()
        if isinstance(item, dict):
            for key in JSON_ENTITY_NAME_KEYS:
                if key in item and item[key]:
                    val = item[key]
                    if isinstance(val, dict):
                        # e.g. biomarker_name: {src: "TLR2", std: "Tlr2"} → "Tlr2"
                        # Prefer the standardized symbol so graph entities carry one
                        # canonical name; fall back to the source spelling only when no
                        # standardized value exists. Using `src` here previously meant the
                        # same molecule appeared under several literature spellings
                        # (e.g. OCT3/4 vs POU5F1, β3-tubulin vs TUBB3).
                        for _sub in ("std", "name", "src"):
                            if val.get(_sub):
                                return str(val[_sub]).strip()
                        return str(list(val.values())[0]).strip() if val else ""
                    return str(val).strip()
            # If no standard key, take the first string value
            for k, v in item.items():
                if isinstance(v, str) and v.strip():
                    return v.strip()
        return None

    def _flatten_json_values(self, data, max_depth: int = 5) -> List[str]:
        """Recursively flatten nested JSON, collecting all leaf-node string values

        For deeply nested structures like techologies:
        {'cellular_imaging': {'microscopies': [{'method': 'Bright-field', ...}]}}
        → ['Bright-field', 'Leica DM6 microscope', ...]
        """
        results = []
        if isinstance(data, str):
            s = data.strip()
            if s:
                results.append(s)
        elif isinstance(data, list):
            for item in data:
                if max_depth > 0:
                    results.extend(self._flatten_json_values(item, max_depth - 1))
        elif isinstance(data, dict):
            for key, val in data.items():
                if isinstance(val, str) and val.strip():
                    results.append(val.strip())
                elif max_depth > 0:
                    results.extend(self._flatten_json_values(val, max_depth - 1))
        return results

    @staticmethod
    def _find_cultures_array(data, max_depth: int = 4):
        """Find cultures[] array in nested JSON, return list or None

        General-purpose helper method shared by _extract_infection_data and _extract_cell_factors_from_raw.
        """
        if max_depth <= 0 or data is None:
            return None
        if isinstance(data, dict):
            if "cultures" in data and isinstance(data["cultures"], list):
                return data["cultures"]
            for val in data.values():
                if isinstance(val, (dict, list)):
                    result = MySQLKnowledgeGraphBuilder._find_cultures_array(val, max_depth - 1)
                    if result is not None:
                        return result
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    result = MySQLKnowledgeGraphBuilder._find_cultures_array(item, max_depth - 1)
                    if result is not None:
                        return result
        return None

    def _extract_infection_data(self, data, max_depth: int = 4):
        """Extract all infection data from raw_json, supporting cultures[].story.infection_list path

        A single record may contain multiple cultures, each with its own
        story.infection_list. This method traverses all cultures[] entries and aggregates all infections.

        Args:
            data: raw_json parsed dict/list
            max_depth: Maximum nesting depth for searching the cultures array

        Returns:
            list of infection dicts, or None if no data
        """
        if max_depth <= 0 or data is None:
            return None

        cultures = self._find_cultures_array(data, max_depth)
        if cultures:
            all_infections = []
            for culture in cultures:
                if not isinstance(culture, dict):
                    continue
                story = culture.get("story")
                if isinstance(story, dict):
                    inf_list = story.get("infection_list")
                    if inf_list and isinstance(inf_list, list):
                        all_infections.extend(inf_list)
            if all_infections:
                return all_infections

        # Fallback: legacy format without cultures[] structure → recursively search for single infection_list
        return self._find_infection_key_fallback(data, max_depth)

    @staticmethod
    def _find_key_recursive(data, target_key: str, max_depth: int = 4):
        """Recursively search for a key in nested dicts/lists and return its value.

        Args:
            data: dict, list, or other parsed JSON data
            target_key: key name to search for (exact match)
            max_depth: Maximum nesting depth

        Returns:
            The value associated with the key, or None if not found
        """
        if max_depth <= 0 or data is None:
            return None
        if isinstance(data, dict):
            if target_key in data and data[target_key] is not None:
                return data[target_key]
            for val in data.values():
                if isinstance(val, (dict, list)):
                    result = MySQLKnowledgeGraphBuilder._find_key_recursive(val, target_key, max_depth - 1)
                    if result is not None:
                        return result
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    result = MySQLKnowledgeGraphBuilder._find_key_recursive(item, target_key, max_depth - 1)
                    if result is not None:
                        return result
        return None

    def _extract_coculture_technique_from_raw(self, data, max_depth: int = 4):
        """Extract CoCultureTechnique data from raw_json at cultures[].story.culture_technique_coculture.

        Iterates all culture entries and collects their technique descriptions.
        Follows the same cultures[] pattern as _extract_infection_data.

        Args:
            data: raw_json parsed dict/list or JSON string
            max_depth: Maximum nesting depth for searching

        Returns:
            List of technique values (strings from each culture entry), or None if no data
        """
        if data is None:
            return None
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None

        techniques = []

        cultures = self._find_cultures_array(data, max_depth)
        if cultures:
            for culture in cultures:
                if not isinstance(culture, dict):
                    continue
                story = culture.get("story")
                if isinstance(story, dict):
                    val = story.get("culture_technique_coculture")
                    if val is not None and str(val).strip():
                        techniques.append(str(val).strip())
        else:
            # Fallback: no cultures[] wrapper — search recursively for the key
            val = self._find_key_recursive(data, "culture_technique_coculture", max_depth)
            if val is not None and str(val).strip():
                techniques.append(str(val).strip())

        return techniques if techniques else None

    def _extract_coculture_material_sources_from_raw(self, data, max_depth: int = 4):
        """Extract material_sources from raw_json for co-culture entries.

        Iterates cultures[], checks base_info.if_co_culture.
        For coculture entries, collects story.material_source values.

        Args:
            data: raw_json parsed dict/list or JSON string
            max_depth: Maximum nesting depth for searching

        Returns:
            Aggregated material_source string (comma-joined), or None if no data
        """
        if data is None:
            return None
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except json.JSONDecodeError:
                return None

        material_sources = []

        # Collect (base_info_dict, story_dict) pairs from cultures[] or top-level
        cultures = self._find_cultures_array(data, max_depth)
        if cultures:
            for c in cultures:
                if isinstance(c, dict):
                    base_info = c.get("base_info", {})
                    story = c.get("story", {})
                    if isinstance(base_info, dict):
                        is_coculture = base_info.get("if_co_culture", False)
                        if is_coculture is True or str(is_coculture).lower() in ("true", "1", "yes"):
                            if isinstance(story, dict):
                                ms = story.get("material_source")
                                if ms is not None:
                                    material_sources.append(str(ms))
        else:
            # Structure 2: no cultures[] — check top-level
            base_info = data.get("base_info", {}) if isinstance(data, dict) else {}
            story = data.get("story", {}) if isinstance(data, dict) else {}
            if isinstance(base_info, dict):
                is_coculture = base_info.get("if_co_culture", False)
                if is_coculture is True or str(is_coculture).lower() in ("true", "1", "yes"):
                    if isinstance(story, dict):
                        ms = story.get("material_source")
                        if ms is not None:
                            material_sources.append(str(ms))

        if material_sources:
            return "; ".join(material_sources)
        return None

    @staticmethod
    def _parse_material_sources(material_sources_value) -> List[dict]:
        """Parse material_sources into a list of dicts with material properties.

        Handles multiple formats:
          1. CultureProtocol: JSON string of list-of-dicts
             e.g. '[{"source":"Corning","cat_number":"356231","material_name":"Matrigel","material_type":"Matrix"}]'
          2. CoCultureProtocol: semicolon-joined string or Python-literal list string
             e.g. "Matrigel; Collagen I" or "['Matrigel', 'Collagen I']"

        Returns:
            List of dicts, each with optional keys: source, cat_number, lot_number,
            source_type, material_name, material_type.
            Returns empty list if material_sources_value is None/empty/unparseable.
        """
        if material_sources_value is None:
            return []

        items = []

        # ---- Format 1: JSON array (CultureProtocol) ----
        if isinstance(material_sources_value, str):
            try:
                parsed = json.loads(material_sources_value)
                if isinstance(parsed, list):
                    for item in parsed:
                        if isinstance(item, dict):
                            items.append(item)
                        elif isinstance(item, str) and item.strip():
                            items.append({"material_name": item.strip()})
                    if items:
                        return items
            except (json.JSONDecodeError, TypeError):
                pass

        # ---- Format 2: semicolon-joined or Python-literal (CoCultureProtocol) ----
        raw_str = (str(material_sources_value) if not isinstance(material_sources_value, str)
                   else material_sources_value)

        # Try Python literal_eval on the whole string first
        try:
            parsed = ast.literal_eval(raw_str)
            if isinstance(parsed, list):
                for item in parsed:
                    if isinstance(item, dict):
                        items.append(item)
                    elif isinstance(item, str) and item.strip():
                        items.append({"material_name": item.strip()})
                if items:
                    return items
        except (ValueError, SyntaxError):
            pass

        # Split by semicolon and try each part
        parts = [p.strip() for p in raw_str.split(";") if p.strip()]
        for part in parts:
            # Try JSON on individual part
            try:
                sub = json.loads(part)
                if isinstance(sub, dict):
                    items.append(sub)
                    continue
                elif isinstance(sub, str) and sub.strip():
                    items.append({"material_name": sub.strip()})
                    continue
            except (json.JSONDecodeError, TypeError):
                pass

            # Try ast.literal_eval on individual part
            try:
                sub = ast.literal_eval(part)
                if isinstance(sub, dict):
                    items.append(sub)
                    continue
                elif isinstance(sub, str) and sub.strip():
                    items.append({"material_name": sub.strip()})
                    continue
            except (ValueError, SyntaxError):
                pass

            # Fallback: plain material name
            items.append({"material_name": part})

        return items

    @staticmethod
    def _match_material_similarity(material_name: str, similarity_rows: List[dict]) -> Optional[dict]:
        """Match a material_name against Material_name_similarity_candidates_enhanced.

        Uses Python 'in' operator for substring matching: iterates all preloaded rows,
        checks whether material_name (lowercased) is found within the row's similar_names text.

        Args:
            material_name: The raw material name from a protocol's material_sources entry.
            similarity_rows: Preloaded list of dicts from the similarity table.

        Returns:
            Dict with keys matching MATERIAL_SIMILARITY_CONFIG.output_fields if a match
            is found, or None if no match.
        """
        if not material_name or not similarity_rows:
            return None

        mat_lower = material_name.lower().strip()
        output_fields = MATERIAL_SIMILARITY_CONFIG.get("output_fields", [])

        # similar_names is a '|'-delimited alias list. Substring matching against the
        # whole string made short names collide (e.g. "EGF" matched VEGF, VEGFC and
        # HB-EGF), and returning on the first hit made the result depend on row order.
        # Aliases are compared exactly instead, and candidates are ranked so that the
        # outcome is deterministic: exact standard_name first, then the longest matching
        # alias, then standard_name alphabetically.
        candidates = []
        for row in similarity_rows:
            similar = row.get("similar_names") or ""
            aliases = [a.strip().lower() for a in str(similar).split("|") if a.strip()]
            std_name = str(row.get("standard_name") or "").strip()
            if std_name and std_name.lower() not in aliases:
                aliases.append(std_name.lower())
            if mat_lower not in aliases:
                continue
            exact_std = 1 if std_name.lower() == mat_lower else 0
            longest = max((len(a) for a in aliases if a == mat_lower), default=0)
            candidates.append((-exact_std, -longest, std_name, row))

        if not candidates:
            return None
        candidates.sort(key=lambda t: (t[0], t[1], t[2]))
        row = candidates[0][3]

        result = {}
        for field in output_fields:
            val = row.get(field)
            if val is not None and str(val).strip():
                result[field] = str(val).strip()
        if result:
            return result
        std_name = row.get("standard_name")
        if std_name:
            return {"standard_name": str(std_name).strip()}
        return None

    def _extract_cell_factors_from_raw(self, data, max_depth: int = 4,
                                        debug_info: dict = None):
        """Extract cell_factors from raw_json, split by if_co_culture

        Supports two JSON structures:
        1. With cultures[] wrapper (multi-culture):
           cultures[].base_info.if_co_culture + cultures[].story.protocol[].growth_factors
        2. Without cultures[] wrapper (single culture, current actual data format):
           top-level base_info.if_co_culture + top-level story.protocol[].growth_factors

        Splitting rule: if_co_culture == True  → coculture_cell_factors
                         otherwise              → cell_factors
        """
        cell_factors = []
        coculture_factors = []

        if data is None:
            if debug_info is not None:
                debug_info["error"] = "data is None"
            return cell_factors, coculture_factors

        # Collect (base_info_dict, story_dict) pairs
        culture_units = []

        cultures = self._find_cultures_array(data, max_depth)
        if cultures:
            # Structure 1: cultures[] wrapper
            if debug_info is not None:
                debug_info["structure"] = "cultures[]"
                debug_info["culture_count"] = len(cultures)
            for c in cultures:
                if isinstance(c, dict):
                    culture_units.append((c.get("base_info", {}), c.get("story", {})))
        else:
            # Structure 2: no cultures[] — find story/base_info from top level or recursively
            if debug_info is not None:
                debug_info["structure"] = "flat (no cultures)"
            # Try to find story and base_info in data
            base_info = data.get("base_info", {}) if isinstance(data, dict) else {}
            story = data.get("story", {}) if isinstance(data, dict) else {}
            if isinstance(story, dict) and story:
                culture_units.append((base_info, story))
            else:
                # Deep search: find first story containing protocol[]
                found_story, found_base_info = self._find_story_and_base_info(data, max_depth)
                if found_story:
                    culture_units.append((found_base_info or {}, found_story))

        if debug_info is not None:
            debug_info["culture_units_found"] = len(culture_units)

        for ui, (base_info, story) in enumerate(culture_units):
            # Determine if co-culture
            is_coculture = False
            if isinstance(base_info, dict):
                val = base_info.get("if_co_culture", False)
                if val is True or str(val).lower() in ("true", "1", "yes"):
                    is_coculture = True

            if debug_info is not None and ui < 2:
                unit_debug = {
                    "unit_idx": ui,
                    "base_info_keys": str(list(base_info.keys()))[:200] if isinstance(base_info, dict) else f"type={type(base_info).__name__}",
                    "if_co_culture_raw": repr(base_info.get("if_co_culture")) if isinstance(base_info, dict) else "N/A",
                    "is_coculture": is_coculture,
                }

            # Collect growth_factors from story.protocol[]
            if not isinstance(story, dict):
                continue
            protocols = story.get("protocol", [])
            if not isinstance(protocols, list):
                if debug_info is not None and ui < 2:
                    unit_debug["protocol_type"] = type(protocols).__name__
                    unit_debug["story_keys"] = str(list(story.keys()))[:200]
                    debug_info.setdefault("culture_units_debug", []).append(unit_debug)
                continue

            if debug_info is not None and ui < 2:
                unit_debug["protocol_count"] = len(protocols)
                if protocols:
                    unit_debug["protocol_0_keys"] = str(list(protocols[0].keys()))[:300] if isinstance(protocols[0], dict) else f"type={type(protocols[0]).__name__}"

            for pi, protocol in enumerate(protocols):
                if not isinstance(protocol, dict):
                    continue
                growth_factors = protocol.get("growth_factors")
                if not growth_factors:
                    if debug_info is not None and ui < 2 and pi == 0:
                        unit_debug["protocol_0_has_growth_factors"] = False
                        unit_debug["protocol_0_keys_full"] = str(list(protocol.keys()))[:300]
                    continue
                if isinstance(growth_factors, list):
                    items = growth_factors
                else:
                    items = [growth_factors]

                if is_coculture:
                    coculture_factors.extend(items)
                else:
                    cell_factors.extend(items)

                if debug_info is not None and ui < 2 and pi == 0:
                    unit_debug["growth_factors_found"] = True
                    unit_debug["growth_factors_type"] = type(growth_factors).__name__
                    unit_debug["growth_factors_len"] = len(items) if isinstance(items, list) else 1
                    unit_debug["growth_factors_preview"] = str(items)[:300]

            if debug_info is not None and ui < 2:
                debug_info.setdefault("culture_units_debug", []).append(unit_debug)

        return cell_factors, coculture_factors

    @staticmethod
    def _find_story_and_base_info(data, max_depth: int = 4):
        """In a structure without cultures[], search for story (containing protocol[]) and its sibling base_info

        Returns: (story_dict, base_info_dict) or (None, None)
        """
        if max_depth <= 0 or data is None:
            return None, None
        if isinstance(data, dict):
            # Sibling story and base_info
            story = data.get("story")
            base_info = data.get("base_info")
            if isinstance(story, dict) and isinstance(story.get("protocol"), list):
                return story, base_info if isinstance(base_info, dict) else None
            # Recursive search
            for val in data.values():
                if isinstance(val, (dict, list)):
                    s, bi = MySQLKnowledgeGraphBuilder._find_story_and_base_info(val, max_depth - 1)
                    if s is not None:
                        return s, bi
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    s, bi = MySQLKnowledgeGraphBuilder._find_story_and_base_info(item, max_depth - 1)
                    if s is not None:
                        return s, bi
        return None, None

    def _find_infection_key_fallback(self, data, max_depth: int = 4):
        """Fallback: recursively search raw_json for infection-related keys, return their values

        Only used when _extract_infection_data cannot find a cultures[] structure.
        """
        if max_depth <= 0:
            return None
        if isinstance(data, dict):
            if "infection_list" in data and data["infection_list"] is not None:
                return data["infection_list"]
            if "infection" in data and data["infection"] is not None:
                return data["infection"]
            for key in data:
                if "infection" in key.lower() and data[key] is not None:
                    return data[key]
            for key, val in data.items():
                if isinstance(val, (dict, list)):
                    result = self._find_infection_key_fallback(val, max_depth - 1)
                    if result is not None:
                        return result
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, (dict, list)):
                    result = self._find_infection_key_fallback(item, max_depth - 1)
                    if result is not None:
                        return result
        return None

    def _make_edge(self, source_id: str, target_id: str, relation: str,
                   props: dict = None) -> Optional[dict]:
        """Create edge"""
        if not source_id or not target_id:
            return None
        edge = {
            "id": f"e_{source_id}_{relation}_{target_id}",
            "source": source_id,
            "target": target_id,
            "relation": relation,
            "properties": props or {},
            "_provenance": {
                "derived_from": props.get("source_column", "") if props else "",
                "confidence": 1.0
            }
        }
        return edge

    def build_explicit_drug_disease_edges(self, entity_registry: dict,
                                          nodes: list) -> List[dict]:
        """Materialize drug→disease edges from the explicit pairing in drug_screening.

        The source JSON pairs each drug with the disease it was screened against, so no
        inference is needed. Diseases named here are often absent from the
        disease_modeling column (which is frequently "not specified"), so the
        DiseaseModel node is created on demand using the same id scheme.

        Edges carry the supporting sample ids, making them traceable per edge rather
        than requiring a reverse lookup.
        """
        records = getattr(self, "_explicit_drug_disease", [])
        if not records:
            return []

        agg = {}
        for rec in records:
            disease_node, is_new = self._get_or_create_entity(
                entity_registry, "DiseaseModel", "dm", rec["disease_name"])
            if is_new:
                disease_node["properties"]["source_field"] = "drug_screening.disease"
                nodes.append(disease_node)
            key = (rec["drug_node_id"], disease_node["id"])
            entry = agg.setdefault(key, {"samples": set(), "responses": set()})
            entry["samples"].add(rec["sample_id"])
            if rec["drug_response"]:
                entry["responses"].add(rec["drug_response"])

        out = []
        for (drug_id, disease_id), entry in agg.items():
            samples = sorted(entry["samples"])
            out.append({
                "id": f"e_screen_{drug_id}_{disease_id}",
                "source": drug_id,
                "target": disease_id,
                "relation": "SCREENED_AGAINST_DISEASE",
                "properties": {
                    "evidence": "explicit_source_pairing",
                    "supporting_sample_count": len(samples),
                    "supporting_samples": samples[:50],
                    "drug_response": sorted(entry["responses"])[:10],
                },
                "_provenance": {
                    "derived_from": "explicit_source_pairing",
                    "source_columns": ["drug_screening"],
                    "supporting_samples": samples,
                },
            })
        print(f"  [explicit drug-disease] {len(out)} SCREENED_AGAINST_DISEASE edges "
              f"from {len(records)} source pairings")
        return out

    def _infer_relationships(self, sample_entities: dict, entity_registry: dict,
                              nodes: list, edges: list) -> List[dict]:
        """Infer co-occurrence relationships (ASSOCIATED_WITH_DISEASE).

        Co-occurrence means the two entities appear on the same Sample; it does not
        imply a causal, therapeutic or validated association. Each edge records the
        samples that support it and how many there are, so the claim can be checked
        without a reverse lookup. Drug→disease is not inferred here: that pairing is
        explicit in the source and handled by build_explicit_drug_disease_edges().
        """
        inferred = []
        agg = {}

        for src_col, tgt_col, src_type, tgt_type, relation in INFERRED_RELATIONS:
            for sample_id, type_sets in sample_entities.items():
                src_ids = type_sets.get(src_type, set())
                tgt_ids = type_sets.get(tgt_type, set())
                if not src_ids or not tgt_ids:
                    continue
                for src_id in src_ids:
                    for tgt_id in tgt_ids:
                        key = (src_id, tgt_id, relation)
                        entry = agg.setdefault(key, {"samples": set(), "cols": set()})
                        entry["samples"].add(sample_id)
                        entry["cols"].add((src_col, tgt_col))

        for (src_id, tgt_id, relation), entry in agg.items():
            samples = sorted(entry["samples"])
            cols = sorted({c for pair in entry["cols"] for c in pair})
            inferred.append({
                "id": f"e_infer_{src_id}_{relation}_{tgt_id}",
                "source": src_id,
                "target": tgt_id,
                "relation": relation,
                "properties": {
                    "evidence": "same_sample_co_occurrence",
                    "supporting_sample_count": len(samples),
                    "supporting_samples": samples[:50],
                    "derived_from": "co_occurrence_inference",
                },
                "_provenance": {
                    "derived_from": "co_occurrence_inference",
                    "source_columns": cols,
                    "supporting_samples": samples,
                },
            })

        return inferred

    # -------------------------------------------------------------------------
    # Compound entity extraction (v3.0 new node types)
    # -------------------------------------------------------------------------

    @staticmethod
    def _content_hash(data: dict) -> str:
        """Generate content hash from a normalized dict (sorted keys, stable JSON serialization)"""
        normalized = json.dumps(data, sort_keys=True, ensure_ascii=False, default=str)
        return hashlib.md5(normalized.encode('utf-8')).hexdigest()[:8]

    @staticmethod
    def _detect_species(organism_str: str):
        """Return 'human', 'mouse', or None from an Organism scientific name.

        Handles 'Homo sapiens', 'Mus musculus', and common variants.
        Case-insensitive substring match.
        """
        if not organism_str:
            return None
        org_lower = organism_str.strip().lower()
        if "homo sapiens" in org_lower:
            return "human"
        if "mus musculus" in org_lower:
            return "mouse"
        return None

    @staticmethod
    def _parse_protocol_json(raw_value) -> dict:
        """Parse a cultivation_protocol JSON value and extract structured fields.

        Handles both primary protocol (protocol[].stage/steps/medium/...) and
        co-culture protocol (time_axis[]/time_anchors[]) structures.

        Returns a dict with keys:
            stages, steps_text, media_used, supplement_names,
            growth_factor_names, small_molecule_names,
            protocol_text (flattened for full-text search),
            summary (human-readable one-liner),
            dedup_key (normalized data for content-hash dedup)
        """
        result = {
            "stages": [], "steps_text": [], "media_used": [],
            "supplement_names": [], "growth_factor_names": [],
            "small_molecule_names": [], "protocol_text": "",
            "summary": "", "dedup_key": {},
        }
        if not raw_value:
            return result

        # Parse JSON if string
        if isinstance(raw_value, str):
            try:
                data = json.loads(raw_value)
            except (json.JSONDecodeError, TypeError):
                return result
        elif isinstance(raw_value, dict):
            data = raw_value
        else:
            return result

        if not isinstance(data, dict):
            return result

        stages = []
        all_steps = []
        media_set = set()
        supplement_set = set()
        gf_set = set()
        sm_set = set()

        # -- Primary protocol structure: {"protocol": [{stage, steps, medium, ...}, ...]} --
        protocol_list = data.get("protocol")
        if isinstance(protocol_list, list):
            for stage_obj in protocol_list:
                if not isinstance(stage_obj, dict):
                    continue
                stage_name = stage_obj.get("stage", "")
                if stage_name:
                    stages.append(str(stage_name).strip())

                steps = stage_obj.get("steps")
                if isinstance(steps, list):
                    for s in steps:
                        if s and str(s).strip():
                            all_steps.append(str(s).strip())

                medium = stage_obj.get("medium")
                if medium and str(medium).strip():
                    media_set.add(str(medium).strip().lower())

                for field, target_set in [
                    ("supplements", supplement_set),
                    ("growth_factors", gf_set),
                    ("small_molecules", sm_set),
                ]:
                    items = stage_obj.get(field)
                    if isinstance(items, list):
                        for item in items:
                            if isinstance(item, dict):
                                name = item.get("name")
                                if name and str(name).strip():
                                    target_set.add(str(name).strip().lower())
                            elif isinstance(item, str) and item.strip():
                                target_set.add(item.strip().lower())

        # -- Co-culture structure: {"time_axis": [...], "time_anchors": [...]} --
        time_axis = data.get("time_axis")
        if isinstance(time_axis, list):
            for axis in time_axis:
                if isinstance(axis, dict):
                    branch = axis.get("branch_name", "")
                    if branch:
                        stages.append(str(branch).strip())
                    axes = axis.get("axes")
                    if isinstance(axes, list):
                        for a in axes:
                            if isinstance(a, dict):
                                for anchor_key in ("start_anchor", "end_anchor"):
                                    anchor = a.get(anchor_key, {})
                                    if isinstance(anchor, dict):
                                        name = anchor.get("name", "")
                                        if name:
                                            stages.append(str(name).strip())

        time_anchors_list = data.get("time_anchors", [])
        if isinstance(time_anchors_list, list):
            for anchor in time_anchors_list:
                if isinstance(anchor, dict):
                    desc = anchor.get("desc", "")
                    if desc and str(desc).strip():
                        all_steps.append(str(desc).strip())
                    name_action = anchor.get("name_action", "")
                    if name_action and str(name_action).strip():
                        all_steps.append(str(name_action).strip())

        # Deduplicate while preserving order
        seen = set()
        unique_stages = []
        for s in stages:
            if s.lower() not in seen:
                seen.add(s.lower())
                unique_stages.append(s)

        # Build protocol_text (flattened for full-text search)
        text_parts = []
        if unique_stages:
            text_parts.append("Stages: " + " → ".join(unique_stages))
        if all_steps:
            text_parts.append("Steps: " + "; ".join(all_steps))
        if media_set:
            text_parts.append("Media: " + ", ".join(sorted(media_set)))
        if supplement_set:
            text_parts.append("Supplements: " + ", ".join(sorted(supplement_set)))
        if gf_set:
            text_parts.append("Growth factors: " + ", ".join(sorted(gf_set)))
        if sm_set:
            text_parts.append("Small molecules: " + ", ".join(sorted(sm_set)))

        protocol_text = ". ".join(text_parts)

        # Build human-readable summary
        summary_parts = []
        if unique_stages:
            summary_parts.append(" → ".join(unique_stages[:6]))
            if len(unique_stages) > 6:
                summary_parts[-1] += f" (+{len(unique_stages) - 6} more)"
        media_str = ", ".join(sorted(media_set)[:5])
        if media_str:
            summary_parts.append(f"[{media_str}]")
        supp_str = ", ".join(sorted(supplement_set)[:5])
        if supp_str:
            summary_parts.append(f"supp: {supp_str}")

        summary = "; ".join(summary_parts) if summary_parts else "(empty protocol)"
        if len(summary) > 200:
            summary = summary[:197] + "..."

        # Build dedup key from structured fields (excludes sample-specific metadata)
        dedup_key = {
            "stages": sorted(unique_stages),
            "steps_count": len(all_steps),
            "steps_hash": hashlib.md5(
                "||".join(sorted(all_steps)[:50]).encode()
            ).hexdigest()[:8] if all_steps else "",
            "media": sorted(media_set),
            "supplements": sorted(supplement_set),
            "growth_factors": sorted(gf_set),
            "small_molecules": sorted(sm_set),
        }

        result.update({
            "stages": unique_stages,
            "steps_text": all_steps,
            "media_used": sorted(media_set),
            "supplement_names": sorted(supplement_set),
            "growth_factor_names": sorted(gf_set),
            "small_molecule_names": sorted(sm_set),
            "protocol_text": protocol_text,
            "summary": summary,
            "dedup_key": dedup_key,
        })
        return result

    @staticmethod
    def _make_display_name(props: dict, node_type: str) -> str:
        """Generate a concise human-readable display name for nodes without a 'name' property."""
        # Use existing human-readable fields first
        for key in ("summary", "full_description", "description"):
            val = props.get(key)
            if val and isinstance(val, str) and len(val.strip()) > 0 and not val.strip().startswith("{"):
                return val.strip()[:120]

        if node_type == "OrganoidProfile":
            for k in ("name", "characteristics", "functions", "maturity", "complexity"):
                v = props.get(k)
                if v and str(v).strip():
                    return str(v).strip()[:120]

        return ""

    def _extract_merged_node(self, row: dict, config: dict) -> Optional[dict]:
        """Build a merged entity node from multiple source columns.

        Merges the configured source_columns into a dict, generates a content hash,
        returns node dict. Returns None if all source columns are null/empty.
        """
        merged_data = {}
        all_empty = True
        for col in config["source_columns"]:
            val = row.get(col)
            if val is None or val == '':
                continue
            all_empty = False
            merged_data[col] = val

        if all_empty:
            return None

        node_type = config["node_type"]

        # Build properties based on node type.
        # For CultureProtocol / CoCultureProtocol, extract structured fields FIRST,
        # then use those for dedup hashing (so structurally identical protocols
        # share the same node regardless of sample-specific JSON noise).
        if node_type == "CultureProtocol":
            protocol_raw = merged_data.get("cultivation_protocol")
            time_anchors_raw = merged_data.get("time_anchors")
            material_raw = merged_data.get("cultivation_material_sources")
            structured = self._parse_protocol_json(protocol_raw)

            # Use structured dedup_key for content hash → better dedup
            content_hash = self._content_hash(structured["dedup_key"])
            node_id = f"{config['id_prefix']}_{content_hash}"

            props = {
                "stages": structured["stages"],
                "steps_text": structured["steps_text"],
                "media_used": structured["media_used"],
                "supplement_names": structured["supplement_names"],
                "growth_factor_names": structured["growth_factor_names"],
                "small_molecule_names": structured["small_molecule_names"],
                "protocol_text": structured["protocol_text"],
                "summary": structured["summary"],
                # Keep raw data for provenance / downstream consumers
                "protocol": protocol_raw,
                "time_axes": time_anchors_raw,
                "time_anchors": time_anchors_raw,
                "material_sources": material_raw,
                # culture_days and endpoints merged from former CultureMetrics node
                "culture_days": str(merged_data.get("culture_days", "")).strip() if merged_data.get("culture_days") else None,
                "endpoints": str(merged_data.get("endpoints", "")).strip() if merged_data.get("endpoints") else None,
            }
        elif node_type == "CoCultureProtocol":
            protocol_raw = merged_data.get("coculture_cultivation_protocol")
            structured = self._parse_protocol_json(protocol_raw)

            content_hash = self._content_hash(structured["dedup_key"])
            node_id = f"{config['id_prefix']}_{content_hash}"

            props = {
                "stages": structured["stages"],
                "steps_text": structured["steps_text"],
                "media_used": structured["media_used"],
                "supplement_names": structured["supplement_names"],
                "growth_factor_names": structured["growth_factor_names"],
                "small_molecule_names": structured["small_molecule_names"],
                "protocol_text": structured["protocol_text"],
                "summary": structured["summary"],
                "protocol": protocol_raw,
                "time_axes": merged_data.get("coculture_time_anchors"),
                "time_anchors": merged_data.get("coculture_time_anchors"),
                # material_sources enriched from raw_json in Step B.6
                "material_sources": None,
                # coculture_days, read_out, coculture merged from former CoCultureMetrics node
                "coculture_days": str(merged_data.get("coculture_days", "")).strip() if merged_data.get("coculture_days") else None,
                "read_out": str(merged_data.get("read_out", "")).strip() if merged_data.get("read_out") else None,
                "coculture_description": str(merged_data.get("coculture", "")).strip() if merged_data.get("coculture") else None,
            }
        else:
            # Non-protocol node types: use raw merged_data for content hash (existing behavior)
            content_hash = self._content_hash(merged_data)
            node_id = f"{config['id_prefix']}_{content_hash}"

            if config["extraction_type"] == "profile":
                props = {}
                for col in config["source_columns"]:
                    val = row.get(col)
                    if val is not None and str(val).strip():
                        s = str(val).strip()
                        if col == "organoid":
                            props["name"] = s
                        else:
                            props[col] = s
                props["display_name"] = self._make_display_name(props, node_type)
            else:
                # Generic fallback: store all merged data as properties
                props = {k: v for k, v in merged_data.items()}

        return {
            "id": node_id,
            "type": node_type,
            "properties": props,
            "_provenance": {
                "source_type": "merged_extraction",
                "source_columns": config["source_columns"],
                "extracted_at": datetime.now().isoformat()
            }
        }

    @staticmethod
    def _make_summary(data: dict, max_chars: int = 200) -> str:
        """Generate a human-readable summary from merged data dict"""
        parts = []
        for k, v in data.items():
            if v is None:
                continue
            if isinstance(v, str):
                s = v.strip()
            elif isinstance(v, (dict, list)):
                s = str(v)
            else:
                s = str(v)
            if s:
                parts.append(s)
        summary = "; ".join(parts)
        if len(summary) > max_chars:
            summary = summary[:max_chars - 3] + "..."
        return summary

    def _extract_json_structured_node(self, row_value, config: dict) -> Optional[dict]:
        """Parse a JSON object column and extract structured fields.

        Handles both JSON objects (dict) and JSON arrays (list).
        Dedup is based on content hash of the entire parsed JSON.
        """
        if row_value is None:
            return None

        # Handle empty string / bytes edge case
        if isinstance(row_value, str) and row_value.strip() == '':
            return None

        node_type = config["node_type"]

        # Parse if string or bytes (MySQL may return JSON as bytes in some configurations)
        if isinstance(row_value, bytes):
            try:
                row_value = row_value.decode('utf-8')
            except UnicodeDecodeError:
                return None
        if isinstance(row_value, str):
            try:
                data = json.loads(row_value)
            except json.JSONDecodeError:
                # Not valid JSON — treat as a plain string value.
                # For technique/condition types, wrap into the expected dict shape.
                s = row_value.strip()
                if not s:
                    return None
                if node_type in ("CultureTechnique", "CoCultureTechnique"):
                    data = {"category": None, "subcategory": None,
                            "formation_strategy": None, "full_description": s}
                elif node_type in ("CultureCondition", "CoCultureCondition"):
                    data = {"conditions": [s], "description": s}
                else:
                    return None
            # json.loads may return a list — wrap it into dict
            if isinstance(data, list):
                data = self._wrap_json_array_as_dict(data, node_type)
        elif isinstance(row_value, dict):
            data = row_value
        elif isinstance(row_value, list):
            # JSON array — wrap into dict depending on node type
            data = self._wrap_json_array_as_dict(row_value, node_type)
        else:
            return None

        # Reject empty data or non-dict (after wrapping)
        if not data:
            return None
        if not isinstance(data, dict):
            return None

        content_hash = self._content_hash(data)
        node_id = f"{config['id_prefix']}_{content_hash}"

        # Extract structured fields
        if node_type in ("CultureTechnique", "CoCultureTechnique"):
            full_desc = ", ".join(
                str(v) for v in data.values()
                if v and isinstance(v, str)
            )
            # CultureTechnique: name = subcategory (e.g. "Matrigel embedding")
            # CoCultureTechnique: name = full_description (e.g. "Direct co-culture")
            tech_name = (data.get("subcategory") or full_desc or "").strip() if node_type == "CultureTechnique" else (full_desc or "").strip()
            props = {
                "name": tech_name,
                "category": data.get("category"),
                "subcategory": data.get("subcategory"),
                "formation_strategy": data.get("formation_strategy"),
                "full_description": full_desc,
            }
        elif node_type in ("CultureCondition", "CoCultureCondition"):
            conditions = data.get("conditions", [])
            if isinstance(conditions, list):
                conditions = sorted([str(c) for c in conditions])
            props = {
                "name": ", ".join(conditions) if conditions else "",
                "conditions": conditions if conditions else [],
            }
        else:
            props = {k: v for k, v in data.items()}

        return {
            "id": node_id,
            "type": node_type,
            "properties": props,
            "_provenance": {
                "source_type": "json_structured_extraction",
                "source_column": config.get("_source_column", ""),
                "extracted_at": datetime.now().isoformat()
            }
        }

    @staticmethod
    def _wrap_json_array_as_dict(data: list, node_type: str) -> dict:
        """Wrap a JSON array into a dict suitable for structured extraction.

        Args:
            data: The parsed JSON array (list)
            node_type: The target node type (CultureCondition, CoCultureTechnique, etc.)

        Returns:
            A dict with appropriate keys for the node type
        """
        if not data:
            return {}

        # For condition-like types: wrap as {"conditions": [...]}
        if node_type in ("CultureCondition", "CoCultureCondition"):
            return {"conditions": data, "description": ", ".join(str(v) for v in data if v)}

        # For technique-like types: extract strings into a flat description
        if node_type in ("CultureTechnique", "CoCultureTechnique"):
            # Flatten nested structures and collect string values
            flat_values = []
            for item in data:
                if isinstance(item, str):
                    flat_values.append(item)
                elif isinstance(item, dict):
                    flat_values.extend(
                        str(v) for v in item.values() if v and isinstance(v, str)
                    )
            return {
                "category": None,
                "subcategory": None,
                "formation_strategy": None,
                "full_description": ", ".join(flat_values) if flat_values else str(data),
            }

        # Generic fallback
        return {"items": data}

    # omics_subtype 归一到 Platform 节点名。仅这三个取值——与源库 platform 列的
    # 取值域一致，不新造 Platform 节点类型。（2026-08-03 Omics 层修复）
    OMICS_SUBTYPE_TO_PLATFORM = {
        "rna-seq": "RNA-seq", "bulk rna-seq": "RNA-seq",
        "bulk rna sequencing": "RNA-seq", "rna sequencing": "RNA-seq",
        "mrna-seq": "RNA-seq", "total rna-seq": "RNA-seq",
        "scrna-seq": "scRNA-seq", "single-cell rna-seq": "scRNA-seq",
        "single cell rna-seq": "scRNA-seq", "sc-rna-seq": "scRNA-seq",
        "microarray": "Microarray", "mirna microarray": "Microarray",
    }

    @staticmethod
    def _analyzed_accessions(row) -> set:
        """该样本被 ORBIT 实际重分析的数据集登记号（小写）。

        omics_id 列列出的是样本引用的全部数据集；accession 与 omics_datasets 列
        才标明哪个是自己的。用于给 HAS_OMICS 打 role。

        注意 accession 列可能把多个登记号连写在一格，例如
            "GSE249801;GSE249802;GSE308654"
            "GSE230848;GSE230848"        （同一个重复两次）
        必须按分隔符拆开。整串当成一个登记号会建出
        omics_id="GSE249801;GSE249802;GSE308654" 这样的垃圾节点。
        """
        junk = {"", "not specified", "none", "na", "n/a", "-", "null", "unknown",
                "not applicable", "not provided", "not available"}
        out = set()
        for part in str(row.get("accession") or "").replace(";", ",").split(","):
            p = part.strip()
            if p and p.lower() not in junk:
                out.add(p.lower())
        raw_ds = row.get("omics_datasets")
        if raw_ds:
            try:
                parsed = json.loads(raw_ds) if isinstance(raw_ds, str) else raw_ds
                for d in (parsed if isinstance(parsed, list) else [parsed]):
                    if isinstance(d, dict):
                        for part in str(d.get("gse_id") or "").replace(
                                ";", ",").split(","):
                            p = part.strip()
                            if p and p.lower() not in junk:
                                out.add(p.lower())
            except (TypeError, ValueError):
                pass
        return out


    # 组学数据集登记号中的占位/垃圾值，一律不建节点。
    OMICS_JUNK_VALUES = {"", "not specified", "none", "na", "n/a", "-", "null",
                         "unknown", "not applicable", "not provided",
                         "not available"}

    def _build_omics_layer(self, all_rows, entity_registry, sample_entities):
        """整层生成 Omics 节点与 HAS_OMICS / USES_PLATFORM 边。

        为什么不放在通用的宽表 JSON 循环里：
          1) 同一个数据集会被多个样本以不同的 omics_subtype 引用，节点属性需要跨样本
             聚合；通用循环只在节点首次创建时写属性，后来的引用会被丢掉。
          2) omics_id 列里有 "not specified" 之类的占位值，不应该建成节点——否则所有
             这类样本会共享同一个无意义节点。
          3) 样本被 ORBIT 实际重分析的数据集记在 accession / omics_datasets 列，
             有一部分并未出现在 omics_id 列里；只读 omics_id 会漏掉这些边。
          4) HAS_OMICS 需要 role 区分「样本自己的数据集」和「论文引用的数据集」，
             判定要同时用到 omics_id 与 accession / omics_datasets 三列。

        节点键为数据集登记号（不是仓库名）。此前按仓库名去重，导致全库只有 206 个
        Omics 节点、GEO 一个节点被 2,655 个样本共用，任何 GEO 来源的样本都会返回
        同一个错误的登记号。
        """
        def _clean(v):
            s = str(v if v is not None else "").strip()
            return "" if s.lower() in self.OMICS_JUNK_VALUES else s

        def _join(vals):
            return "; ".join(sorted(v for v in vals if v))

        node_db, node_type, node_sub = {}, {}, {}
        node_analyzed, display = {}, {}
        edge_role = {}

        for row in all_rows:
            sample_id_raw = str(row.get("sample_id") or "").strip()
            if not sample_id_raw:
                continue
            sample_id = f"smp_{sample_id_raw}"
            analyzed = self._analyzed_accessions(row)

            # (a) omics_id 列：样本引用的全部数据集
            items = self._parse_json_column(row.get("omics_id")) or []
            for it in items:
                if not isinstance(it, dict):
                    continue
                acc = _clean(it.get("omics_id"))
                if not acc:
                    continue
                key = acc.lower()
                display.setdefault(key, {})
                display[key][acc] = display[key].get(acc, 0) + 1
                node_db.setdefault(key, set()).add(_clean(it.get("db")))
                node_type.setdefault(key, set()).add(_clean(it.get("omics_type")))
                node_sub.setdefault(key, set()).add(_clean(it.get("omics_subtype")))
                if key in analyzed:
                    node_analyzed[key] = True
                role = "analyzed" if key in analyzed else "cited"
                if edge_role.get((sample_id, key)) != "analyzed":
                    edge_role[(sample_id, key)] = role

            # (b) accession / omics_datasets 列：ORBIT 实际重分析的数据集。
            #     其中一部分不在 omics_id 列里，必须在此补回。
            ds_meta = {}
            raw_ds = row.get("omics_datasets")
            if raw_ds:
                try:
                    parsed = json.loads(raw_ds) if isinstance(raw_ds, str) else raw_ds
                    for d in (parsed if isinstance(parsed, list) else [parsed]):
                        if isinstance(d, dict) and _clean(d.get("gse_id")):
                            ds_meta.setdefault(
                                _clean(d["gse_id"]).lower(), []).append(d)
                except (TypeError, ValueError):
                    pass
            for key in analyzed:
                disp = key
                for d in ds_meta.get(key, []):
                    disp = _clean(d.get("gse_id")) or disp
                if not ds_meta.get(key):
                    # accession 列可能连写多个登记号，逐个比对取原始拼写
                    for _p in str(row.get("accession") or "").replace(
                            ";", ",").split(","):
                        if _p.strip().lower() == key:
                            disp = _p.strip()
                            break
                display.setdefault(key, {})
                display[key][disp] = display[key].get(disp, 0) + 1
                node_analyzed[key] = True
                node_db.setdefault(key, set()).add(
                    "GEO" if key.startswith("gse") else "")
                node_type.setdefault(key, set())
                node_sub.setdefault(key, set())
                for d in ds_meta.get(key, []):
                    node_type[key].add(_clean(d.get("omics_type")))
                    node_sub[key].add(_clean(d.get("platform")))
                if not ds_meta.get(key) and _clean(row.get("platform")):
                    node_sub[key].add(_clean(row.get("platform")))
                edge_role[(sample_id, key)] = "analyzed"

        # ---- 组装节点 ----
        out_nodes, out_edges = [], []
        nid_of = {}
        for key in node_db:
            name = max(display[key].items(), key=lambda kv: (kv[1], kv[0]))[0]
            node, is_new = self._get_or_create_entity(
                entity_registry, "Omics", "omc", name)
            nid_of[key] = node["id"]
            node["properties"].update({
                "name": name,
                "omics_id": name,
                "db": _join(node_db[key]),
                "omics_type": _join(node_type[key]),
                "omics_subtype": _join(node_sub[key]),
                "is_analyzed": bool(node_analyzed.get(key)),
            })
            plats = sorted({self.OMICS_SUBTYPE_TO_PLATFORM[s.lower()]
                            for s in node_sub[key]
                            if s and s.lower() in self.OMICS_SUBTYPE_TO_PLATFORM})
            if len(plats) == 1:
                node["properties"]["platform"] = plats[0]
            if is_new:
                out_nodes.append(node)
            # Omics → Platform：按该数据集自己的 omics_subtype
            for pname in plats:
                pnode = self._extract_platform_node(pname)
                if not pnode:
                    continue
                pkey = ("Platform", pnode["id"])
                if pkey not in entity_registry:
                    entity_registry[pkey] = pnode
                    out_nodes.append(pnode)
                pe = self._make_edge(node["id"], pnode["id"], "USES_PLATFORM",
                                     {"source_column": "omics_id.omics_subtype"})
                if pe:
                    out_edges.append(pe)

        # ---- 组装 HAS_OMICS ----
        for (sample_id, key), role in sorted(edge_role.items()):
            tgt = nid_of.get(key)
            if not tgt:
                continue
            e = self._make_edge(sample_id, tgt, "HAS_OMICS",
                                {"source_column": "omics_id", "context": "primary",
                                 "role": role})
            if e:
                out_edges.append(e)
            sample_entities[sample_id]["Omics"].add(tgt)

        return out_nodes, out_edges

    def _extract_platform_node(self, platform_value: str) -> Optional[dict]:
        """Create a Platform node from the normalized platform name."""
        if not platform_value or not str(platform_value).strip():
            return None

        name = str(platform_value).strip()
        name_hash = hashlib.md5(name.lower().encode('utf-8')).hexdigest()[:8]
        node_id = f"pla_{name_hash}"

        return {
            "id": node_id,
            "type": "Platform",
            "properties": {"name": name},
            "_provenance": {
                "source_type": "column_extraction",
                "source_column": "platform",
                "extracted_at": datetime.now().isoformat()
            }
        }

    # -------------------------------------------------------------------------
    # Helper methods
    # -------------------------------------------------------------------------

    # -------------------------------------------------------------------------
    # Output
    # -------------------------------------------------------------------------

    def save_json(self, nodes: List[dict], edges: List[dict], output_path: str):
        """Save as JSON format knowledge graph"""
        node_types = sorted(set(n["type"] for n in nodes))
        relation_types = sorted(set(e["relation"] for e in edges))

        kg = {
            "meta": {
                "name": f"Organoid Culture Knowledge Graph",
                "version": "3.0",
                "created": datetime.now().isoformat(),
                "source_database": self.connection_params["database"],
                "description": "Knowledge graph of organoid culture experiments built from MySQL database",
                "node_types": node_types,
                "relationship_types": relation_types,
                "statistics": {
                    "total_nodes": len(nodes),
                    "total_edges": len(edges),
                    "nodes_by_type": self._count_by_key(nodes, "type"),
                    "edges_by_relation": self._count_by_key(edges, "relation")
                }
            },
            "nodes": nodes,
            "edges": edges
        }

        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)
        with open(output_path, 'w', encoding='utf-8') as f:
            json.dump(kg, f, ensure_ascii=False, indent=2)

        print(f"[OK] JSON knowledge graph saved to: {output_path}")
        print(f"     Nodes: {len(nodes)}, Edges: {len(edges)}")
        self._print_statistics(nodes, edges)

    def save_sqlite(self, nodes: List[dict], edges: List[dict], output_path: str):
        """Save as SQLite format knowledge graph"""
        os.makedirs(os.path.dirname(output_path) or ".", exist_ok=True)

        conn = sqlite3.connect(output_path)
        cursor = conn.cursor()

        # Create nodes table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS nodes (
                id TEXT PRIMARY KEY,
                type TEXT NOT NULL,
                properties TEXT NOT NULL
            )
        """)

        # Create edges table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS edges (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                edge_id TEXT,
                source TEXT NOT NULL,
                target TEXT NOT NULL,
                relation TEXT NOT NULL,
                properties TEXT,
                FOREIGN KEY (source) REFERENCES nodes(id),
                FOREIGN KEY (target) REFERENCES nodes(id)
            )
        """)

        # Create metadata table
        cursor.execute("""
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT
            )
        """)

        # Create indexes
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_nodes_type ON nodes(type)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_source ON edges(source)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_target ON edges(target)")
        cursor.execute("CREATE INDEX IF NOT EXISTS idx_edges_relation ON edges(relation)")

        # Insert nodes
        for node in nodes:
            cursor.execute(
                "INSERT OR REPLACE INTO nodes (id, type, properties) VALUES (?, ?, ?)",
                (node["id"], node["type"], json.dumps(node.get("properties", {}), ensure_ascii=False))
            )

        # Insert edges
        for i, edge in enumerate(edges):
            cursor.execute(
                "INSERT INTO edges (edge_id, source, target, relation, properties) VALUES (?, ?, ?, ?, ?)",
                (
                    edge.get("id", f"edge_{i}"),
                    edge["source"],
                    edge["target"],
                    edge["relation"],
                    json.dumps(edge.get("properties", {}), ensure_ascii=False)
                )
            )

        # Insert metadata
        meta = {
            "name": "Organoid Culture Knowledge Graph",
            "version": "3.0",
            "created": datetime.now().isoformat(),
            "source_database": self.connection_params["database"],
            "total_nodes": len(nodes),
            "total_edges": len(edges),
        }
        for key, val in meta.items():
            cursor.execute("INSERT OR REPLACE INTO meta (key, value) VALUES (?, ?)", (key, str(val)))

        conn.commit()
        conn.close()

        print(f"[OK] SQLite knowledge graph saved to: {output_path}")
        print(f"     Nodes: {len(nodes)}, Edges: {len(edges)}")

    @staticmethod
    def _count_by_key(items: List[dict], key: str) -> dict:
        counts = defaultdict(int)
        for item in items:
            counts[item.get(key, "unknown")] += 1
        return dict(sorted(counts.items(), key=lambda x: x[1], reverse=True))

    @staticmethod
    def _print_statistics(nodes: List[dict], edges: List[dict]):
        print(f"\n{'='*50}")
        print(f"  Knowledge Graph Statistics")
        print(f"{'='*50}")
        print(f"  Total nodes: {len(nodes)}")
        print(f"  Total edges: {len(edges)}")
        print(f"\n  Nodes by type:")
        for ntype, count in MySQLKnowledgeGraphBuilder._count_by_key(nodes, "type").items():
            print(f"    {ntype:20s}: {count:>6d}")
        print(f"\n  Edges by relation:")
        for rel, count in MySQLKnowledgeGraphBuilder._count_by_key(edges, "relation").items():
            print(f"    {rel:25s}: {count:>6d}")


# =============================================================================
# Main CLI
# =============================================================================

def main():
    global GROUPINFO_SUB_NODE_MAX_ROWS, GENE_ANNOTATION_MAX_ROWS, MATERIAL_INFO_MAX_ITEMS
    global STRING_PARTNERS_PER_GENE

    parser = argparse.ArgumentParser(
        description="Build knowledge graph from MySQL organoid culture database",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Explore database structure
  python build_kg.py --host localhost --database organoid_db --user root --password xxx --explore

  # Build knowledge graph (JSON + SQLite)
  python build_kg.py --host localhost --database organoid_db --user root --password xxx --output-dir ./output

  # Build JSON only
  python build_kg.py --host localhost --database organoid_db --user root --password xxx --format json
        """
    )

    # MySQL connection
    parser.add_argument("--host", default="192.168.20.30", help="MySQL host (default:)")
    parser.add_argument("--port", type=int, default=33061, help="MySQL port (default: 33061)")
    parser.add_argument("--user", default="wangchenglong", help="MySQL user")
    parser.add_argument("--password", default="", help="MySQL password")
    parser.add_argument("--database", default='public', help="MySQL database name (default: public)")

    # Operation mode
    parser.add_argument("--explore", action="store_true", help="Explore database schema and exit")

    # Build options
    parser.add_argument("--output-dir", default="./organoid-kg-output", help="Output directory")
    parser.add_argument("--format", nargs="+", default=["json", "sqlite"],
                        choices=["json", "sqlite"], help="Output formats (default: json sqlite)")
    parser.add_argument("--tables", nargs="*", default=None,
                        help="Only process tables whose names contain these strings (substring match). "
                             "e.g. --tables organoid culture. If not set, uses FOCUS_TABLES from script config.")
    parser.add_argument("--mapping", default=None,
                        help="Path to custom KG mapping JSON file. "
                             "Default: scripts/kg_mapping.json")
    parser.add_argument("--groupinfo-max-rows", type=int, default=GROUPINFO_SUB_NODE_MAX_ROWS,
                        help=f"Max rows per GSE_ID for GroupInfo sub-nodes "
                             f"(GroupDEGs/IntraClusterDEGs/ClusterMarkers/GSVA/GSEA). "
                             f"Default: {GROUPINFO_SUB_NODE_MAX_ROWS}. "
                             f"Set to 0 for unlimited.")
    parser.add_argument("--gene-annotation-max-rows", type=int, default=GENE_ANNOTATION_MAX_ROWS,
                        help=f"Max rows per batch for gene annotation table queries "
                             f"(STRING, CRISPick, gene_annotation, site, disease). "
                             f"Default: {GENE_ANNOTATION_MAX_ROWS}. "
                             f"Set to 0 for unlimited.")
    parser.add_argument("--string-partners-per-gene", type=int, default=STRING_PARTNERS_PER_GENE,
                        help=f"Max STRING partners retained per gene, ranked by combined_score "
                             f"after deduplication. Default: {STRING_PARTNERS_PER_GENE}. "
                             f"Set to 0 for unlimited.")
    parser.add_argument("--material-max-items", type=int, default=MATERIAL_INFO_MAX_ITEMS,
                        help=f"Max material items to extract per protocol for MaterialInfo nodes. "
                             f"Default: {MATERIAL_INFO_MAX_ITEMS}. "
                             f"Set to 0 for unlimited.")

    args = parser.parse_args()

    # Focus tables: CLI argument > script config
    focus_tables = args.tables if args.tables is not None else None

    # Override module-level truncation limits
    if args.groupinfo_max_rows == 0:
        GROUPINFO_SUB_NODE_MAX_ROWS = None  # 0 means unlimited
    else:
        GROUPINFO_SUB_NODE_MAX_ROWS = args.groupinfo_max_rows
    if args.gene_annotation_max_rows == 0:
        GENE_ANNOTATION_MAX_ROWS = None  # 0 means unlimited
    else:
        GENE_ANNOTATION_MAX_ROWS = args.gene_annotation_max_rows
    if args.material_max_items == 0:
        MATERIAL_INFO_MAX_ITEMS = None  # 0 means unlimited
    else:
        MATERIAL_INFO_MAX_ITEMS = args.material_max_items
    if args.string_partners_per_gene == 0:
        STRING_PARTNERS_PER_GENE = None  # 0 means unlimited
    else:
        STRING_PARTNERS_PER_GENE = args.string_partners_per_gene

    # --explore mode
    if args.explore:
        builder = MySQLKnowledgeGraphBuilder(
            host=args.host, port=args.port, user=args.user,
            password=args.password, database=args.database,
            focus_tables=focus_tables,
            mapping_path=args.mapping,
        )
        schema = builder.explore()
        builder.print_explore(schema)
        return

    builder = MySQLKnowledgeGraphBuilder(
        host=args.host, port=args.port, user=args.user,
        password=args.password, database=args.database,
        focus_tables=focus_tables,
        mapping_path=args.mapping,
    )

    # Build mode
    print(f"\nBuilding knowledge graph from {args.database}...")
    nodes, edges = builder.build()

    if not nodes:
        print("[WARNING] No nodes were extracted. Check your database connection and data.")
        return

    # Output — write to timestamped subdirectory
    timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
    output_subdir = os.path.join(args.output_dir, timestamp)
    os.makedirs(output_subdir, exist_ok=True)

    if "json" in args.format:
        json_path = os.path.join(output_subdir, "organoid_kg.json")
        builder.save_json(nodes, edges, json_path)

    if "sqlite" in args.format:
        sqlite_path = os.path.join(output_subdir, "organoid_kg.sqlite")
        builder.save_sqlite(nodes, edges, sqlite_path)

    # Build report
    report = {
        "build_time": datetime.now().isoformat(),
        "source_database": args.database,
        "output_dir": output_subdir,
        "statistics": {
            "total_nodes": len(nodes),
            "total_edges": len(edges),
            "nodes_by_type": builder._count_by_key(nodes, "type"),
            "edges_by_relation": builder._count_by_key(edges, "relation")
        }
    }
    report_path = os.path.join(output_subdir, "build_report.json")
    with open(report_path, 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=2)
    print(f"[OK] Build report saved to: {report_path}")

    print(f"\n{'='*50}")
    print(f"  Build complete!")
    print(f"{'='*50}")


if __name__ == "__main__":
    main()

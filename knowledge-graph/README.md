# ORBIT knowledge graph

Build, load and query the sample-centric organoid knowledge graph used by ORBIT.

One module of the [ORBIT Resource](https://github.com/chenjiang-bio/ORBIT-resource). The live graph powers the portal assistant, Skills follow-up and the manuscript case studies (e.g. `KM-14955`).

[![Python](https://img.shields.io/badge/python-3.9%2B-blue.svg)](https://github.com/chenjiang-bio/ORBIT-resource/tree/main/knowledge-graph)
[![License](https://img.shields.io/badge/license-MIT-blue.svg)](../LICENSE)

## What this module contains

| Path | Role |
|------|------|
| [`config/kg_mapping.json`](config/kg_mapping.json) | Table / column → node / edge mapping (edit this, not the builder logic) |
| [`scripts/build_kg.py`](scripts/build_kg.py) | MySQL → SQLite / JSON graph export |
| [`scripts/import_sqlite_to_neo4j.py`](scripts/import_sqlite_to_neo4j.py) | Streaming SQLite → Neo4j import |
| [`orbit_kg/`](orbit_kg/) | Small Python client for Neo4j or offline fixtures |
| [`docs/SCHEMA.md`](docs/SCHEMA.md) | Schema notes for writers and tool designers |
| [`docs/BUILD.md`](docs/BUILD.md) | Build and load instructions |
| [`examples/`](examples/) | Cypher, fixtures and a joint LLM + KG notebook |

Large SQLite dumps and Neo4j stores are **not** in git (multi‑GB). The current release export (4,557,354 nodes / 6,816,859 relationships) is archived at [Zenodo (DOI: 10.5281/zenodo.21920709)](https://doi.org/10.5281/zenodo.21920709).

## Install

```bash
git clone https://github.com/chenjiang-bio/ORBIT-resource.git
cd ORBIT-resource/knowledge-graph

python -m venv .venv
source .venv/bin/activate
pip install -e ".[mysql,notebook]"
cp .env.example .env
```

## Quick start (offline joint LLM + KG example)

No Neo4j or API key required:

```bash
jupyter notebook examples/llm_kg_joint_reasoning.ipynb
```

The notebook runs a **joint** loop: a planner selects graph tools → the ORBIT KG (fixtures) returns identifier-linked rows → a synthesizer writes an answer that keeps sample / gene / reagent IDs. This mirrors the manuscript Prader-Willi trace (`KM-14955`).

Optional live backends:

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_PASSWORD=...
export OPENAI_API_KEY=...   # optional
```

## Build a release graph

See [`docs/BUILD.md`](docs/BUILD.md). Short form:

```bash
python scripts/build_kg.py \
  --host "$ORBIT_MYSQL_HOST" --user "$ORBIT_MYSQL_USER" \
  --password "$ORBIT_MYSQL_PASSWORD" --database "$ORBIT_MYSQL_DATABASE" \
  --mapping config/kg_mapping.json \
  --output-dir output --format sqlite

python scripts/import_sqlite_to_neo4j.py \
  --sqlite output/organoid_kg.sqlite \
  --uri "$NEO4J_URI" --user "$NEO4J_USER" --password "$NEO4J_PASSWORD" \
  --clear
```

## Schema (short)

```text
Sample ─HAS_PHENOTYPE──► Phenotype
Sample ─HAS_GROUP_INFO─► GroupInfo ─HAS_CLUSTER_MARKER─► ClusterMarkers
                                              └─HAS_GENE_ANNOTATION─► GeneAnnotation
Sample ─HAS_OMICS──────► Omics (role = analyzed | cited)
```

Full notes: [`docs/SCHEMA.md`](docs/SCHEMA.md).

## Tests

```bash
pip install -e ".[dev]"
pytest -q
```

## License

MIT — see the repository root [`LICENSE`](../LICENSE).

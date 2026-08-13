# Building and loading the ORBIT knowledge graph

## Prerequisites

- Python 3.9+
- Read access to the ORBIT MySQL snapshot used for a release build
- Optional: Neo4j 5.x with enough disk for ~5M nodes / ~7M relationships
- Optional: `APOC` if you prefer CSV bulk load helpers in `scripts/`

```bash
cd knowledge-graph
python -m venv .venv
source .venv/bin/activate
pip install -e ".[mysql]"
cp .env.example .env   # fill MySQL / Neo4j credentials locally
```

## 1. Configure the mapping

All table→node and column→edge rules live in:

```text
config/kg_mapping.json
```

The builder loads this file at runtime (`--mapping` overrides the default path). Core samples are restricted to `is_organoid='yes'` (11,890 records in the manuscript release).

## 2. Build SQLite / JSON exports from MySQL

```bash
python scripts/build_kg.py \
  --host "$ORBIT_MYSQL_HOST" \
  --port "$ORBIT_MYSQL_PORT" \
  --user "$ORBIT_MYSQL_USER" \
  --password "$ORBIT_MYSQL_PASSWORD" \
  --database "$ORBIT_MYSQL_DATABASE" \
  --mapping config/kg_mapping.json \
  --output-dir output \
  --format sqlite
```

Useful flags:

| Flag | Purpose |
|------|---------|
| `--explore` | Print discovered tables / columns and exit |
| `--format sqlite` | Offline SQL export (preferred for Neo4j import) |
| `--format json` | Universal JSON dump (very large; avoid on modest machines) |

The SQLite artifact is typically multi-gigabyte and must **not** be committed to git. Document its SHA-256 in the release notes instead.

## 3. Import SQLite into Neo4j

`scripts/import_sqlite_to_neo4j.py` streams nodes and edges in batches so a 20 GB JSON dump is not required.

```bash
python scripts/import_sqlite_to_neo4j.py \
  --sqlite output/organoid_kg.sqlite \
  --uri "$NEO4J_URI" \
  --user "$NEO4J_USER" \
  --password "$NEO4J_PASSWORD" \
  --clear \
  --batch-size 5000
```

`--clear` runs only after the driver connects and the SQLite file passes a basic integrity check. Omit `--clear` for resumable `MERGE` imports.

## 4. Smoke-check

```cypher
MATCH (n) RETURN count(n) AS nodes;
MATCH ()-[r]->() RETURN count(r) AS relationships;
MATCH (s:Sample {sample_id:'KM-14955'}) RETURN s.sample_id, s.year;
```

For the manuscript case-study queries, see [`examples/cypher/`](../examples/cypher/) and the offline fixture notebook under [`examples/`](../examples/).

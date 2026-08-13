# Examples

| Path | Description |
|------|-------------|
| [`llm_kg_joint_reasoning.ipynb`](llm_kg_joint_reasoning.ipynb) | Joint LLM + knowledge-graph tool loop on `KM-14955` (offline fixtures by default) |
| [`cypher/pws_km14955.cypher`](cypher/pws_km14955.cypher) | Manuscript Cypher queries for the Prader-Willi case |
| [`fixtures/pws_km14955.json`](fixtures/pws_km14955.json) | Recorded rows matching the Data S5 trace |

## Run the notebook offline

```bash
cd knowledge-graph
pip install -e ".[notebook]"
jupyter notebook examples/llm_kg_joint_reasoning.ipynb
```

## Optional live backends

```bash
export NEO4J_URI=bolt://localhost:7687
export NEO4J_USER=neo4j
export NEO4J_PASSWORD=...
export OPENAI_API_KEY=...   # optional; omit to keep the deterministic planner/synthesizer
```

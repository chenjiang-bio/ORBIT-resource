# ORBIT Organoid Resource

Source code and analysis workflows for ORBIT, a sample-level organoid knowledge resource linking experimental context with molecular evidence across 80 organs and 38 species.

Online platform: https://db-orbit.com/

## Repository layout

| Directory | Description |
|-----------|-------------|
| [`knowledge-graph/`](knowledge-graph/) | Knowledge-graph construction (MySQL → SQLite / Neo4j), query client, and examples |
| [`signature-prioritization/`](signature-prioritization/) | OCSP: context-guided candidate prioritization against organoid pathway references |
| [`omics-pipeline/`](omics-pipeline/) | Transcriptomic processing pipeline |
| [`literature-agent/`](literature-agent/) | LAMPS multi-agent extraction framework |
| [`literature-mcp/`](literature-mcp/) | MCP services for literature extraction |

## Data

The ORBIT knowledge graph (4,557,354 nodes / 6,816,859 relationships) is archived at [Zenodo (DOI: 10.5281/zenodo.21920709)](https://doi.org/10.5281/zenodo.21920709).

## Getting started

```bash
git clone https://github.com/chenjiang-bio/ORBIT-resource.git
cd ORBIT-resource
```

Each subdirectory has its own README with install and usage instructions.

## License

[MIT](LICENSE)

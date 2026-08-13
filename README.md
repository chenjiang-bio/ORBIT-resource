# ORBIT Resource

Code and reproducible workflows for **ORBIT**, an organoid knowledge and analysis resource. Each top-level subdirectory is a self-contained module.

## Repository layout

| Directory | Description |
|-----------|-------------|
| [`omics-pipeline/`](omics-pipeline/) | Multi-omics analysis pipeline for organoid transcriptomic and related datasets |
| [`literature-agent/`](literature-agent/) | Multi-agent system for structured extraction from organoid literature |
| [`knowledge-graph/`](knowledge-graph/) | Knowledge-graph construction, schema, and query utilities |
| [`literature-mcp/`](literature-mcp/) | MCP services used by the literature extraction agent |
| [`signature-prioritization/`](signature-prioritization/) | OCSP: context-guided prioritization of candidate genes against condition-specific organoid pathway backgrounds |

## Getting started

```bash
git clone https://github.com/chenjiang-bio/ORBIT-resource.git
cd ORBIT-resource
```

Open the module subdirectory you need and follow its `README.md`.

## Notes

- Keep module code, configs, and docs inside the corresponding subdirectory.
- Prefer English for public documentation and commit messages.
- Do not commit secrets (API keys, credentials, or `.env` files with private tokens).

## License

This project is released under the [MIT License](LICENSE).

# Wikipedia & Bio-database Search MCP Service

A multi-source search service built on FastMCP, providing query tools for Wikipedia, NCBI GEO, and NCBI Gene databases.

## Overview

This project implements an MCP (Model Context Protocol) service with the following capabilities:

### Wikipedia search
- `get_wikipedia_summary` - get page summary
- `get_wikipedia_categories` - get page categories
- `get_wikipedia_text` - get full page text
- `get_wikipedia_links` - get outgoing links from a page
- `get_wikipedia_backlinks` - get incoming links to a page

### NCBI GEO database search
- `get_gse_summary` - get GSE dataset summary
- `get_gse_full_info` - get full GSE dataset information

### NCBI Gene database search
- `get_gene_summary` - get gene summary
- `get_gene_full_info` - get full gene information

All query results are cached locally to avoid repeated requests.

## Installation

### Requirements
- Python 3.8+
- pip

### Install dependencies

```bash
pip install fastmcp wikipediaapi requests
```

Or use requirements.txt:

```bash
pip install -r requirements.txt
```

## Starting the service

### 1. Start the MCP server

From the project root:

```bash
fastmcp run search_mcp.py:mcp --transport http --port 12400
```

The service listens at `http://localhost:12400/mcp` by default.

### 2. Service logs

After startup you will see:
- Registered tools
- Listen address
- Request handling logs

## MCP client tests

### 1. Run the test script

```bash
cd search_mcp
python search_mcpclient.py
```

### 2. What is tested

The script runs the following tests in order:

#### Wikipedia search tests
```python
# get summary
await client.call_tool("get_wikipedia_summary", {
    "keywords": ["Organoid", "Cancer"]
})

# get categories
await client.call_tool("get_wikipedia_categories", {
    "keywords": ["Cell"]
})

# get full text
await client.call_tool("get_wikipedia_text", {
    "keywords": ["Organoid"]
})

# get links
await client.call_tool("get_wikipedia_links", {
    "keywords": ["Homo sapiens", "Mus musculus"]
})

# get backlinks
await client.call_tool("get_wikipedia_backlinks", {
    "keywords": ["Model Context Protocol", "HTTP"]
})
```

#### GEO database tests
```python
# get GSE summary
await client.call_tool("get_gse_summary", {
    "gse_ids": ["GSE14594", "GSE28265"]
})

# get full GSE info
await client.call_tool("get_gse_full_info", {
    "gse_ids": ["GSE14594"]
})
```

#### Gene database tests
```python
# get gene summary
await client.call_tool("get_gene_summary", {
    "querys": [
        {"gene_name": "TP53", "organism": "Homo sapiens"},
        {"gene_name": "LGR5", "organism": "Homo sapiens"}
    ]
})

# get full gene info
await client.call_tool("get_gene_full_info", {
    "querys": [
        {"gene_name": "TP53", "organism": "Mus musculus"}
    ]
})
```

## Usage examples

### From Python

```python
import asyncio
from fastmcp import Client

client = Client("http://localhost:12400/mcp")

async def search_example():
    async with client:
        # query Wikipedia
        result = await client.call_tool("get_wikipedia_summary", {
            "keywords": ["Machine Learning"]
        })
        print(result.content)
        
        # query gene info
        result = await client.call_tool("get_gene_summary", {
            "querys": [
                {"gene_name": "BRCA1", "organism": "Homo sapiens"}
            ]
        })
        print(result.content)

asyncio.run(search_example())
```

### Cache

All query results are cached under `./cache`:
- Wikipedia: `wiki_{keyword}.json`
- GEO: `{gse_id}.json`
- Gene: `gene_{organism}_{gene_name}.json`

To clear the cache, delete the corresponding cache files.

## Response format

All tools return JSON:

### Wikipedia tools
```json
{
  "keyword1": "content...",
  "keyword2": "content...",
  "keyword3": "not found, need to check"
}
```

### GEO tools
```json
{
  "GSE12345": ["ID: GSE12345", "Title: ...", "Summary: ..."],
  "GSE67890": "not found, need to check"
}
```

### Gene tools
```json
[
  {
    "gene_name": "TP53",
    "organism": "Homo sapiens",
    "summary": ["Gene ID: 7157", "Symbol: TP53", ...]
  }
]
```

## Project structure

```
embeddingmcp/
├── search_mcp/
│   ├── search_mcp.py           # MCP service entry
│   ├── search_mcpclient.py     # test client
│   └── calling/
│       ├── wiki_search.py      # Wikipedia search implementation
│       ├── gse_search.py       # GEO database search implementation
│       ├── gene_search.py      # Gene database search implementation
│       ├── wiki.env            # fuzzy wiki match via OpenAI-compatible Embedding API:
│       │                       # EMBEDDING_BASE_URL=http://xxxxxxxx:xxxxx
│       │                       # EMBEDDING_API_KEY=your_embedding_api_key_here
│       │                       # EMBEDDING_MODEL=text-embedding-3-small
│       ├── google_search.py    # Google search implementation
│       └── google.env          # Google CSE credentials:
│                               # GOOGLE_SEARCH_KEY=your_google_search_key_here
│                               # GOOGLE_SEARCH_CX=your_google_cx_here
└── cache/                      # cache directory
```

## Notes

1. The first query for a given key is slower because data is fetched from the network
2. Cache files persist on disk; clean them periodically to free space
3. Ensure network access to Wikipedia and NCBI services
4. Gene queries require both gene name and organism
5. GSE IDs may be provided with or without the `GSE` prefix

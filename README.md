# Silvaco Handbook MCP

[中文文档](README.zh-CN.md)

An [MCP](https://modelcontextprotocol.io/) (Model Context Protocol) server that
gives AI assistants fast, bounded access to the **Silvaco TCAD manuals** and the
**official Deckbuild example decks** — without dumping thousands of PDF pages
into the conversation context.

It indexes MinerU-converted Markdown of the manuals into a local SQLite FTS5
database and exposes context-friendly search/read tools over stdio. Each manual
section carries its original PDF page range, so results can be cross-referenced
with the source PDFs.

## Features

- **Handbook corpus** — section-level index of the Silvaco manuals
  (Deckbuild, Victory Device, Victory Process, ...) with original PDF page
  ranges preserved.
- **Examples corpus** — full-text index of the official `.in` example decks
  from your Silvaco installation (`examples/deckbuild/<version>`).
- **Bounded output** — every tool has hard caps (search ≤ 30 hits,
  read ≤ 20 000 chars, paged via `offset`) so the LLM context stays small.
- **Incremental indexing** — the index is rebuilt lazily on startup whenever a
  Markdown file or example deck changes (size/mtime fingerprint).

## Tools

| Tool | Description |
|---|---|
| `handbook_list_manuals` | List indexed manuals with section counts and source paths |
| `handbook_toc` | Section headings of a manual (its table of contents), optional title filter |
| `handbook_search` | Full-text search; returns section id, title, PDF page range, highlighted snippet |
| `handbook_read` | Read one section by id; page through long sections with `offset` |
| `examples_list` | List indexed example decks (name / category / description) |
| `examples_search` | Full-text search over deck name, description, and content |
| `examples_read` | Read one example deck in full (partial unique names accepted) |

## Requirements

- Python ≥ 3.10
- Python packages: `mcp`, `pymupdf` (see `requirements.txt`)
- [MinerU Open API CLI](https://github.com/opendatalab/MinerU) (`mineru-open-api`)
  plus an API token — only needed for `convert_mineru.py`
- The Silvaco manuals as PDFs, and (optionally) a Silvaco installation for the
  example decks

Install the Python dependencies:

```bash
pip install -r requirements.txt
```

## Quick start

### 1. Convert the manuals to Markdown

MinerU's precision extract is limited to 200 pages per request, so
`convert_mineru.py` processes each PDF in page-range chunks, marks chunk
boundaries with `<!-- pdf pages S-E -->` comments, and merges them into one
Markdown file per manual. It is resumable: existing chunks are skipped on
re-run.

```bash
export MINERU_TOKEN=<your-mineru-token>
export SILVACO_PDF_DIR=/path/to/silvaco/handbook/pdfs
export SILVACO_MD_DIR=/path/to/markdown/output

# Convert every PDF in SILVACO_PDF_DIR (or only one manual with --only)
python convert_mineru.py
python convert_mineru.py --only deckbuild_users1 --chunk-size 200
```

Output layout:

```
<SILVACO_MD_DIR>/<manual>/chunks/p001-200.md   # one per chunk
<SILVACO_MD_DIR>/<manual>/chunks/images/      # extracted images
<SILVACO_MD_DIR>/<manual>/<manual>.md         # merged, indexed by the server
```

### 2. Build the index

```bash
python server.py --build          # incremental
python server.py --build --force  # full rebuild
```

The index also rebuilds itself lazily when the server starts and a source file
has changed, so this step is optional.

### 3. Register the MCP server

Add it to your MCP client configuration.

**Kimi Code / Claude Code (`.kimi-code/mcp.json` or `.claude/mcp.json`):**

```json
{
  "mcpServers": {
    "silvaco-handbook": {
      "command": "python",
      "args": ["/path/to/Silvaco_MCP/server.py"],
      "env": {
        "SILVACO_MD_DIR": "/path/to/markdown/output",
        "SILVACO_EXAMPLES_DIR": "C:/Silvaco/examples/deckbuild/5.2.40.R"
      }
    }
  }
}
```

**Claude Desktop (`claude_desktop_config.json`):** same `mcpServers` block.

**Cursor:** Settings → MCP → add the same command/args/env.

### 4. Ask questions

Once connected, your assistant can answer with manual citations, e.g.:

- *"How does Atlas model impact ionization with the Selberherr model?"*
  → `handbook_search("impact ionization Selberherr")` → `handbook_read(...)`
- *"Show me an official example of a quantum well laser gain simulation."*
  → `examples_search("quantum well laser optical gain")` → `examples_read(...)`

## Configuration

All paths are set via environment variables:

| Variable | Default | Purpose |
|---|---|---|
| `SILVACO_MD_DIR` | `<repo>/../../mineru-output/silvaco` | Root of converted manual Markdown (`<manual>/<manual>.md`) |
| `SILVACO_CACHE_DB` | `<repo>/.cache/handbooks_md.db` | SQLite FTS5 cache location |
| `SILVACO_EXAMPLES_DIR` | *(unset → examples tools disabled)* | Root of the official deckbuild examples tree |
| `SILVACO_PDF_DIR` | *(vault layout default)* | Handbook PDF directory (used by `convert_mineru.py`) |
| `SILVACO_MCP_CONFIG` | *(vault layout default)* | `mcp.json` to read `MINERU_TOKEN` from (optional) |
| `MINERU_TOKEN` | — | MinerU Open API token (takes precedence over config file) |
| `MINERU_CLI` | `mineru-open-api` | Path/name of the MinerU CLI executable |

## Usage examples

Typical agent workflow with the tools:

```
handbook_list_manuals()
→ [{"manual": "deckbuild_users1", "sections": 481, ...}, ...]

handbook_search("impact ionization Selberherr", manual="victorydevice")
→ [{"section_id": 512, "section": "3.7.4 Impact Ionization Models",
    "pdf_pages": "201-400", "snippet": "... **Selberherr** ..."}, ...]

handbook_read("victorydevice", section_id=512)
→ "===== victorydevice [512] 3.7.4 Impact Ionization Models (pdf p.201-400) =====
   ... full section text ..."

handbook_read("victorydevice", section_id=512, offset=12000)   # continue long sections

examples_search("quantum well laser optical gain", category="Opto")
→ [{"name": "optoex14", "category": "Technology/Opto_and_Photonics",
    "description": "Quantum Well Laser ...", "snippet": "..."}, ...]

examples_read("optoex14")
→ "===== example optoex14 [...] =====\n# Quantum Well Laser ...\ngo atlas ..."
```

## Maintenance

- Manual PDFs updated → re-run `python convert_mineru.py` (incremental); the
  index rebuilds automatically on the next server start, or force it with
  `python server.py --build`.
- Example decks changed under `SILVACO_EXAMPLES_DIR` → the index is updated
  incrementally on startup (deleted decks are dropped as well).
- The `.cache/` directory is derived state — safe to delete at any time.

"""Silvaco Handbook MCP server.

Indexes the MinerU-converted Markdown of the Silvaco manuals
(``mineru-output/silvaco/<manual>/<manual>.md``, produced by
``convert_mineru.py``) into a local SQLite FTS5 cache and exposes bounded,
context-friendly tools over stdio MCP:

- handbook_list_manuals : what manuals exist, section counts
- handbook_toc          : section headings (= table of contents)
- handbook_search       : full-text search, returns section id + short snippet
- handbook_read         : read one section, bounded, page-through via offset

Each section carries the original PDF page range (from chunk-boundary
markers) so results can be cross-referenced with the source PDFs in
``20_Knowledge/silvaco handbook``.

The index is rebuilt lazily on start when a Markdown file changes
(size/mtime fingerprint). Manual rebuild: ``python server.py --build``.

It also indexes the official Silvaco Deckbuild examples (the ``.in`` input
decks under ``SILVACO_EXAMPLES_DIR``, e.g. the installation's
``examples/deckbuild/<version>`` tree) into a second FTS5 corpus:

- examples_list    : list indexed example decks (optionally by category)
- examples_search  : full-text search over deck name / description / content
- examples_read    : read one example deck in full (bounded)

Configuration via environment variables:
- SILVACO_MD_DIR       : directory with <manual>/<manual>.md (default mineru-output/silvaco)
- SILVACO_CACHE_DB     : path of the SQLite cache file
- SILVACO_EXAMPLES_DIR : root of the official deckbuild examples tree
                         (contains Educational/ Technology/ Tool/ ...)
"""

from __future__ import annotations

import os
import re
import sqlite3
import sys
from pathlib import Path
from typing import NamedTuple

from mcp.server.fastmcp import FastMCP

BASE_DIR = Path(__file__).resolve().parent
VAULT_ROOT = BASE_DIR.parent.parent
MD_ROOT = Path(os.environ.get("SILVACO_MD_DIR", VAULT_ROOT / "mineru-output" / "silvaco"))
DB_PATH = Path(os.environ.get("SILVACO_CACHE_DB", BASE_DIR / ".cache" / "handbooks_md.db"))
EXAMPLES_ROOT = Path(os.environ["SILVACO_EXAMPLES_DIR"]) if os.environ.get("SILVACO_EXAMPLES_DIR") else None

MAX_READ_CHARS = 20000      # hard bound per handbook_read call
MAX_SEARCH_RESULTS = 30     # hard bound per handbook_search call
MAX_LIST_ENTRIES = 200      # hard bound per examples_list call

HEADING_RE = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$", re.M)
PAGES_MARK_RE = re.compile(r"<!--\s*pdf pages (\d+)-(\d+)\s*-->")

mcp = FastMCP("silvaco-handbook")


# --------------------------------------------------------------------------
# Markdown section parsing
# --------------------------------------------------------------------------

class Section(NamedTuple):
    """A heading-delimited markdown section; start/end are char offsets into the md text."""
    idx: int
    level: int
    title: str
    start: int
    end: int
    pdf_pages: str | None


def parse_sections(text: str) -> list[Section]:
    """Split markdown into heading-delimited sections (flat model)."""
    headings = list(HEADING_RE.finditer(text))
    marks = [(m.start(), f"{m.group(1)}-{m.group(2)}") for m in PAGES_MARK_RE.finditer(text)]

    def pages_at(pos: int) -> str | None:
        current = None
        for mpos, label in marks:
            if mpos > pos:
                break
            current = label
        return current

    sections: list[Section] = []
    if headings and headings[0].start() > 0:
        sections.append(Section(0, 0, "(front matter)", 0, headings[0].start(), pages_at(0)))
    for i, h in enumerate(headings):
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        sections.append(
            Section(len(sections), len(h.group(1)), h.group(2).strip(), h.start(), end, pages_at(h.start()))
        )
    if not headings:
        sections.append(Section(0, 0, "(document)", 0, len(text), pages_at(0)))
    return sections


# --------------------------------------------------------------------------
# Index building
# --------------------------------------------------------------------------

def _connect() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(
        "CREATE TABLE IF NOT EXISTS meta (manual TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, sections INTEGER)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS sections "
        "(manual TEXT, idx INTEGER, level INTEGER, title TEXT, start INTEGER, \"end\" INTEGER, pdf_pages TEXT)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS sections_fts "
        "USING fts5(manual UNINDEXED, idx UNINDEXED, title, text)"
    )
    conn.execute(
        "CREATE TABLE IF NOT EXISTS examples "
        "(path TEXT PRIMARY KEY, name TEXT, category TEXT, title TEXT, size INTEGER, mtime_ns INTEGER)"
    )
    conn.execute(
        "CREATE VIRTUAL TABLE IF NOT EXISTS examples_fts "
        "USING fts5(path UNINDEXED, name, category, title, text)"
    )
    return conn


def _index_manual(conn: sqlite3.Connection, md_path: Path) -> None:
    manual = md_path.stem
    stat = md_path.stat()
    text = md_path.read_text(encoding="utf-8")

    for table in ("meta", "sections"):
        conn.execute(f"DELETE FROM {table} WHERE manual = ?", (manual,))
    conn.execute("DELETE FROM sections_fts WHERE manual = ?", (manual,))

    sections = parse_sections(text)
    conn.executemany(
        'INSERT INTO sections (manual, idx, level, title, start, "end", pdf_pages) VALUES (?, ?, ?, ?, ?, ?, ?)',
        [(manual, s.idx, s.level, s.title, s.start, s.end, s.pdf_pages) for s in sections],
    )
    conn.executemany(
        "INSERT INTO sections_fts (manual, idx, title, text) VALUES (?, ?, ?, ?)",
        [(manual, s.idx, s.title, text[s.start:s.end]) for s in sections],
    )
    conn.execute(
        "INSERT INTO meta (manual, size, mtime_ns, sections) VALUES (?, ?, ?, ?)",
        (manual, stat.st_size, stat.st_mtime_ns, len(sections)),
    )


def _find_manual_mds() -> list[Path]:
    """Merged manual markdowns: <MD_ROOT>/<manual>/<manual>.md (chunks excluded)."""
    return sorted(p for p in MD_ROOT.glob("*/*.md") if p.parent.name != "chunks")


def build_index(force: bool = False) -> list[str]:
    """(Re)index any manual whose Markdown fingerprint changed."""
    if not MD_ROOT.is_dir():
        raise RuntimeError(
            f"Markdown directory not found: {MD_ROOT}\nRun convert_mineru.py first."
        )
    rebuilt: list[str] = []
    with _connect() as conn:
        existing = {
            row[0]: (row[1], row[2])
            for row in conn.execute("SELECT manual, size, mtime_ns FROM meta")
        }
        for md in _find_manual_mds():
            stat = md.stat()
            if not force and existing.get(md.stem) == (stat.st_size, stat.st_mtime_ns):
                continue
            print(f"[silvaco-handbook] indexing {md.name} ...", file=sys.stderr)
            _index_manual(conn, md)
            rebuilt.append(md.stem)
    return rebuilt


# --------------------------------------------------------------------------
# Official example deck (.in) indexing
# --------------------------------------------------------------------------

def _example_title(text: str) -> str:
    """Description from the leading '#' comment block of a deck, minus copyright lines."""
    lines: list[str] = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            if lines:
                break
            continue
        if not stripped.startswith("#"):
            break
        content = stripped.lstrip("#").strip()
        if content and not content.lower().startswith(("(c)", "copyright")):
            lines.append(content)
        if len(lines) >= 6:
            break
    return "; ".join(lines)


def _example_category(rel: Path) -> str:
    """Category from the first path components, e.g. 'Technology/Opto_and_Photonics'."""
    return "/".join(rel.parts[:-2]) if len(rel.parts) > 2 else (rel.parts[0] if rel.parts else "")


def _index_example(conn: sqlite3.Connection, in_path: Path) -> None:
    rel = in_path.relative_to(EXAMPLES_ROOT)
    rel_str = rel.as_posix()
    text = in_path.read_text(encoding="utf-8", errors="replace")
    stat = in_path.stat()
    conn.execute("DELETE FROM examples WHERE path = ?", (rel_str,))
    conn.execute("DELETE FROM examples_fts WHERE path = ?", (rel_str,))
    conn.execute(
        "INSERT INTO examples (path, name, category, title, size, mtime_ns) VALUES (?, ?, ?, ?, ?, ?)",
        (rel_str, in_path.stem, _example_category(rel), _example_title(text), stat.st_size, stat.st_mtime_ns),
    )
    conn.execute(
        "INSERT INTO examples_fts (path, name, category, title, text) VALUES (?, ?, ?, ?, ?)",
        (rel_str, in_path.stem, _example_category(rel), _example_title(text), text),
    )


def build_examples_index(force: bool = False) -> list[str]:
    """(Re)index example decks whose fingerprint changed; drop deleted ones."""
    if EXAMPLES_ROOT is None or not EXAMPLES_ROOT.is_dir():
        return []
    rebuilt: list[str] = []
    found = {p.relative_to(EXAMPLES_ROOT).as_posix(): p for p in EXAMPLES_ROOT.rglob("*.in")}
    with _connect() as conn:
        existing = {
            row[0]: (row[1], row[2])
            for row in conn.execute("SELECT path, size, mtime_ns FROM examples")
        }
        for stale in set(existing) - set(found):
            conn.execute("DELETE FROM examples WHERE path = ?", (stale,))
            conn.execute("DELETE FROM examples_fts WHERE path = ?", (stale,))
            rebuilt.append(f"-{stale}")
        for rel_str, path in sorted(found.items()):
            stat = path.stat()
            if not force and existing.get(rel_str) == (stat.st_size, stat.st_mtime_ns):
                continue
            _index_example(conn, path)
            rebuilt.append(rel_str)
    if rebuilt:
        print(f"[silvaco-handbook] indexed {len(rebuilt)} example deck(s)", file=sys.stderr)
    return rebuilt


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

def _resolve_manual(conn: sqlite3.Connection, manual: str) -> str:
    names = [row[0] for row in conn.execute("SELECT manual FROM meta ORDER BY manual")]
    if not names:
        raise ValueError("Index is empty - run convert_mineru.py first, then restart.")
    if manual in names:
        return manual
    hits = [n for n in names if manual.lower() in n.lower()]
    if len(hits) == 1:
        return hits[0]
    raise ValueError(f"Unknown or ambiguous manual '{manual}'. Available: {', '.join(names)}")


def _fts_query(raw: str) -> str:
    tokens = re.findall(r"[\w.-]+", raw, flags=re.UNICODE)
    if not tokens:
        raise ValueError("Query contains no searchable terms.")
    return " ".join(f'"{t}"' for t in tokens)


def _resolve_example(conn: sqlite3.Connection, name: str) -> str:
    """Resolve an example name (stem or relative path, partial ok) to its relative path."""
    rows = conn.execute("SELECT path, name FROM examples").fetchall()
    if not rows:
        raise ValueError("Example index is empty - check SILVACO_EXAMPLES_DIR and restart.")
    for path, stem in rows:
        if name == path:
            return path
    exact = [path for path, stem in rows if name == stem]
    if len(exact) == 1:
        return exact[0]
    if exact:
        raise ValueError(f"Ambiguous example '{name}'. Matches: {', '.join(exact)}")
    hits = [path for path, stem in rows if name.lower() in stem.lower() or name.lower() in path.lower()]
    if len(hits) == 1:
        return hits[0]
    if hits:
        raise ValueError(f"Ambiguous example '{name}'. Matches: {', '.join(hits[:10])}")
    raise ValueError(f"Unknown example '{name}'. See examples_list / examples_search.")


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------

@mcp.tool()
def handbook_list_manuals() -> list[dict]:
    """List the indexed Silvaco manuals with section counts and source paths."""
    pdf_dir = Path(os.environ.get("SILVACO_PDF_DIR", VAULT_ROOT / "20_Knowledge" / "silvaco handbook"))
    with _connect() as conn:
        return [
            {
                "manual": manual,
                "sections": n,
                "markdown": str(MD_ROOT / manual / f"{manual}.md"),
                "source_pdf": str(pdf) if (pdf := pdf_dir / f"{manual}.pdf").is_file() else None,
            }
            for manual, n in conn.execute("SELECT manual, sections FROM meta ORDER BY manual")
        ]


@mcp.tool()
def handbook_toc(manual: str, title_filter: str | None = None, max_entries: int = 300) -> str:
    """List section headings of a manual (its table of contents).

    Args:
        manual: manual name, partial names ok (e.g. "deckbuild", "victoryprocess").
        title_filter: optional case-insensitive substring filter on headings.
        max_entries: cap on returned entries.
    """
    with _connect() as conn:
        manual = _resolve_manual(conn, manual)
        rows = conn.execute(
            "SELECT idx, title, pdf_pages FROM sections WHERE manual = ? ORDER BY idx",
            (manual,),
        ).fetchall()
    if title_filter:
        rows = [r for r in rows if title_filter.lower() in r[1].lower()]
    lines = [f"[{idx}] {title}  (pdf p.{pages or '?'})" for idx, title, pages in rows[:max_entries]]
    if not lines:
        return f"No sections found for '{manual}'."
    return "\n".join(lines)


@mcp.tool()
def handbook_search(query: str, manual: str | None = None, max_results: int = 10) -> list[dict]:
    """Full-text search across the manuals. Returns section id, section title,
    original PDF page range, and a short highlighted snippet per hit.
    Use handbook_read with the section id to get the full section text.

    Args:
        query: free-text query, e.g. "impact ionization Selberherr".
        manual: optional manual name filter (partial ok).
        max_results: number of hits (hard cap 30).
    """
    max_results = min(max_results, MAX_SEARCH_RESULTS)
    fts = _fts_query(query)
    sql = (
        "SELECT f.manual, f.idx, f.title, s.pdf_pages, "
        "snippet(sections_fts, 3, '**', '**', ' ... ', 40) "
        "FROM sections_fts f JOIN sections s ON s.manual = f.manual AND s.idx = f.idx "
        "WHERE sections_fts MATCH ? {scope} ORDER BY rank LIMIT ?"
    )
    with _connect() as conn:
        params: list = [fts]
        scope = ""
        if manual:
            scope = "AND f.manual = ?"
            params.append(_resolve_manual(conn, manual))
        params.append(max_results)
        rows = conn.execute(sql.format(scope=scope), params).fetchall()
        return [
            {
                "manual": m,
                "section_id": idx,
                "section": title,
                "pdf_pages": pages,
                "snippet": re.sub(r"\s+", " ", snip).strip(),
            }
            for m, idx, title, pages, snip in rows
        ]


@mcp.tool()
def handbook_read(manual: str, section_id: int, offset: int = 0, max_chars: int = 12000) -> str:
    """Read one section's Markdown text. Long sections are paged through with
    `offset` (character offset reported by a previous truncated read).

    Args:
        manual: manual name (partial ok).
        section_id: section id from handbook_toc / handbook_search.
        offset: character offset into the section text (for continuation).
        max_chars: max characters to return (hard cap 20000).
    """
    max_chars = min(max_chars, MAX_READ_CHARS)
    with _connect() as conn:
        manual = _resolve_manual(conn, manual)
        row = conn.execute(
            'SELECT idx, title, start, "end", pdf_pages FROM sections WHERE manual = ? AND idx = ?',
            (manual, section_id),
        ).fetchone()
        if not row:
            raise ValueError(f"No section {section_id} in '{manual}'. See handbook_toc.")
        md = MD_ROOT / manual / f"{manual}.md"
        text = md.read_text(encoding="utf-8")
    idx, title, start, end, pages = row
    body = text[start:end]
    chunk = body[offset:offset + max_chars]
    header = f"===== {manual} [{idx}] {title} (pdf p.{pages or '?'}) ====="
    if offset + max_chars < len(body):
        chunk += f"\n\n... [truncated: {len(body) - offset - max_chars} chars left; continue with offset={offset + max_chars}]"
    return f"{header}\n{chunk}"


@mcp.tool()
def examples_list(category: str | None = None, max_entries: int = 200) -> list[dict]:
    """List the indexed official Silvaco example decks.

    Args:
        category: optional case-insensitive substring filter on the category
            (e.g. "Opto", "Power_and_RF", "Victory_Process").
        max_entries: cap on returned entries (hard cap 200).
    """
    max_entries = min(max_entries, MAX_LIST_ENTRIES)
    with _connect() as conn:
        rows = conn.execute(
            "SELECT name, category, title FROM examples ORDER BY category, name"
        ).fetchall()
    if category:
        rows = [r for r in rows if category.lower() in (r[1] or "").lower()]
    return [
        {"name": name, "category": cat, "description": title}
        for name, cat, title in rows[:max_entries]
    ]


@mcp.tool()
def examples_search(query: str, category: str | None = None, max_results: int = 10) -> list[dict]:
    """Full-text search across the official Silvaco example decks (name,
    description, and deck content). Returns example name, category,
    description, and a short highlighted snippet per hit.
    Use examples_read with the name to get the full deck.

    Args:
        query: free-text query, e.g. "quantum well laser optical gain".
        category: optional case-insensitive substring filter on the category.
        max_results: number of hits (hard cap 30).
    """
    max_results = min(max_results, MAX_SEARCH_RESULTS)
    fts = _fts_query(query)
    sql = (
        "SELECT f.name, f.category, f.title, "
        "snippet(examples_fts, 4, '**', '**', ' ... ', 40) "
        "FROM examples_fts f WHERE examples_fts MATCH ? {scope} ORDER BY rank LIMIT ?"
    )
    with _connect() as conn:
        params: list = [fts]
        scope = ""
        if category:
            scope = "AND lower(f.category) LIKE ?"
            params.append(f"%{category.lower()}%")
        params.append(max_results)
        rows = conn.execute(sql.format(scope=scope), params).fetchall()
        return [
            {
                "name": name,
                "category": cat,
                "description": title,
                "snippet": re.sub(r"\s+", " ", snip).strip(),
            }
            for name, cat, title, snip in rows
        ]


@mcp.tool()
def examples_read(name: str, max_chars: int = 20000) -> str:
    """Read one official example deck in full.

    Args:
        name: example name (e.g. "ASU_ex01") or relative path; partial unique
            matches are accepted.
        max_chars: max characters to return (hard cap 20000).
    """
    if EXAMPLES_ROOT is None:
        raise ValueError("SILVACO_EXAMPLES_DIR is not configured.")
    max_chars = min(max_chars, MAX_READ_CHARS)
    with _connect() as conn:
        rel = _resolve_example(conn, name)
        row = conn.execute(
            "SELECT name, category, title FROM examples WHERE path = ?", (rel,)
        ).fetchone()
    text = (EXAMPLES_ROOT / rel).read_text(encoding="utf-8", errors="replace")
    chunk = text[:max_chars]
    header = f"===== example {row[0]} [{row[1]}] {row[2]} ({rel}) ====="
    if len(text) > max_chars:
        chunk += f"\n\n... [truncated: {len(text) - max_chars} chars left]"
    return f"{header}\n{chunk}"


# --------------------------------------------------------------------------
# Entry point
# --------------------------------------------------------------------------

def main() -> None:
    if "--build" in sys.argv:
        force = "--force" in sys.argv
        rebuilt = build_index(force=force)
        rebuilt += build_examples_index(force=force)
        print("rebuilt:", ", ".join(rebuilt) if rebuilt else "(nothing changed)")
        return
    build_index()  # lazy warmup if Markdown changed
    build_examples_index()
    mcp.run()


if __name__ == "__main__":
    main()

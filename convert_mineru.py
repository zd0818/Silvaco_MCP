"""Convert the Silvaco handbook PDFs to Markdown via the MinerU Open API CLI.

MinerU precision extract is limited to 200 pages per request, so each manual
is processed in page-range chunks:

    mineru-output/silvaco/<manual>/chunks/p001-200.md   (one per chunk)
    mineru-output/silvaco/<manual>/chunks/images/       (shared, hash names)
    mineru-output/silvaco/<manual>/<manual>.md          (merged, image paths fixed)

Resumable: existing non-empty chunk files are skipped. Re-run to continue
after an interruption. The API token is read from .kimi-code/mcp.json
(obsidian-vault env MINERU_TOKEN) or the MINERU_TOKEN environment variable.

Usage:
    python convert_mineru.py [--chunk-size 200] [--only deckbuild_users1]

Configuration via environment variables:
    SILVACO_PDF_DIR    : directory containing the handbook PDFs
    SILVACO_MD_DIR     : output root for the converted Markdown
    SILVACO_MCP_CONFIG : mcp.json to read MINERU_TOKEN from (optional)
    MINERU_TOKEN       : MinerU Open API token (takes precedence)
    MINERU_CLI         : path/name of the mineru-open-api CLI
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pymupdf

BASE_DIR = Path(__file__).resolve().parent
VAULT_ROOT = BASE_DIR.parent.parent
HANDBOOK_DIR = Path(os.environ.get("SILVACO_PDF_DIR", VAULT_ROOT / "20_Knowledge" / "silvaco handbook"))
OUTPUT_ROOT = Path(os.environ.get("SILVACO_MD_DIR", VAULT_ROOT / "mineru-output" / "silvaco"))
MCP_CONFIG = Path(os.environ.get("SILVACO_MCP_CONFIG", VAULT_ROOT / ".kimi-code" / "mcp.json"))
MINERU_CLI = Path(os.environ.get("MINERU_CLI", "mineru-open-api"))

CHUNK_SIZE = 200
TIMEOUT = 1800  # seconds per chunk


def get_token() -> str:
    if os.environ.get("MINERU_TOKEN"):
        return os.environ["MINERU_TOKEN"]
    if MCP_CONFIG.is_file():
        cfg = json.loads(MCP_CONFIG.read_text(encoding="utf-8"))
        return cfg["mcpServers"]["obsidian-vault"]["env"]["MINERU_TOKEN"]
    raise RuntimeError(
        "No MinerU token found. Set MINERU_TOKEN or point SILVACO_MCP_CONFIG "
        f"at an mcp.json containing one (looked at: {MCP_CONFIG})."
    )


def page_count(pdf: Path) -> int:
    with pymupdf.open(pdf) as doc:
        return doc.page_count


def chunk_ranges(pages: int, size: int) -> list[tuple[int, int]]:
    return [(s, min(s + size - 1, pages)) for s in range(1, pages + 1, size)]


def extract_chunk(pdf: Path, start: int, end: int, out_md: Path, token: str) -> bool:
    """Run one MinerU extract. Returns True on success."""
    if out_md.exists() and out_md.stat().st_size > 0:
        print(f"  skip (exists): {out_md.name}", flush=True)
        return True
    out_md.parent.mkdir(parents=True, exist_ok=True)
    cmd = (
        f'"{MINERU_CLI}" extract "{pdf}" --pages {start}-{end} '
        f'-o "{out_md}" -f md -l en --token "{token}" --timeout {TIMEOUT}'
    )
    print(f"  extracting pages {start}-{end} -> {out_md.name}", flush=True)
    proc = subprocess.run(cmd, shell=True, capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0 or not (out_md.exists() and out_md.stat().st_size > 0):
        print(f"  FAILED ({proc.returncode}): {proc.stdout[-500:]}{proc.stderr[-500:]}", flush=True)
        return False
    return True


def merge_chunks(manual: str, chunks: list[tuple[tuple[int, int], Path]], out_md: Path) -> None:
    """Concatenate chunk markdowns; rewrite image links to chunks/images/ and
    mark each chunk boundary with a ``<!-- pdf pages S-E -->`` comment so the
    MCP index can map sections back to original PDF pages."""
    parts = []
    for (s, e), chunk in chunks:
        text = chunk.read_text(encoding="utf-8")
        text = re.sub(r"\]\(images/", "](chunks/images/", text)
        parts.append(f"<!-- pdf pages {s}-{e} -->\n\n" + text.rstrip())
    out_md.write_text("\n\n".join(parts) + "\n", encoding="utf-8")
    print(f"merged {len(chunks)} chunks -> {out_md}", flush=True)


def convert_manual(pdf: Path, token: str, chunk_size: int) -> bool:
    manual = pdf.stem
    manual_dir = OUTPUT_ROOT / manual
    chunks_dir = manual_dir / "chunks"
    pages = page_count(pdf)
    ranges = chunk_ranges(pages, chunk_size)
    print(f"[{manual}] {pages} pages, {len(ranges)} chunks", flush=True)

    chunk_files = [chunks_dir / f"p{s:03d}-{e:03d}.md" for s, e in ranges]
    ok = True
    for (s, e), out in zip(ranges, chunk_files):
        if not extract_chunk(pdf, s, e, out, token):
            ok = False
            break  # keep order; rerun resumes from here
    if ok:
        merge_chunks(manual, list(zip(ranges, chunk_files)), manual_dir / f"{manual}.md")
    return ok


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--chunk-size", type=int, default=CHUNK_SIZE)
    ap.add_argument("--only", help="convert only this manual (stem substring)")
    args = ap.parse_args()

    token = get_token()
    pdfs = sorted(HANDBOOK_DIR.glob("*.pdf"))
    if args.only:
        pdfs = [p for p in pdfs if args.only.lower() in p.stem.lower()]
    if not pdfs:
        print("no matching PDFs", file=sys.stderr)
        return 1

    failed = []
    for pdf in pdfs:
        if not convert_manual(pdf, token, args.chunk_size):
            failed.append(pdf.stem)
    if failed:
        print(f"FAILED manuals: {failed}", flush=True)
        return 1
    print("ALL DONE", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

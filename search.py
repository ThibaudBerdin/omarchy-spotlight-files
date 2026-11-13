#!/usr/bin/env python3
"""Spotlight-style search & preview backend for Spotlight Files (Omarchy Quickshell).

Zero permanent background daemon:
- Filename search: fd + fzf --scheme=path over cached relative paths (~25-200ms on 220k files).
- Content search: SQLite FTS5 local cache (unicode61 remove_diacritics 2) for PDFs & Office
  documents + live ripgrep (rg --null) for text/markdown/code.
- Preview: PDF page rendering (pdftoppm), images, Office XML text, text/code, folder listing.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

PLUGIN_ID = "io.github.thibaudberdin.spotlight-files"
HOME = Path.home()
XDG_CONFIG = Path(os.environ.get("XDG_CONFIG_HOME", HOME / ".config"))
CACHE_DIR = Path(os.environ.get("XDG_CACHE_HOME", HOME / ".cache")) / PLUGIN_ID
CONFIG_PATH = XDG_CONFIG / "omarchy" / "spotlight-files.json"
LANGUAGE_PATH = XDG_CONFIG / "omarchy" / "language"
PATHS_CACHE = CACHE_DIR / "paths.txt"
RECENT_CACHE = CACHE_DIR / "recent.json"
PREVIEW_DIR = CACHE_DIR / "previews"
FTS_DB = CACHE_DIR / "fts.sqlite"


def is_french() -> bool:
    try:
        if LANGUAGE_PATH.is_file():
            code = LANGUAGE_PATH.read_text(encoding="utf-8").strip().lower()
            if code:
                return code.startswith("fr")
    except OSError:
        pass
    lang = (os.environ.get("LC_ALL") or os.environ.get("LANG") or "").lower()
    return lang.startswith("fr")


IS_FR = is_french()

DEFAULT_EXCLUDES = [
    ".git",
    "node_modules",
    "__MACOSX",
    ".dropbox",
    ".dropbox-dist",
    ".dropbox.cache",
    "target",
    "venv",
    ".venv",
    "__pycache__",
    ".next",
    "dist",
    "build",
]

DOC_EXTS = {".pdf", ".docx", ".xlsx", ".pptx", ".odt", ".ods", ".odp"}
TEXT_EXTS = {
    ".txt", ".md", ".markdown", ".csv", ".tsv", ".json", ".jsonc", ".toml",
    ".yaml", ".yml", ".ini", ".conf", ".cfg", ".log", ".org", ".rst",
    ".py", ".js", ".ts", ".tsx", ".jsx", ".qml", ".lua", ".sh", ".bash",
    ".zsh", ".fish", ".rs", ".go", ".rb", ".php", ".c", ".h", ".cpp",
    ".hpp", ".java", ".kt", ".swift", ".sql", ".html", ".css", ".scss", ".xml",
}
IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".svg", ".bmp", ".ico", ".avif"}
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".xz", ".zst", ".7z", ".rar", ".bz2"}
MEDIA_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".mov", ".webm", ".avi"}

RG_GLOBS = [f"*{ext}" for ext in sorted(TEXT_EXTS)]


def ensure_dirs() -> None:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    PREVIEW_DIR.mkdir(parents=True, exist_ok=True)


def load_config() -> dict:
    cfg = {
        "excludes": DEFAULT_EXCLUDES,
        "maxIndexedTextBytes": 350_000,
    }
    if CONFIG_PATH.is_file():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                cfg.update(user_cfg)
        except Exception:
            pass
    return cfg


def detect_dropbox_roots() -> list[Path]:
    roots: list[Path] = []
    info_file = HOME / ".dropbox" / "info.json"
    if info_file.is_file():
        try:
            data = json.loads(info_file.read_text(encoding="utf-8"))
            for section in ("personal", "business"):
                sec = data.get(section, {})
                for key in ("root_path", "path"):
                    p = sec.get(key)
                    if p and Path(p).is_dir():
                        rp = Path(p).resolve()
                        if rp not in roots:
                            roots.append(rp)
        except Exception:
            pass
    try:
        for child in HOME.iterdir():
            if child.is_dir() and "dropbox" in child.name.lower() and not child.name.startswith("."):
                rp = child.resolve()
                if rp not in roots:
                    roots.append(rp)
    except OSError:
        pass
    return roots


DROPBOX_ROOTS = detect_dropbox_roots()


def is_in_dropbox(path: Path) -> bool:
    try:
        rp = path.resolve()
        return any(rp == root or root in rp.parents for root in DROPBOX_ROOTS)
    except Exception:
        return False


def glyph_for(path: Path, is_dir: bool = False) -> str:
    if is_dir:
        return "󰉋"
    ext = path.suffix.lower()
    if ext == ".pdf":
        return "󰈦"
    if ext in {".docx", ".doc", ".odt", ".rtf"}:
        return "󰈬"
    if ext in {".xlsx", ".xls", ".ods", ".csv", ".tsv"}:
        return "󰈛"
    if ext in {".pptx", ".ppt", ".odp"}:
        return "󰈧"
    if ext in IMAGE_EXTS:
        return "󰋩"
    if ext in MEDIA_EXTS:
        return "󰎁"
    if ext in ARCHIVE_EXTS:
        return "󰀼"
    if ext in {".md", ".markdown", ".txt", ".org"}:
        return "󰈙"
    if ext in TEXT_EXTS:
        return "󰅩"
    return "󰈔"


def human_size(num: int) -> str:
    val = float(num)
    units = ("o", "Ko", "Mo", "Go", "To") if IS_FR else ("B", "KB", "MB", "GB", "TB")
    for unit in units:
        if val < 1024.0 or unit == units[-1]:
            if unit == units[0]:
                return f"{int(val)} {unit}"
            formatted = f"{val:.1f} {unit}"
            return formatted.replace(".", ",") if IS_FR else formatted
        val /= 1024.0
    return f"{num} {units[0]}"


def human_date(mtime: float) -> str:
    d = dt.datetime.fromtimestamp(mtime)
    now = dt.datetime.now()
    if d.date() == now.date():
        prefix = "Aujourd’hui" if IS_FR else "Today"
        return f"{prefix} {d.strftime('%H:%M')}"
    if d.date() == (now.date() - dt.timedelta(days=1)):
        prefix = "Hier" if IS_FR else "Yesterday"
        return f"{prefix} {d.strftime('%H:%M')}"
    if IS_FR:
        months = ["janv.", "févr.", "mars", "avr.", "mai", "juin", "juil.", "août", "sept.", "oct.", "nov.", "déc."]
        return f"{d.day} {months[d.month - 1]} {d.year}"
    months_en = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
    return f"{months_en[d.month - 1]} {d.day}, {d.year}"


def rel_display(path: Path) -> str:
    try:
        rel = path.relative_to(HOME)
        parent = str(rel.parent)
        return "~" if parent == "." else parent.replace("/", " › ")
    except ValueError:
        return str(path.parent)


def make_result_item(path: Path, is_dir: bool | None = None, snippet: str = "") -> dict | None:
    try:
        st = path.stat()
    except OSError:
        return None
    if is_dir is None:
        is_dir = os.path.isdir(path)
    clean_snip = " ".join(snippet.split()) if snippet else ""
    if len(clean_snip) > 160:
        clean_snip = clean_snip[:157] + "…"
    return {
        "path": str(path),
        "name": path.name or str(path),
        "dir": rel_display(path),
        "glyph": glyph_for(path, is_dir),
        "isDir": bool(is_dir),
        "inDropbox": is_in_dropbox(path),
        "snippet": clean_snip,
        "mtime": st.st_mtime,
    }


def fd_base_cmd(cfg: dict) -> list[str]:
    cmd = ["fd", "--color=never", "--strip-cwd-prefix"]
    for ex in cfg.get("excludes", DEFAULT_EXCLUDES):
        cmd.extend(["--exclude", ex])
    cmd.extend(["--exclude", "._*", "--exclude", "~$*"])
    return cmd


def refresh_paths_cache() -> int:
    ensure_dirs()
    cfg = load_config()
    cmd = fd_base_cmd(cfg) + ["."]
    proc = subprocess.run(
        cmd,
        cwd=str(HOME),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    lines = [ln.rstrip("/") for ln in proc.stdout.splitlines() if ln and not ln.startswith(".")]

    fd_tmp, tmp_name = tempfile.mkstemp(prefix="paths.", dir=str(CACHE_DIR), text=True)
    with os.fdopen(fd_tmp, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines))
        if lines:
            fh.write("\n")
    os.replace(tmp_name, PATHS_CACHE)

    # Compute top 35 recent files across ALL directories without alphabetical truncation
    recent_candidates: list[tuple[float, Path]] = []
    for window in ("14d", "90d"):
        rec_cmd = fd_base_cmd(cfg) + ["-t", "f", "--changed-within", window, "."]
        rec_proc = subprocess.run(
            rec_cmd,
            cwd=str(HOME),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            check=False,
        )
        recent_candidates.clear()
        for rel in rec_proc.stdout.splitlines():
            if not rel:
                continue
            p = HOME / rel
            try:
                recent_candidates.append((p.stat().st_mtime, p))
            except OSError:
                continue
        if len(recent_candidates) >= 35:
            break

    recent_candidates.sort(key=lambda x: x[0], reverse=True)
    recent_items = []
    for _, p in recent_candidates[:35]:
        item = make_result_item(p, is_dir=False)
        if item:
            recent_items.append(item)

    fd_rtmp, rtmp_name = tempfile.mkstemp(prefix="recent.", dir=str(CACHE_DIR), text=True)
    with os.fdopen(fd_rtmp, "w", encoding="utf-8") as fh:
        json.dump(recent_items, fh, ensure_ascii=False)
    os.replace(rtmp_name, RECENT_CACHE)

    # Opportunistically index recently modified PDF/Office docs (last 14 days) in background
    con = open_fts()
    opportunistic_index_recent(con, cfg, window="14d", budget_sec=0.35)
    con.close()

    return len(lines)


def load_recent(limit: int = 30) -> list[dict]:
    if not RECENT_CACHE.is_file():
        refresh_paths_cache()
    if RECENT_CACHE.is_file():
        try:
            items = json.loads(RECENT_CACHE.read_text(encoding="utf-8"))
            out = [it for it in items if os.path.exists(it.get("path", ""))]
            return out[:limit]
        except Exception:
            pass
    return []


# ---------------- Document text extraction & SQLite FTS5 ----------------


def open_fts() -> sqlite3.Connection:
    ensure_dirs()
    con = sqlite3.connect(str(FTS_DB), timeout=10.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS docs_meta (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL
        )
        """
    )
    con.execute(
        """
        CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
            path UNINDEXED,
            name,
            body,
            tokenize='unicode61 remove_diacritics 2'
        )
        """
    )
    return con


def extract_docx(path: Path, max_bytes: int) -> str:
    parts: list[str] = []
    total = 0
    with zipfile.ZipFile(path, "r") as zf:
        names = [n for n in zf.namelist() if n.startswith("word/") and n.endswith(".xml") and ("document" in n or "header" in n or "footer" in n)]
        for name in sorted(names):
            data = zf.read(name)
            root = ET.fromstring(data)
            for elem in root.iter():
                tag = elem.tag.rsplit("}", 1)[-1]
                if tag == "t" and elem.text:
                    parts.append(elem.text)
                    total += len(elem.text)
                elif tag in ("p", "tr", "br"):
                    parts.append("\n")
                elif tag == "tab":
                    parts.append("\t")
                if total >= max_bytes:
                    break
            if total >= max_bytes:
                break
    return "".join(parts)[:max_bytes]


def extract_xlsx(path: Path, max_bytes: int) -> str:
    out: list[str] = []
    total = 0
    with zipfile.ZipFile(path, "r") as zf:
        shared: list[str] = []
        if "xl/sharedStrings.xml" in zf.namelist():
            root = ET.fromstring(zf.read("xl/sharedStrings.xml"))
            for si in root:
                if si.tag.rsplit("}", 1)[-1] == "si":
                    texts = [t.text for t in si.iter() if t.tag.rsplit("}", 1)[-1] == "t" and t.text]
                    shared.append("".join(texts))

        sheet_names: list[str] = []
        if "xl/workbook.xml" in zf.namelist():
            wb = ET.fromstring(zf.read("xl/workbook.xml"))
            for sh in wb.iter():
                if sh.tag.rsplit("}", 1)[-1] == "sheet" and sh.attrib.get("name"):
                    sheet_names.append(sh.attrib["name"])

        sheets = sorted(n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        for idx, sname in enumerate(sheets):
            default_sheet = f"Feuille {idx + 1}" if IS_FR else f"Sheet {idx + 1}"
            header = sheet_names[idx] if idx < len(sheet_names) else default_sheet
            out.append(f"=== {header} ===\n")
            sroot = ET.fromstring(zf.read(sname))
            for row in sroot.iter():
                if row.tag.rsplit("}", 1)[-1] != "row":
                    continue
                vals: list[str] = []
                for c in row:
                    if c.tag.rsplit("}", 1)[-1] != "c":
                        continue
                    ctype = c.attrib.get("t", "")
                    val_text = ""
                    if ctype == "inlineStr":
                        val_text = "".join(t.text for t in c.iter() if t.tag.rsplit("}", 1)[-1] == "t" and t.text)
                    else:
                        for child in c:
                            if child.tag.rsplit("}", 1)[-1] == "v" and child.text:
                                if ctype == "s":
                                    try:
                                        sidx = int(child.text)
                                        val_text = shared[sidx] if 0 <= sidx < len(shared) else child.text
                                    except ValueError:
                                        val_text = child.text
                                else:
                                    val_text = child.text
                                break
                    if val_text:
                        vals.append(val_text)
                if vals:
                    line = "\t".join(vals) + "\n"
                    out.append(line)
                    total += len(line)
                    if total >= max_bytes:
                        return "".join(out)[:max_bytes]
    return "".join(out)[:max_bytes]


def extract_pptx(path: Path, max_bytes: int) -> str:
    out: list[str] = []
    total = 0
    with zipfile.ZipFile(path, "r") as zf:
        slides = sorted(n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml"))
        for idx, sname in enumerate(slides, 1):
            sroot = ET.fromstring(zf.read(sname))
            texts = [t.text for t in sroot.iter() if t.tag.rsplit("}", 1)[-1] == "t" and t.text]
            if texts:
                block = f"--- Slide {idx} ---\n" + " ".join(texts) + "\n"
                out.append(block)
                total += len(block)
                if total >= max_bytes:
                    break
    return "".join(out)[:max_bytes]


def extract_odf(path: Path, max_bytes: int) -> str:
    with zipfile.ZipFile(path, "r") as zf:
        if "content.xml" not in zf.namelist():
            return ""
        root = ET.fromstring(zf.read("content.xml"))
        chunks: list[str] = []
        total = 0
        for elem in root.iter():
            if elem.text:
                chunks.append(elem.text)
                total += len(elem.text)
            tag = elem.tag.rsplit("}", 1)[-1]
            if tag in ("p", "h", "table-row"):
                chunks.append("\n")
            elif tag == "table-cell":
                chunks.append("\t")
            if total >= max_bytes:
                break
        return "".join(chunks)[:max_bytes]


def extract_pdf(path: Path, max_bytes: int, timeout_sec: float = 3.5) -> str:
    try:
        proc = subprocess.run(
            ["pdftotext", "-q", "-nopgbrk", str(path), "-"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_sec,
            check=False,
        )
        return proc.stdout[:max_bytes].decode("utf-8", errors="replace")
    except Exception:
        return ""


def extract_document_text(path: Path, max_bytes: int = 350_000) -> str:
    if path.name.startswith("._") or "__MACOSX" in path.parts:
        return ""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return extract_pdf(path, max_bytes)
        if ext == ".docx":
            return extract_docx(path, max_bytes)
        if ext == ".xlsx":
            return extract_xlsx(path, max_bytes)
        if ext == ".pptx":
            return extract_pptx(path, max_bytes)
        if ext in {".odt", ".ods", ".odp"}:
            return extract_odf(path, max_bytes)
    except Exception:
        return ""
    return ""


def fts_stats(con: sqlite3.Connection) -> int:
    row = con.execute("SELECT COUNT(*) FROM docs_meta").fetchone()
    return int(row[0]) if row else 0


def build_fts_query(raw: str) -> str:
    tokens = [re.sub(r'["\'*^():-]', " ", tok).strip() for tok in raw.split()]
    tokens = [t for t in tokens if t]
    if not tokens:
        return ""
    parts = []
    for tok in tokens:
        clean = "".join(ch for ch in tok if ch.isalnum() or ch == "_")
        if clean:
            parts.append(f'"{clean}"*')
    return " AND ".join(parts)


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def make_snippet_around(text: str, query: str, window: int = 140) -> str:
    if not text:
        return ""
    norm_text = strip_accents(text).lower()
    terms = [strip_accents(t).lower() for t in query.split() if t.strip()]
    pos = -1
    for t in terms:
        pos = norm_text.find(t)
        if pos >= 0:
            break
    if pos < 0:
        return " ".join(text[:window].split())
    start = max(0, pos - 45)
    end = min(len(text), pos + window)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    return prefix + " ".join(text[start:end].split()) + suffix


def opportunistic_index_recent(con: sqlite3.Connection, cfg: dict, window: str = "14d", budget_sec: float = 0.35) -> None:
    """Point-checks only recently modified documents without loading the entire docs_meta table."""
    deadline = time.monotonic() + budget_sec
    cmd = fd_base_cmd(cfg) + ["-t", "f"]
    for ext in sorted(DOC_EXTS):
        cmd.extend(["-e", ext.lstrip(".")])
    cmd.extend(["--changed-within", window, "."])
    proc = subprocess.run(
        cmd,
        cwd=str(HOME),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )
    candidates: list[tuple[int, Path, int]] = []
    for rel in proc.stdout.splitlines():
        if not rel:
            continue
        p = HOME / rel
        if p.name.startswith("._") or "__MACOSX" in p.parts:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        row = con.execute("SELECT mtime_ns, size FROM docs_meta WHERE path = ?", (str(p),)).fetchone()
        if row and row[0] == st.st_mtime_ns and row[1] == st.st_size:
            continue
        candidates.append((st.st_mtime_ns, p, st.st_size))

    if not candidates:
        return

    candidates.sort(key=lambda x: x[0], reverse=True)
    max_bytes = int(cfg.get("maxIndexedTextBytes", 350_000))
    rows_to_save: list[tuple[str, str, str, int, int]] = []
    for mtime_ns, p, size in candidates[:24]:
        if time.monotonic() >= deadline:
            break
        body = extract_document_text(p, max_bytes=max_bytes)
        rows_to_save.append((str(p), p.name, body, mtime_ns, size))

    if rows_to_save:
        with con:
            for sp, name, body, mtime_ns, size in rows_to_save:
                con.execute("DELETE FROM docs_fts WHERE path = ?", (sp,))
                con.execute("INSERT INTO docs_fts(path, name, body) VALUES (?, ?, ?)", (sp, name, body))
                con.execute(
                    "INSERT OR REPLACE INTO docs_meta(path, mtime_ns, size) VALUES (?, ?, ?)",
                    (sp, mtime_ns, size),
                )


# ---------------- Search implementations ----------------


def parse_prefix(raw_query: str, explicit_mode: str) -> tuple[str, str, str]:
    """Supports chained prefixes like '?pdf: devis' or 'c: xls: budget'."""
    q = raw_query.strip()
    mode = explicit_mode
    filter_kind = "all"

    for _ in range(3):
        if q.startswith("?"):
            mode = "content"
            q = q[1:].strip()
            continue
        m = re.match(r"^(c|d|dir|pdf|doc|xls|img):(.*)$", q, flags=re.IGNORECASE)
        if m:
            tag = m.group(1).lower()
            q = m.group(2).strip()
            if tag == "c":
                mode = "content"
            elif tag in ("d", "dir"):
                filter_kind = "dir"
            elif tag == "pdf":
                filter_kind = "pdf"
            elif tag == "doc":
                filter_kind = "doc"
            elif tag == "xls":
                filter_kind = "xls"
            elif tag == "img":
                filter_kind = "img"
            continue
        break

    return mode, filter_kind, q


def matches_filter_kind(path: Path, is_dir: bool, filter_kind: str) -> bool:
    if filter_kind == "all":
        return True
    if filter_kind == "dir":
        return is_dir
    if is_dir:
        return False
    ext = path.suffix.lower()
    if filter_kind == "pdf":
        return ext == ".pdf"
    if filter_kind == "doc":
        return ext in {".pdf", ".docx", ".doc", ".odt", ".rtf", ".md", ".txt"}
    if filter_kind == "xls":
        return ext in {".xlsx", ".xls", ".ods", ".csv", ".tsv"}
    if filter_kind == "img":
        return ext in IMAGE_EXTS
    return True


def search_names(query: str, filter_kind: str, limit: int) -> list[dict]:
    if not PATHS_CACHE.is_file():
        refresh_paths_cache()

    # Strip accents on the query so fzf's built-in Unicode normalization matches
    # symmetrically whether the query or the filename on disk carries accents.
    norm_q = strip_accents(query)
    cmd = ["fzf", f"--filter={norm_q}", "--scheme=path"]
    try:
        with open(PATHS_CACHE, "rb") as fh:
            proc = subprocess.run(
                cmd,
                stdin=fh,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                check=False,
            )
    except Exception:
        return []

    results: list[dict] = []
    for raw_line in proc.stdout.decode("utf-8", errors="replace").splitlines():
        if not raw_line:
            continue
        p = HOME / raw_line
        is_dir = p.is_dir()
        if not matches_filter_kind(p, is_dir, filter_kind):
            continue
        item = make_result_item(p, is_dir=is_dir)
        if item:
            results.append(item)
            if len(results) >= limit:
                break
    return results


def search_content(query: str, filter_kind: str, limit: int) -> tuple[list[dict], int]:
    cfg = load_config()
    con = open_fts()
    cached_count = fts_stats(con)

    # Cold-start helper if the user hasn't indexed anything yet
    if cached_count < 20:
        opportunistic_index_recent(con, cfg, window="365d", budget_sec=0.28)
        cached_count = fts_stats(con)

    seen: set[str] = set()
    results: list[dict] = []

    # 1. Instant SQLite FTS5 query across cached PDFs & Office docs (<10ms)
    fts_q = build_fts_query(query)
    if fts_q and filter_kind != "dir":
        try:
            rows = con.execute(
                """
                SELECT path, snippet(docs_fts, 2, '', '', ' … ', 22), bm25(docs_fts, 0.0, 5.0, 1.0) AS rank
                FROM docs_fts
                WHERE docs_fts MATCH ?
                ORDER BY rank
                LIMIT ?
                """,
                (fts_q, limit * 2),
            ).fetchall()
            for sp, snip, _ in rows:
                p = Path(sp)
                if not matches_filter_kind(p, False, filter_kind):
                    continue
                item = make_result_item(p, is_dir=False, snippet=snip or "")
                if item:
                    seen.add(sp)
                    results.append(item)
                    if len(results) >= limit:
                        con.close()
                        return results, cached_count
        except sqlite3.OperationalError:
            pass

    con.close()

    # 2. Live ripgrep (rg --null) across plain-text / Markdown / CSV / code files
    if len(results) < limit and filter_kind in ("all", "doc", "xls"):
        rg_cmd = [
            "rg",
            "--smart-case",
            "--max-count=1",
            "--no-heading",
            "--with-filename",
            "--null",
            "--max-filesize=5M",
            "--color=never",
        ]
        for ex in cfg.get("excludes", DEFAULT_EXCLUDES):
            rg_cmd.extend(["--glob", f"!{ex}/**"])
        for gl in RG_GLOBS:
            rg_cmd.extend(["--glob", gl])
        rg_cmd.extend(["--", query, "."])
        try:
            proc = subprocess.run(
                rg_cmd,
                cwd=str(HOME),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                timeout=0.9,
                check=False,
            )
            out_text = proc.stdout.decode("utf-8", errors="replace")
        except subprocess.TimeoutExpired as exc:
            out_text = (exc.stdout or b"").decode("utf-8", errors="replace")
        except Exception:
            out_text = ""

        for line in out_text.splitlines():
            sep = line.find("\x00")
            if sep <= 0:
                continue
            rel = line[:sep].lstrip("./")
            snip = line[sep + 1 :].strip()
            p = HOME / rel
            sp = str(p)
            if sp in seen:
                continue
            if not matches_filter_kind(p, False, filter_kind):
                continue
            item = make_result_item(p, is_dir=False, snippet=snip)
            if item:
                seen.add(sp)
                results.append(item)
                if len(results) >= limit:
                    break

    return results, cached_count


# ---------------- Full incremental on-demand indexer (Ctrl+I) ----------------


def run_index() -> int:
    cfg = load_config()
    con = open_fts()
    known = {row[0]: (row[1], row[2]) for row in con.execute("SELECT path, mtime_ns, size FROM docs_meta")}

    cmd = fd_base_cmd(cfg) + ["-t", "f"]
    for ext in sorted(DOC_EXTS):
        cmd.extend(["-e", ext.lstrip(".")])
    cmd.append(".")
    proc = subprocess.run(
        cmd,
        cwd=str(HOME),
        stdout=subprocess.PIPE,
        stderr=subprocess.DEVNULL,
        text=True,
        check=False,
    )

    all_docs: list[Path] = []
    todo: list[tuple[int, Path, int]] = []
    present_paths: set[str] = set()

    for rel in proc.stdout.splitlines():
        if not rel:
            continue
        p = HOME / rel
        if p.name.startswith("._") or "__MACOSX" in p.parts:
            continue
        sp = str(p)
        present_paths.add(sp)
        all_docs.append(p)
        try:
            st = p.stat()
        except OSError:
            continue
        prev = known.get(sp)
        if prev and prev[0] == st.st_mtime_ns and prev[1] == st.st_size:
            continue
        todo.append((st.st_mtime_ns, p, st.st_size))

    # Prune deleted paths from cache
    stale = [sp for sp in known if sp not in present_paths]
    if stale:
        with con:
            for sp in stale:
                con.execute("DELETE FROM docs_fts WHERE path = ?", (sp,))
                con.execute("DELETE FROM docs_meta WHERE path = ?", (sp,))

    # Index newest documents first so recent years become searchable first
    todo.sort(key=lambda x: x[0], reverse=True)
    total_all = len(all_docs)
    already_cached = total_all - len(todo)
    print(json.dumps({"done": already_cached, "total": total_all, "finished": len(todo) == 0}), flush=True)

    if not todo:
        con.close()
        return 0

    max_bytes = int(cfg.get("maxIndexedTextBytes", 350_000))
    workers = max(2, min(6, (os.cpu_count() or 4) // 2))

    def _worker(entry: tuple[int, Path, int]) -> tuple[str, str, str, int, int]:
        mtime_ns, p, size = entry
        body = extract_document_text(p, max_bytes=max_bytes)
        return (str(p), p.name, body, mtime_ns, size)

    done = already_cached
    batch: list[tuple[str, str, str, int, int]] = []

    with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as pool:
        for res in pool.map(_worker, todo, chunksize=8):
            batch.append(res)
            done += 1
            if len(batch) >= 100 or done == total_all:
                with con:
                    for sp, name, body, mtime_ns, size in batch:
                        con.execute("DELETE FROM docs_fts WHERE path = ?", (sp,))
                        con.execute("INSERT INTO docs_fts(path, name, body) VALUES (?, ?, ?)", (sp, name, body))
                        con.execute(
                            "INSERT OR REPLACE INTO docs_meta(path, mtime_ns, size) VALUES (?, ?, ?)",
                            (sp, mtime_ns, size),
                        )
                batch.clear()
                print(json.dumps({"done": done, "total": total_all, "finished": done >= total_all}), flush=True)

    con.close()
    return 0


# ---------------- Preview & Actions ----------------


def render_pdf_page1(path: Path, mtime_ns: int) -> str:
    ensure_dirs()
    key = hashlib.sha1(f"{path}:{mtime_ns}".encode("utf-8")).hexdigest()[:20]
    out_png = PREVIEW_DIR / f"{key}.png"
    if out_png.is_file():
        return str(out_png)
    prefix = PREVIEW_DIR / key
    try:
        subprocess.run(
            ["pdftoppm", "-png", "-r", "110", "-f", "1", "-l", "1", "-singlefile", "--", str(path), str(prefix)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
        if out_png.is_file():
            return str(out_png)
    except Exception:
        pass
    return ""


def build_preview(path_str: str, query: str = "") -> dict:
    p = Path(path_str)
    if not p.exists():
        return {"kind": "info", "imagePath": "", "text": "", "meta": "", "inDropbox": False}

    try:
        st = p.stat()
    except OSError:
        return {"kind": "info", "imagePath": "", "text": "", "meta": "", "inDropbox": False}

    in_dbx = is_in_dropbox(p)

    if p.is_dir():
        entries: list[str] = []
        try:
            children = sorted(
                (c for c in p.iterdir() if not c.name.startswith(".")),
                key=lambda c: (not c.is_dir(), c.name.lower()),
            )
            for child in children[:60]:
                is_d = child.is_dir()
                entries.append(f"{glyph_for(child, is_d)}  {child.name}{'/' if is_d else ''}")
        except OSError:
            pass
        folder_label = "Dossier" if IS_FR else "Folder"
        meta = f"{folder_label} · {human_date(st.st_mtime)}"
        return {
            "kind": "folder",
            "imagePath": "",
            "text": "\n".join(entries),
            "meta": meta,
            "inDropbox": in_dbx,
        }

    ext = p.suffix.lower()
    meta = f"{human_size(st.st_size)} · {human_date(st.st_mtime)}"

    if ext in IMAGE_EXTS:
        return {
            "kind": "image",
            "imagePath": str(p),
            "text": "",
            "meta": meta,
            "inDropbox": in_dbx,
        }

    if ext == ".pdf":
        img = render_pdf_page1(p, st.st_mtime_ns)
        snippet = ""
        if query.strip() or not img:
            try:
                con = open_fts()
                row = con.execute("SELECT body FROM docs_fts WHERE path = ? LIMIT 1", (str(p),)).fetchone()
                con.close()
                body = row[0] if row and row[0] else extract_pdf(p, 80_000, timeout_sec=1.5)
                snippet = make_snippet_around(body, query, window=260) if query.strip() else body[:2500].strip()
            except Exception:
                snippet = ""
        return {
            "kind": "pdf",
            "imagePath": img,
            "text": snippet,
            "meta": meta + " · PDF",
            "inDropbox": in_dbx,
        }

    if ext in DOC_EXTS:
        body = ""
        try:
            con = open_fts()
            row = con.execute("SELECT body FROM docs_fts WHERE path = ? LIMIT 1", (str(p),)).fetchone()
            con.close()
            if row and row[0]:
                body = row[0]
        except Exception:
            pass
        if not body:
            body = extract_document_text(p, max_bytes=60_000)
        if query.strip() and body:
            text_out = make_snippet_around(body, query, window=1400)
        else:
            text_out = body[:3500].strip()
        return {
            "kind": "text",
            "imagePath": "",
            "text": text_out,
            "meta": meta,
            "inDropbox": in_dbx,
        }

    if ext in TEXT_EXTS or st.st_size <= 256_000:
        try:
            raw = p.read_bytes()[:48_000]
            if b"\x00" not in raw[:1024]:
                decoded = raw.decode("utf-8", errors="replace")
                if query.strip():
                    decoded = make_snippet_around(decoded, query, window=1600)
                else:
                    decoded = "\n".join(decoded.splitlines()[:80])
                return {
                    "kind": "text",
                    "imagePath": "",
                    "text": decoded,
                    "meta": meta,
                    "inDropbox": in_dbx,
                }
        except Exception:
            pass

    return {
        "kind": "info",
        "imagePath": "",
        "text": str(p),
        "meta": meta,
        "inDropbox": in_dbx,
    }


def launch_detached_xdg_open(target: Path) -> None:
    cmd = ["uwsm-app", "--", "xdg-open", str(target)] if shutil.which("uwsm-app") else ["xdg-open", str(target)]
    subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)


def notify(headline: str, description: str = "", glyph: str = "󰈔") -> None:
    if shutil.which("omarchy-notification-send"):
        subprocess.run(
            ["omarchy-notification-send", "-g", glyph, headline, description],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def run_action(action: str, path_str: str) -> int:
    p = Path(path_str)
    if not p.exists():
        return 1

    if action == "open":
        launch_detached_xdg_open(p)
        return 0

    if action == "reveal":
        if p.is_dir():
            launch_detached_xdg_open(p)
            return 0
        uri = "file://" + urllib.parse.quote(str(p.resolve()))
        res = subprocess.run(
            [
                "gdbus",
                "call",
                "--session",
                "--dest",
                "org.freedesktop.FileManager1",
                "--object-path",
                "/org/freedesktop/FileManager1",
                "--method",
                "org.freedesktop.FileManager1.ShowItems",
                f"['{uri}']",
                "",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=2.5,
            check=False,
        )
        if res.returncode != 0:
            launch_detached_xdg_open(p.parent)
        return 0

    if action == "dropbox-link":
        if not shutil.which("dropbox-cli"):
            notify("Dropbox CLI introuvable" if IS_FR else "Dropbox CLI not found", p.name, "󰇣")
            return 1
        proc = subprocess.run(
            ["dropbox-cli", "sharelink", str(p)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=6.0,
            check=False,
        )
        link = proc.stdout.strip()
        if proc.returncode == 0 and link.startswith("http"):
            subprocess.run(["wl-copy", "--", link], check=False)
            notify("Lien Dropbox copié" if IS_FR else "Dropbox link copied", p.name, "󰇣")
            return 0
        fallback_err = "Impossible de générer le lien" if IS_FR else "Unable to generate share link"
        err = (proc.stderr or proc.stdout).strip() or fallback_err
        notify("Erreur Dropbox" if IS_FR else "Dropbox error", err[:120], "󰇣")
        return 1

    if action == "copy-path":
        subprocess.run(["wl-copy", "--", str(p)], check=False)
        notify("Chemin copié" if IS_FR else "Path copied", str(p), "󰆏")
        return 0

    if action == "trash":
        res = subprocess.run(["gio", "trash", "--", str(p)], check=False)
        if res.returncode == 0:
            notify("Déplacé dans la corbeille" if IS_FR else "Moved to trash", p.name, "󰩺")
        return res.returncode

    return 1


def main() -> int:
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("refresh-paths")
    sub.add_parser("index")

    pq = sub.add_parser("query")
    pq.add_argument("--mode", choices=("name", "content"), default="name")
    pq.add_argument("--limit", type=int, default=50)
    pq.add_argument("--text", default=None)
    pq.add_argument("query", nargs="?", default="")

    pp = sub.add_parser("preview")
    pp.add_argument("--query", default="")
    pp.add_argument("path")

    pa = sub.add_parser("action")
    pa.add_argument("kind", choices=("open", "reveal", "dropbox-link", "copy-path", "trash"))
    pa.add_argument("path")

    args = parser.parse_args()

    if args.cmd == "refresh-paths":
        count = refresh_paths_cache()
        con = open_fts()
        cached = fts_stats(con)
        con.close()
        print(json.dumps({"paths": count, "cachedDocs": cached}))
        return 0

    if args.cmd == "index":
        return run_index()

    if args.cmd == "query":
        raw_q = args.text if args.text is not None else args.query
        eff_mode, filter_kind, clean_q = parse_prefix(raw_q, args.mode)
        if not clean_q:
            con = open_fts()
            cached = fts_stats(con)
            con.close()
            items = [it for it in load_recent(args.limit) if matches_filter_kind(Path(it["path"]), it["isDir"], filter_kind)]
            print(json.dumps({"mode": eff_mode, "recent": True, "results": items, "cachedDocs": cached}, ensure_ascii=False))
            return 0

        if eff_mode == "content":
            items, cached = search_content(clean_q, filter_kind, args.limit)
            print(json.dumps({"mode": "content", "recent": False, "results": items, "cachedDocs": cached}, ensure_ascii=False))
            return 0

        items = search_names(clean_q, filter_kind, args.limit)
        print(json.dumps({"mode": "name", "recent": False, "results": items, "cachedDocs": 0}, ensure_ascii=False))
        return 0

    if args.cmd == "preview":
        _, _, clean_q = parse_prefix(args.query, "name")
        print(json.dumps(build_preview(args.path, clean_q), ensure_ascii=False))
        return 0

    if args.cmd == "action":
        return run_action(args.kind, args.path)

    return 1


if __name__ == "__main__":
    sys.exit(main())

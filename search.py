#!/usr/bin/env python3
"""Spotlight-style search & preview backend for Spotlight Files (Omarchy Quickshell).

Zero permanent background daemon:
- Filename search: fd-cached relative paths ranked by an in-process fzf-style (--scheme=path) fuzzy
  matcher over private case/accent-folded mirrors (~25-60ms on 220k files); the query never reaches
  any child process argv.
- Content search: SQLite FTS5 local cache (unicode61 remove_diacritics 2) for PDFs & Office
  documents + live ripgrep (rg --fixed-strings -f -) for text/markdown/code.
- Preview: PDF page rendering (pdftoppm -scale-to), images, streaming Office XML text, text/code, folder listing.
  PDFs are handed to pdftotext/pdftoppm as /dev/stdin so document paths stay out of argv as well.
"""

from __future__ import annotations

import argparse
import concurrent.futures
import datetime as dt
import hashlib
import heapq
import io
import json
import os
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import time
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

# Restrict all newly created cache directories (0700) and files (0600) — including
# SQLite FTS5/WAL/SHM files, mkstemp artifacts, and child-process outputs such as
# pdftoppm PNG previews — while preserving the caller's original umask so user
# desktop applications launched via xdg-open keep normal file creation permissions.
_ORIG_UMASK = os.umask(0o077)


def _install_signal_handlers() -> None:
    """Convert SIGTERM/SIGINT into SystemExit so subprocess.run kills children and runs finally blocks."""
    def _handle_signal(signum: int, _frame: object) -> None:
        sys.exit(128 + signum)

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)


def _xdg_dir(env_name: str, fallback: Path) -> Path:
    val = os.environ.get(env_name, "").strip()
    if val and os.path.isabs(val):
        return Path(val)
    return fallback


PLUGIN_ID = "io.github.thibaudberdin.spotlight-files"
HOME = Path.home()
XDG_CONFIG = _xdg_dir("XDG_CONFIG_HOME", HOME / ".config")
CACHE_DIR = _xdg_dir("XDG_CACHE_HOME", HOME / ".cache") / PLUGIN_ID
CONFIG_PATH = XDG_CONFIG / "omarchy" / "spotlight-files.json"
LANGUAGE_PATH = XDG_CONFIG / "omarchy" / "language"
PATHS_CACHE = CACHE_DIR / "paths.txt"
RECENT_CACHE = CACHE_DIR / "recent.json"
PREVIEW_DIR = CACHE_DIR / "previews"
FTS_DB = CACHE_DIR / "fts.sqlite"

MAX_XML_MEMBER_BYTES = 8 * 1024 * 1024
MAX_DOC_UNCOMPRESSED_BYTES = 24 * 1024 * 1024
MAX_DOC_PARSE_SECONDS = 2.5
MAX_PREVIEW_IMAGE_BYTES = 25 * 1024 * 1024
MAX_CACHED_PREVIEWS = 150


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
SAFE_PREVIEW_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
ARCHIVE_EXTS = {".zip", ".tar", ".gz", ".tgz", ".xz", ".zst", ".7z", ".rar", ".bz2"}
MEDIA_EXTS = {".mp3", ".wav", ".flac", ".ogg", ".m4a", ".mp4", ".mkv", ".mov", ".webm", ".avi"}

RG_GLOBS = [f"*{ext}" for ext in sorted(TEXT_EXTS)]


def _secure_dir(path: Path) -> None:
    """Ensure directory exists, is not a symlink, is owned by current UID, and has mode 0700 via fd."""
    if path.is_symlink():
        raise RuntimeError(f"Refusing symlinked cache directory: {path}")
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.getuid():
            raise RuntimeError(f"Invalid cache directory ownership or type: {path}")
        if (st.st_mode & 0o777) != 0o700:
            os.fchmod(fd, 0o700)
    finally:
        os.close(fd)


def _purge_insecure_or_stale_files(directory: Path, is_preview_dir: bool = False) -> None:
    """Unlink symlinks, legacy group/other-readable cache files (new inode revokes open fds), and old temp files."""
    now = time.time()
    png_entries: list[tuple[float, str]] = []
    try:
        with os.scandir(directory) as it:
            for entry in it:
                try:
                    st = entry.stat(follow_symlinks=False)
                except OSError:
                    continue
                if stat.S_ISLNK(st.st_mode) or (
                    stat.S_ISREG(st.st_mode) and st.st_uid == os.getuid() and (st.st_mode & 0o077) != 0
                ):
                    try:
                        os.unlink(entry.path)
                        if entry.name == FTS_DB.name:
                            for suffix in ("-wal", "-shm", "-journal"):
                                sidecar = directory / f"{FTS_DB.name}{suffix}"
                                if sidecar.exists() or sidecar.is_symlink():
                                    sidecar.unlink()
                    except OSError:
                        pass
                    continue
                if is_preview_dir and stat.S_ISREG(st.st_mode):
                    if entry.name.startswith(".tmp.") and (now - st.st_mtime) > 30.0:
                        try:
                            os.unlink(entry.path)
                        except OSError:
                            pass
                    elif entry.name.endswith(".png"):
                        png_entries.append((st.st_mtime, entry.path))
    except OSError:
        return

    if is_preview_dir and len(png_entries) > MAX_CACHED_PREVIEWS:
        png_entries.sort(key=lambda x: x[0], reverse=True)
        for _, old_path in png_entries[MAX_CACHED_PREVIEWS:]:
            try:
                os.unlink(old_path)
            except OSError:
                pass


def ensure_dirs() -> None:
    _secure_dir(CACHE_DIR)
    _secure_dir(PREVIEW_DIR)
    _purge_insecure_or_stale_files(CACHE_DIR, is_preview_dir=False)
    _purge_insecure_or_stale_files(PREVIEW_DIR, is_preview_dir=True)


def load_config() -> dict:
    cfg = {
        "excludes": list(DEFAULT_EXCLUDES),
        "maxIndexedTextBytes": 350_000,
    }
    if CONFIG_PATH.is_file():
        try:
            user_cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(user_cfg, dict):
                raw_ex = user_cfg.get("excludes")
                if isinstance(raw_ex, list):
                    clean_ex = [s.strip() for s in raw_ex if isinstance(s, str) and 0 < len(s.strip()) <= 256]
                    cfg["excludes"] = clean_ex[:100]
                raw_max = user_cfg.get("maxIndexedTextBytes")
                if isinstance(raw_max, (int, float)) and not isinstance(raw_max, bool):
                    cfg["maxIndexedTextBytes"] = max(4_096, min(2_000_000, int(raw_max)))
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
        is_dir = stat.S_ISDIR(st.st_mode)
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
        cmd.append(f"--exclude={ex}")
    cmd.extend(["--exclude=._*", "--exclude=~$*"])
    return cmd


def refresh_paths_cache() -> int:
    ensure_dirs()
    cfg = load_config()
    cmd = fd_base_cmd(cfg) + ["."]
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(HOME),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=25.0,
            check=False,
        )
        raw_stdout = proc.stdout or ""
    except Exception:
        raw_stdout = ""

    lines = [ln.rstrip("/") for ln in raw_stdout.splitlines() if ln and not ln.startswith(".") and "\ufffd" not in ln]

    text = "\n".join(lines) + ("\n" if lines else "")
    fd_tmp, tmp_name = tempfile.mkstemp(prefix="paths.", dir=str(CACHE_DIR))
    try:
        with os.fdopen(fd_tmp, "wb") as fh:
            fh.write(text.encode("utf-8", errors="replace"))
        # Derived folded caches are bound to this exact file (inode/size/mtime) and published
        # before paths.txt itself, so readers never pair a new paths.txt with stale offsets.
        try:
            write_fold_caches(text, os.stat(tmp_name))
        except Exception:
            pass
        os.replace(tmp_name, PATHS_CACHE)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.unlink(tmp_name)
            except OSError:
                pass

    recent_candidates: list[tuple[float, Path]] = []
    for window in ("14d", "90d"):
        rec_cmd = fd_base_cmd(cfg) + ["-t", "f", f"--changed-within={window}", "."]
        try:
            rec_proc = subprocess.run(
                rec_cmd,
                cwd=str(HOME),
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                encoding="utf-8",
                errors="replace",
                timeout=15.0,
                check=False,
            )
            rec_stdout = rec_proc.stdout or ""
        except Exception:
            rec_stdout = ""
        recent_candidates.clear()
        for rel in rec_stdout.splitlines():
            if not rel or "\ufffd" in rel:
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
    try:
        with os.fdopen(fd_rtmp, "w", encoding="utf-8") as fh:
            json.dump(recent_items, fh, ensure_ascii=False)
        os.replace(rtmp_name, RECENT_CACHE)
    finally:
        if os.path.exists(rtmp_name):
            try:
                os.unlink(rtmp_name)
            except OSError:
                pass

    try:
        con = open_fts()
        try:
            opportunistic_index_recent(con, cfg, window="14d", budget_sec=0.35)
        finally:
            con.close()
    except sqlite3.OperationalError:
        pass

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
    # IMPORTANT: ensure_dirs() MUST run BEFORE sqlite3.connect(). SQLite copies the
    # main database file's mode to -wal/-shm files regardless of process umask, so
    # purging/recreating any legacy >0600 DB file beforehand guarantees 0600 WAL/SHM.
    ensure_dirs()
    if FTS_DB.is_symlink():
        raise RuntimeError(f"Refusing symlinked FTS database: {FTS_DB}")
    con = sqlite3.connect(str(FTS_DB), timeout=10.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS docs_meta (
            path TEXT PRIMARY KEY,
            mtime_ns INTEGER NOT NULL,
            size INTEGER NOT NULL,
            fts_rowid INTEGER
        )
        """
    )
    cols = {row[1] for row in con.execute("PRAGMA table_info(docs_meta)")}
    if "fts_rowid" not in cols:
        con.execute("ALTER TABLE docs_meta ADD COLUMN fts_rowid INTEGER")
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


def _upsert_fts_batch(con: sqlite3.Connection, rows: list[tuple[str, str, str, int, int]]) -> None:
    """Replace indexed document entries in O(log N) using docs_meta.fts_rowid instead of scanning UNINDEXED path."""
    with con:
        for sp, name, body, mtime_ns, size in rows:
            prev = con.execute("SELECT fts_rowid FROM docs_meta WHERE path = ?", (sp,)).fetchone()
            if prev is not None:
                if prev[0] is not None:
                    con.execute("DELETE FROM docs_fts WHERE rowid = ?", (prev[0],))
                else:
                    con.execute("DELETE FROM docs_fts WHERE path = ?", (sp,))
            cur = con.execute("INSERT INTO docs_fts(path, name, body) VALUES (?, ?, ?)", (sp, name, body))
            con.execute(
                "INSERT OR REPLACE INTO docs_meta(path, mtime_ns, size, fts_rowid) VALUES (?, ?, ?, ?)",
                (sp, mtime_ns, size, cur.lastrowid),
            )


def _lookup_cached_body(con: sqlite3.Connection, path_str: str) -> str:
    row = con.execute(
        """
        SELECT f.body
        FROM docs_meta m
        JOIN docs_fts f ON f.rowid = m.fts_rowid
        WHERE m.path = ?
        LIMIT 1
        """,
        (path_str,),
    ).fetchone()
    if row and row[0]:
        return str(row[0])
    row_fallback = con.execute(
        "SELECT body FROM docs_fts WHERE path = ? LIMIT 1",
        (path_str,),
    ).fetchone()
    return str(row_fallback[0]) if row_fallback and row_fallback[0] else ""


def _natural_key(name: str) -> list[int | str]:
    return [int(part) if part.isdigit() else part.lower() for part in re.split(r"(\d+)", name)]


class _XmlBudgetExceeded(Exception):
    pass


class _BoundedZipMember(io.RawIOBase):
    """Streams a ZIP entry while enforcing per-member byte cap, per-document budget, and wall-clock deadline."""

    def __init__(self, raw: io.BufferedIOBase, budget: list[int], deadline: float) -> None:
        self._raw = raw
        self._budget = budget
        self._deadline = deadline
        self._member_read = 0
        self._checked_head = False

    def readable(self) -> bool:
        return True

    def readinto(self, b: bytearray | memoryview) -> int:
        if time.monotonic() > self._deadline:
            raise _XmlBudgetExceeded("XML parse deadline exceeded")
        chunk = self._raw.read(len(b))
        if not chunk:
            return 0
        if not self._checked_head:
            self._checked_head = True
            upper_head = chunk[:4096].upper()
            if b"<!ENTITY" in upper_head or b"<!DOCTYPE" in upper_head:
                raise _XmlBudgetExceeded("DTD/ENTITY declarations rejected")
        n = len(chunk)
        self._member_read += n
        self._budget[0] -= n
        if self._member_read > MAX_XML_MEMBER_BYTES or self._budget[0] < 0:
            raise _XmlBudgetExceeded("XML uncompressed size budget exceeded")
        b[:n] = chunk
        return n

    def close(self) -> None:
        try:
            self._raw.close()
        finally:
            super().close()


def _iter_safe_xml(zf: zipfile.ZipFile, member: str, budget: list[int], deadline: float):
    try:
        info = zf.getinfo(member)
    except KeyError:
        return None
    if info.file_size > MAX_XML_MEMBER_BYTES or info.file_size > budget[0]:
        return None
    raw = zf.open(info, "r")
    bounded = io.BufferedReader(_BoundedZipMember(raw, budget, deadline))
    return ET.iterparse(bounded, events=("end",))


def extract_docx(path: Path, max_bytes: int) -> str:
    parts: list[str] = []
    total = 0
    budget = [MAX_DOC_UNCOMPRESSED_BYTES]
    deadline = time.monotonic() + MAX_DOC_PARSE_SECONDS
    with zipfile.ZipFile(path, "r") as zf:
        names = [
            n for n in zf.namelist()
            if n.startswith("word/") and n.endswith(".xml") and ("document" in n or "header" in n or "footer" in n)
        ]
        for name in sorted(names, key=_natural_key)[:40]:
            it = _iter_safe_xml(zf, name, budget, deadline)
            if it is None:
                continue
            try:
                for _, elem in it:
                    tag = elem.tag.rsplit("}", 1)[-1]
                    if tag == "t" and elem.text:
                        parts.append(elem.text)
                        total += len(elem.text)
                    elif tag in ("p", "tr", "br"):
                        parts.append("\n")
                    elif tag == "tab":
                        parts.append("\t")
                    elem.clear()
                    if total >= max_bytes:
                        break
            except (_XmlBudgetExceeded, ET.ParseError):
                break
            if total >= max_bytes:
                break
    return "".join(parts)[:max_bytes]


def extract_xlsx(path: Path, max_bytes: int) -> str:
    out: list[str] = []
    total = 0
    budget = [MAX_DOC_UNCOMPRESSED_BYTES]
    deadline = time.monotonic() + MAX_DOC_PARSE_SECONDS
    with zipfile.ZipFile(path, "r") as zf:
        shared: list[str] = []
        it_shared = _iter_safe_xml(zf, "xl/sharedStrings.xml", budget, deadline)
        if it_shared is not None:
            cur_si: list[str] = []
            try:
                for _, elem in it_shared:
                    tag = elem.tag.rsplit("}", 1)[-1]
                    if tag == "t" and elem.text:
                        cur_si.append(elem.text)
                    elif tag == "si":
                        shared.append("".join(cur_si))
                        cur_si.clear()
                        elem.clear()
                        if len(shared) >= 200_000:
                            break
            except (_XmlBudgetExceeded, ET.ParseError):
                pass

        sheet_names: list[str] = []
        it_wb = _iter_safe_xml(zf, "xl/workbook.xml", budget, deadline)
        if it_wb is not None:
            try:
                for _, elem in it_wb:
                    if elem.tag.rsplit("}", 1)[-1] == "sheet" and elem.attrib.get("name"):
                        sheet_names.append(elem.attrib["name"])
                    elem.clear()
            except (_XmlBudgetExceeded, ET.ParseError):
                pass

        sheets = sorted(
            (n for n in zf.namelist() if n.startswith("xl/worksheets/sheet") and n.endswith(".xml")),
            key=_natural_key,
        )
        for idx, sname in enumerate(sheets[:50]):
            it_sheet = _iter_safe_xml(zf, sname, budget, deadline)
            if it_sheet is None:
                continue
            default_sheet = f"Feuille {idx + 1}" if IS_FR else f"Sheet {idx + 1}"
            header = sheet_names[idx] if idx < len(sheet_names) else default_sheet
            out.append(f"=== {header} ===\n")
            row_vals: list[str] = []
            cell_inline: list[str] = []
            cell_v: str = ""
            try:
                for _, elem in it_sheet:
                    tag = elem.tag.rsplit("}", 1)[-1]
                    if tag == "t" and elem.text:
                        cell_inline.append(elem.text)
                    elif tag == "v" and elem.text:
                        cell_v = elem.text
                    elif tag == "c":
                        ctype = elem.attrib.get("t", "")
                        val_text = ""
                        if ctype == "inlineStr":
                            val_text = "".join(cell_inline)
                        elif cell_v:
                            if ctype == "s":
                                try:
                                    sidx = int(cell_v)
                                    val_text = shared[sidx] if 0 <= sidx < len(shared) else cell_v
                                except ValueError:
                                    val_text = cell_v
                            else:
                                val_text = cell_v
                        if val_text:
                            row_vals.append(val_text)
                        cell_inline.clear()
                        cell_v = ""
                        elem.clear()
                    elif tag == "row":
                        if row_vals:
                            line = "\t".join(row_vals) + "\n"
                            out.append(line)
                            total += len(line)
                            row_vals.clear()
                        elem.clear()
                        if total >= max_bytes:
                            return "".join(out)[:max_bytes]
            except (_XmlBudgetExceeded, ET.ParseError):
                break
    return "".join(out)[:max_bytes]


def extract_pptx(path: Path, max_bytes: int) -> str:
    out: list[str] = []
    total = 0
    budget = [MAX_DOC_UNCOMPRESSED_BYTES]
    deadline = time.monotonic() + MAX_DOC_PARSE_SECONDS
    with zipfile.ZipFile(path, "r") as zf:
        slides = sorted(
            (n for n in zf.namelist() if n.startswith("ppt/slides/slide") and n.endswith(".xml")),
            key=_natural_key,
        )
        for idx, sname in enumerate(slides[:200], 1):
            it_slide = _iter_safe_xml(zf, sname, budget, deadline)
            if it_slide is None:
                continue
            texts: list[str] = []
            try:
                for _, elem in it_slide:
                    if elem.tag.rsplit("}", 1)[-1] == "t" and elem.text:
                        texts.append(elem.text)
                    elem.clear()
            except (_XmlBudgetExceeded, ET.ParseError):
                break
            if texts:
                block = f"--- Slide {idx} ---\n" + " ".join(texts) + "\n"
                out.append(block)
                total += len(block)
                if total >= max_bytes:
                    break
    return "".join(out)[:max_bytes]


def extract_odf(path: Path, max_bytes: int) -> str:
    budget = [MAX_DOC_UNCOMPRESSED_BYTES]
    deadline = time.monotonic() + MAX_DOC_PARSE_SECONDS
    with zipfile.ZipFile(path, "r") as zf:
        it_odf = _iter_safe_xml(zf, "content.xml", budget, deadline)
        if it_odf is None:
            return ""
        chunks: list[str] = []
        total = 0
        try:
            for _, elem in it_odf:
                if elem.text:
                    chunks.append(elem.text)
                    total += len(elem.text)
                tag = elem.tag.rsplit("}", 1)[-1]
                if tag in ("p", "h", "table-row"):
                    chunks.append("\n")
                elif tag == "table-cell":
                    chunks.append("\t")
                elem.clear()
                if total >= max_bytes:
                    break
        except (_XmlBudgetExceeded, ET.ParseError):
            pass
        return "".join(chunks)[:max_bytes]


def open_pdf_for_child_stdin(path: Path) -> io.BufferedReader | None:
    """Open a regular file read-only so a poppler child can read it as /dev/stdin.

    Handing the document over as an inherited stdin descriptor (and naming it "/dev/stdin") keeps the
    confidential document path out of the child's world-readable /proc/<pid>/cmdline. "/dev/stdin" is
    preferred over "-" because poppler then re-opens the same regular file (seekable, lazily paged)
    instead of copying the whole PDF from a pipe into memory. O_NONBLOCK guards against FIFOs.
    """
    flags = os.O_RDONLY | getattr(os, "O_NONBLOCK", 0) | getattr(os, "O_CLOEXEC", 0)
    try:
        fd = os.open(path, flags)
    except OSError:
        return None
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            os.close(fd)
            return None
        os.set_blocking(fd, True)
        return os.fdopen(fd, "rb")
    except OSError:
        os.close(fd)
        return None


def extract_pdf(path: Path, max_bytes: int, timeout_sec: float = 3.5) -> str:
    """Stream at most max_bytes from pdftotext and terminate early so giant PDFs never buffer in RAM."""
    proc: subprocess.Popen[bytes] | None = None
    src = open_pdf_for_child_stdin(path)
    if src is None:
        return ""
    try:
        with src:
            proc = subprocess.Popen(
                ["pdftotext", "-q", "-nopgbrk", "-l", "120", "/dev/stdin", "-"],
                stdin=src,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            )
        deadline = time.monotonic() + timeout_sec
        chunks: list[bytes] = []
        total = 0
        assert proc.stdout is not None
        while total < max_bytes:
            if time.monotonic() > deadline:
                break
            buf = proc.stdout.read(min(32_768, max_bytes - total))
            if not buf:
                break
            chunks.append(buf)
            total += len(buf)
        return b"".join(chunks).decode("utf-8", errors="replace")
    except Exception:
        return ""
    finally:
        if proc is not None:
            if proc.poll() is None:
                proc.kill()
            try:
                proc.wait(timeout=1.0)
            except Exception:
                pass
            if proc.stdout is not None:
                proc.stdout.close()


def extract_document_text(path: Path, max_bytes: int = 350_000) -> str:
    if path.name.startswith("._") or "__MACOSX" in path.parts:
        return ""
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode):
            return ""
    except OSError:
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
    """Split punctuation/apostrophes/hyphens cleanly (e.g. \"l'été\", \"rendez-vous\") into safe FTS5 prefix tokens."""
    tokens = re.findall(r"[\w]+", raw, flags=re.UNICODE)
    if not tokens:
        return ""
    return " AND ".join(f'"{tok}"*' for tok in tokens[:24])


def strip_accents(s: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFD", s) if unicodedata.category(c) != "Mn")


def make_snippet_around(text: str, query: str, window: int = 140) -> str:
    if not text:
        return ""
    norm_text = strip_accents(text).lower()
    terms = [strip_accents(t).lower() for t in re.findall(r"[\w]+", query, flags=re.UNICODE) if len(t) >= 2]
    if not terms:
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
    cmd.extend([f"--changed-within={window}", "."])
    try:
        proc = subprocess.run(
            cmd,
            cwd=str(HOME),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=8.0,
            check=False,
        )
        raw_out = proc.stdout or ""
    except Exception:
        return

    candidates: list[tuple[int, Path, int]] = []
    for rel in raw_out.splitlines():
        if not rel or "\ufffd" in rel:
            continue
        p = HOME / rel
        if p.name.startswith("._") or "__MACOSX" in p.parts:
            continue
        try:
            st = p.stat()
        except OSError:
            continue
        if not stat.S_ISREG(st.st_mode):
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
        _upsert_fts_batch(con, rows_to_save)


# ---------------- In-process fuzzy path matcher (replaces `fzf --filter`) ----------------
#
# The Names-mode query must never appear in any child's argv (/proc/<pid>/cmdline is world-readable),
# so path matching runs entirely inside this process. To stay fast on 200k+ paths, refresh-paths also
# writes two private derived caches next to paths.txt:
#   paths.fold — paths.txt lowercased and accent-stripped *character for character* (same length, so
#                every offset in paths.fold is the same offset in paths.txt). Characters that have no
#                ASCII folding (’, °, œ, CJK…) become the placeholder \x7f, keeping the text pure ASCII
#                (compact 1-byte str, fastest `re`/`str.find` paths);
#   names.fold — a header binding it to the exact paths.txt (inode/size/mtime/length) it was derived
#                from, then one "<folded basename>\0<line offset>" record per path; offsets are written
#                in hexadecimal with the letters A–P, which can never occur in a (lowercase) folded term.
# Candidates are located with C-speed literal searches (str.find) and possessive, backtracking-free
# regexes over those folded texts; only a bounded candidate set is then re-folded exactly and scored in
# Python with fzf-like `--scheme=path` rules (boundary/camelCase/consecutive bonuses, gap penalties),
# plus a filename-over-directory bonus.

PATHS_FOLD_CACHE = CACHE_DIR / "paths.fold"
NAMES_FOLD_CACHE = CACHE_DIR / "names.fold"
_FOLD_MAGIC = "#spotlight-files-fold-v2"

MAX_NAME_CANDIDATES = 2000
MAX_TIER_GATHER = 8_000
MAX_QUERY_TERMS = 8

# Single-character foldings Unicode decomposition does not provide (must keep a 1:1 length).
_FOLD_EXTRA = {"ø": "o", "ł": "l", "đ": "d", "ð": "d", "ħ": "h", "ı": "i", "ŧ": "t", "ŀ": "l"}
_NON_ASCII_RUN = re.compile(r"[^\x00-\x7f]+")
_NON_ASCII_CHAR = re.compile(r"[^\x00-\x7f]")
_LOSSY_PLACEHOLDER = "\x7f"
_HEX_TO_LETTERS = str.maketrans("0123456789abcdef", "ABCDEFGHIJKLMNOP")
_LETTERS_TO_HEX = str.maketrans("ABCDEFGHIJKLMNOP", "0123456789abcdef")
_fold_char_cache: dict[str, str] = {}
_fold_run_cache: dict[str, str] = {}


def _fold_char(c: str) -> str:
    r = _fold_char_cache.get(c)
    if r is None:
        low = c.lower()
        if len(low) != 1:
            low = c
        base = strip_accents(low)
        if len(base) == 1:
            base_low = base.lower()
            r = base_low if len(base_low) == 1 else base
        else:
            r = low  # e.g. a lone combining mark: keep it so lengths stay aligned
        r = _FOLD_EXTRA.get(r, r)
        _fold_char_cache[c] = r
    return r


def _fold_run(m: re.Match[str]) -> str:
    run = m.group()
    r = _fold_run_cache.get(run)
    if r is None:
        r = "".join(map(_fold_char, run))
        if len(_fold_run_cache) < 100_000:
            _fold_run_cache[run] = r
    return r


def fold_text(text: str) -> str:
    """Case- and accent-insensitive form of text with exactly the same length (offset compatible)."""
    if text.isascii():
        return text.lower()
    folded = _NON_ASCII_RUN.sub(_fold_run, text).lower()
    if len(folded) == len(text):
        return folded
    return "".join(c.lower() if c.isascii() else _fold_char(c) for c in text)


def lossy_ascii(folded: str) -> str:
    """Same-length pure-ASCII search form: any leftover non-ASCII char becomes the \\x7f placeholder.

    Applied identically to the cache and to query terms, so it can only add candidates (never lose
    one); each candidate is re-checked against its exact fold before scoring.
    """
    return folded if folded.isascii() else _NON_ASCII_CHAR.sub(_LOSSY_PLACEHOLDER, folded)


def query_terms(query: str) -> list[str]:
    """Space-separated, folded, de-duplicated search terms (accents in the query are ignored too)."""
    q = unicodedata.normalize("NFC", re.sub(r"[\x00-\x1f\x7f]", " ", query))
    folded = "".join(c for c in fold_text(q) if unicodedata.category(c) != "Mn")
    terms: list[str] = []
    for t in folded.split():
        if t not in terms:
            terms.append(t)
    return terms[:MAX_QUERY_TERMS]


def _read_private_text(path: Path) -> str:
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
    with os.fdopen(fd, "rb") as fh:
        return fh.read().decode("utf-8", errors="replace")


def _atomic_write_private(target: Path, text: str) -> None:
    fd, tmp = tempfile.mkstemp(prefix=f".{target.name}.", dir=str(target.parent))
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(text.encode("utf-8", errors="replace"))
        os.replace(tmp, target)
    finally:
        if os.path.exists(tmp):
            try:
                os.unlink(tmp)
            except OSError:
                pass


def _fold_header(st: os.stat_result, text_len: int) -> str:
    return f"{_FOLD_MAGIC} {st.st_ino}:{st.st_size}:{st.st_mtime_ns}:{text_len}"


def build_names_index(fold: str, header: str) -> str:
    parts = [header]
    off = 0
    for line in fold.split("\n"):
        if line:
            parts.append(f"{line[line.rfind('/') + 1:]}\x00{format(off, 'x').translate(_HEX_TO_LETTERS)}")
        off += len(line) + 1
    return "\n".join(parts) + "\n"


def _decode_offset(enc: str) -> int:
    return int(enc.translate(_LETTERS_TO_HEX), 16)


def write_fold_caches(text: str, st: os.stat_result) -> tuple[str, str]:
    """Derive and persist paths.fold / names.fold for the paths.txt content described by st."""
    fold = lossy_ascii(fold_text(text))
    names = build_names_index(fold, _fold_header(st, len(text)))
    try:
        _atomic_write_private(PATHS_FOLD_CACHE, fold)
        _atomic_write_private(NAMES_FOLD_CACHE, names)
    except Exception:
        pass
    return fold, names


class NameCaches:
    __slots__ = ("orig", "fold", "names")

    def __init__(self, orig: str, fold: str, names: str) -> None:
        self.orig = orig
        self.fold = fold
        self.names = names


def load_name_caches() -> NameCaches | None:
    if not PATHS_CACHE.is_file():
        refresh_paths_cache()
    try:
        fd = os.open(PATHS_CACHE, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0))
        with os.fdopen(fd, "rb") as fh:
            st = os.fstat(fh.fileno())
            orig = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return None

    fold: str | None = None
    names: str | None = None
    try:
        names = _read_private_text(NAMES_FOLD_CACHE)
        if names[: names.find("\n")] == _fold_header(st, len(orig)):
            fold = _read_private_text(PATHS_FOLD_CACHE)
            if len(fold) != len(orig):
                fold = None
    except OSError:
        fold = None
    if fold is None or names is None:
        try:
            ensure_dirs()
        except Exception:
            pass
        fold, names = write_fold_caches(orig, st)
    return NameCaches(orig, fold, names)


def _fuzzy_regex(term: str, stop: str) -> str:
    """Possessive left-to-right subsequence pattern: linear time, never backtracks."""
    return "".join(f"[^{re.escape(ch)}{stop}]*+{re.escape(ch)}" for ch in term)


def _names_body_start(names: str) -> int:
    """Offset just past the header line (records start there; each record is preceded by '\\n')."""
    return names.find("\n") + 1


# Tier iterators yield (line offset in paths.txt/paths.fold, basename length) in file order.


def _iter_name_prefix_hits(names: str, needle: str):
    """Paths whose folded basename starts with needle (literal C search)."""
    pat = "\n" + needle
    find = names.find
    pos = _names_body_start(names) - 1
    while True:
        i = find(pat, pos)
        if i < 0:
            return
        z = find("\x00", i + 1)
        if z < 0:
            return
        e = find("\n", z)
        if e < 0:
            e = len(names)
        if i + len(pat) <= z:
            yield _decode_offset(names[z + 1 : e]), z - i - 1
        pos = e


def _iter_name_substring_hits(names: str, needle: str):
    """Paths whose folded basename contains needle (literal C search)."""
    find = names.find
    n = len(needle)
    pos = _names_body_start(names)
    while True:
        i = find(needle, pos)
        if i < 0:
            return
        ls = names.rfind("\n", 0, i) + 1
        e = find("\n", i)
        if e < 0:
            e = len(names)
        z = find("\x00", ls, e)
        if z >= 0 and i + n <= z:
            yield _decode_offset(names[z + 1 : e]), z - ls
        pos = e + 1


def _iter_path_substring_hits(fold: str, needle: str):
    """Paths whose folded full relative path contains needle (literal C search)."""
    find = fold.find
    rfind = fold.rfind
    pos = 0
    while True:
        i = find(needle, pos)
        if i < 0:
            return
        ls = rfind("\n", 0, i) + 1
        e = find("\n", i)
        if e < 0:
            e = len(fold)
        yield ls, e - max(ls, rfind("/", ls, e) + 1)
        pos = e + 1


def _literal_first_fuzzy(term: str, stop: str) -> re.Pattern[str]:
    # Leading literal lets `re` jump between occurrences of term[0] in C; the rest never backtracks.
    return re.compile(re.escape(term[0]) + _fuzzy_regex(term[1:], stop))


def _iter_name_fuzzy_hits(names: str, term: str):
    """Paths whose folded basename contains term as a subsequence."""
    search = _literal_first_fuzzy(term, r"\n\x00").search
    find = names.find
    pos = _names_body_start(names)
    while True:
        m = search(names, pos)
        if m is None:
            return
        z = find("\x00", m.end())
        if z < 0:
            return
        e = find("\n", z)
        if e < 0:
            e = len(names)
        yield _decode_offset(names[z + 1 : e]), z - names.rfind("\n", 0, m.start()) - 1
        pos = e


def _iter_path_fuzzy_hits(fold: str, term: str):
    """Paths whose folded full relative path contains term as a subsequence."""
    search = _literal_first_fuzzy(term, r"\n").search
    find = fold.find
    rfind = fold.rfind
    pos = 0
    while True:
        m = search(fold, pos)
        if m is None:
            return
        ls = rfind("\n", 0, m.start()) + 1
        e = find("\n", m.end())
        if e < 0:
            e = len(fold)
        yield ls, e - max(ls, rfind("/", ls, e) + 1)
        if e >= len(fold):
            return
        pos = e


# fzf-compatible scoring constants (algo.go), `--scheme=path` flavour.
_SCORE_MATCH = 16
_SCORE_GAP_START = -3
_SCORE_GAP_EXT = -1
_BONUS_BOUNDARY = _SCORE_MATCH // 2
_BONUS_NONWORD = _SCORE_MATCH // 2
_BONUS_CAMEL123 = _BONUS_BOUNDARY + _SCORE_GAP_EXT
_BONUS_CONSECUTIVE = -(_SCORE_GAP_START + _SCORE_GAP_EXT)
_BONUS_FIRST_CHAR_MULT = 2
_BONUS_BOUNDARY_WHITE = _BONUS_BOUNDARY
_BONUS_BOUNDARY_DELIM = _BONUS_BOUNDARY + 1
# Extra relevance on top of fzf: matches inside the filename, and exact filename/stem hits.
_BONUS_BASENAME = 12
_BONUS_EXACT_NAME = 24

_C_WHITE, _C_NONWORD, _C_DELIM, _C_LOWER, _C_UPPER, _C_LETTER, _C_NUMBER = range(7)
_char_class_cache: dict[str, int] = {}


def _char_class(c: str) -> int:
    cls = _char_class_cache.get(c)
    if cls is None:
        if c == "/":
            cls = _C_DELIM
        elif c.isspace():
            cls = _C_WHITE
        elif c.isdigit():
            cls = _C_NUMBER
        elif c.isalpha():
            cls = _C_UPPER if c.isupper() else (_C_LOWER if c.islower() else _C_LETTER)
        else:
            cls = _C_NONWORD
        _char_class_cache[c] = cls
    return cls


def _compute_bonus(prev: int, cur: int) -> int:
    if cur > _C_NONWORD:
        if prev == _C_WHITE:
            return _BONUS_BOUNDARY_WHITE
        if prev == _C_DELIM:
            return _BONUS_BOUNDARY_DELIM
        if prev == _C_NONWORD:
            return _BONUS_BOUNDARY
    if (prev == _C_LOWER and cur == _C_UPPER) or (prev != _C_NUMBER and cur == _C_NUMBER):
        return _BONUS_CAMEL123
    if cur in (_C_NONWORD, _C_DELIM):
        return _BONUS_NONWORD
    if cur == _C_WHITE:
        return _BONUS_BOUNDARY_WHITE
    return 0


_BONUS_TABLE = [[_compute_bonus(p, c) for c in range(7)] for p in range(7)]


def _calc_score(orig_line: str, fold_line: str, term: str, sidx: int, eidx: int) -> int:
    """fzf v1 calculateScore over [sidx, eidx), visiting only the matched positions.

    Equivalent to fzf's per-character loop: a gap of g characters costs GAP_START + (g-1)*GAP_EXT and
    resets the consecutive/first-bonus state; character classes come from the original (cased) line.
    """
    find = fold_line.find
    get_cls = _char_class_cache.get
    table = _BONUS_TABLE
    score = consecutive = first_bonus = 0
    last = sidx - 1
    pos = sidx
    for pidx, ch in enumerate(term):
        idx = find(ch, pos, eidx)
        if idx < 0:
            break
        gap = idx - last - 1
        if gap > 0:
            score += _SCORE_GAP_START + (gap - 1) * _SCORE_GAP_EXT
            consecutive = 0
            first_bonus = 0
        if idx > 0:
            pc = orig_line[idx - 1]
            prev = get_cls(pc)
            if prev is None:
                prev = _char_class(pc)
        else:
            prev = _C_DELIM
        c = orig_line[idx]
        cls = get_cls(c)
        if cls is None:
            cls = _char_class(c)
        bonus = table[prev][cls]
        if consecutive == 0:
            first_bonus = bonus
        else:
            if bonus >= _BONUS_BOUNDARY and bonus > first_bonus:
                first_bonus = bonus
            if first_bonus > bonus:
                bonus = first_bonus
            if bonus < _BONUS_CONSECUTIVE:
                bonus = _BONUS_CONSECUTIVE
        score += _SCORE_MATCH + (bonus * _BONUS_FIRST_CHAR_MULT if pidx == 0 else bonus)
        consecutive += 1
        last = idx
        pos = idx + 1
    return score


def _fuzzy_window(fold_line: str, term: str, start: int) -> tuple[int, int] | None:
    """Greedy forward subsequence match, then backward shrink to the shortest window (fzf v1)."""
    idx = start
    find = fold_line.find
    for ch in term:
        idx = find(ch, idx)
        if idx < 0:
            return None
        idx += 1
    eidx = idx
    j = eidx
    rfind = fold_line.rfind
    for ch in reversed(term):
        j = rfind(ch, start, j)
    return j, eidx


_WORD_DELIMS = "/ -_."


def _term_score(orig_line: str, fold_line: str, term: str, bstart: int) -> int | None:
    n = len(term)
    find = fold_line.find
    first = find(term)
    if first >= 0:
        # Contiguous: score the first, the last (usually in the filename) and boundary occurrences.
        starts = {first, fold_line.rfind(term)}
        i = find(term, first + 1)
        probes = 0
        while i >= 0 and probes < 8:
            if fold_line[i - 1] in _WORD_DELIMS:
                starts.add(i)
            probes += 1
            i = find(term, i + 1)
        return max(
            _calc_score(orig_line, fold_line, term, s, s + n) + (_BONUS_BASENAME if s >= bstart else 0)
            for s in starts
        )
    windows = []
    w = _fuzzy_window(fold_line, term, 0)
    if w is None:
        return None
    windows.append(w)
    if bstart > w[0]:
        wb = _fuzzy_window(fold_line, term, bstart)
        if wb is not None:
            windows.append(wb)
    return max(
        _calc_score(orig_line, fold_line, term, s, e) + (_BONUS_BASENAME if s >= bstart else 0)
        for s, e in windows
    )


def _line_score(orig_line: str, fold_line: str, terms: list[str]) -> int | None:
    bstart = fold_line.rfind("/") + 1
    name = fold_line[bstart:]
    dot = name.rfind(".")
    stem = name[:dot] if dot > 0 else name
    total = 0
    for term in terms:
        sc = _term_score(orig_line, fold_line, term, bstart)
        if sc is None:
            return None
        if term == name or term == stem:
            sc += _BONUS_EXACT_NAME
        total += sc
    return total


def _estimate_count(text: str, needle: str, samples: int = 16, span: int = 131_072) -> int:
    """Occurrence estimate from evenly spread slices (~2 MB scanned instead of the whole cache)."""
    n = len(text)
    if n <= samples * span:
        return text.count(needle)
    step = n // samples
    return sum(text.count(needle, i * step, i * step + span) for i in range(samples))


def _verify_spans(fold: str, spans: list[tuple[int, int, int]], rx: re.Pattern[str]) -> list[tuple[int, int, int]]:
    """Keep spans whose folded line satisfies rx: one backtracking-free `re` pass over a joined blob."""
    line_at: dict[int, int] = {}
    off = 0
    for idx, (_, ls, le) in enumerate(spans):
        line_at[off] = idx
        off += le - ls + 1
    blob = "\n".join([fold[ls:le] for _, ls, le in spans])
    return [spans[line_at[m.start()]] for m in rx.finditer(blob)]


def rank_paths(caches: NameCaches, terms: list[str], want: int, accept=None) -> list[str]:
    """Return relative paths ranked best-first; every term must fuzzy-match (AND, order-independent).

    Candidates are gathered tier by tier, best tiers first: filename starts with a term, filename
    contains a term, full path contains the most selective term, then (only if still scarce) fuzzy
    filename / fuzzy path matches. Each tier is verified against the remaining terms in bulk; when a
    tier overflows the candidate budget, the shortest filenames are kept.
    """
    orig, fold, names = caches.orig, caches.fold, caches.names
    search_terms = list(dict.fromkeys(lossy_ascii(t) for t in terms))
    if len(search_terms) > 1:
        search_terms.sort(key=lambda t: (_estimate_count(fold, t), -len(t)))
    primary = search_terms[0]

    verifiers: dict[str, re.Pattern[str] | None] = {}

    def verifier(gen_term: str) -> re.Pattern[str] | None:
        if gen_term not in verifiers:
            rest = [t for t in search_terms if t != gen_term]
            verifiers[gen_term] = (
                re.compile("^" + "".join("(?=" + _fuzzy_regex(t, r"\n") + ")" for t in rest), re.M)
                if rest
                else None
            )
        return verifiers[gen_term]

    seen: set[int] = set()
    cands: list[tuple[int, int]] = []

    def gather(hits, gen_term: str, share: int = MAX_NAME_CANDIDATES) -> bool:
        """Add one tier's verified hits (at most `share`); returns True once the budget is full."""
        room = min(share, MAX_NAME_CANDIDATES - len(cands))
        if room <= 0:
            return len(cands) >= MAX_NAME_CANDIDATES
        limit = min(MAX_TIER_GATHER, max(4 * room, 1000))
        spans: list[tuple[int, int, int]] = []
        find = fold.find
        for ls, blen in hits:
            if ls in seen:
                continue
            seen.add(ls)
            le = find("\n", ls)
            if le < 0:
                le = len(fold)
            if le > ls:
                spans.append((blen, ls, le))
            if len(spans) >= limit:
                break
        if not spans:
            return False
        rx = verifier(gen_term)
        if rx is not None:
            spans = _verify_spans(fold, spans, rx)
        if accept is not None:
            spans = [sp for sp in spans if accept(orig[sp[1] : sp[2]])]
        if len(spans) > room:
            spans = heapq.nsmallest(room, spans)
        cands.extend((ls, le) for _, ls, le in spans)
        return len(cands) >= MAX_NAME_CANDIDATES

    name_terms = [t for t in search_terms if "/" not in t]
    # With several terms, each one's filename tiers get a fair share of the budget so that a path whose
    # filename holds a less selective term (e.g. "Factures/dropbox.pdf") still competes.
    share = MAX_NAME_CANDIDATES // len(name_terms) if len(name_terms) > 1 else MAX_NAME_CANDIDATES
    tiers = [(_iter_name_prefix_hits, names, t, share) for t in name_terms]
    tiers += [(_iter_name_substring_hits, names, t, share) for t in name_terms]
    tiers.append((_iter_path_substring_hits, fold, primary, MAX_NAME_CANDIDATES))
    full = False
    for make, text, t, tier_share in tiers:
        if gather(make(text, t), t, tier_share):
            full = True
            break
    # Fuzzy tiers only when contiguous matches are scarce (they would rank below them anyway).
    if not full and "/" not in primary and len(cands) < max(want * 2, 100):
        full = gather(_iter_name_fuzzy_hits(names, primary), primary)
    if not full and len(cands) < want:
        gather(_iter_path_fuzzy_hits(fold, primary), primary)

    scored: list[tuple[int, int, int, str]] = []
    for ls, le in cands:
        ol = orig[ls:le]
        # Exact (non-lossy) fold of this one line: rejects \x7f placeholder false positives.
        fl = fold_text(ol)
        sc = _line_score(ol, fl, terms)
        if sc is None:
            continue
        scored.append((-sc, le - ls - fl.rfind("/") - 1, le - ls, ol))
    scored.sort()
    return [row[3] for row in scored]


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


def _kind_from_suffix_ok(rel: str, filter_kind: str) -> bool:
    """Cheap pre-scoring extension filter (no stat); 'dir' is resolved later with a real stat."""
    if filter_kind in ("all", "dir"):
        return True
    return matches_filter_kind(Path(rel), False, filter_kind)


def search_names(query: str, filter_kind: str, limit: int) -> list[dict]:
    """In-process fuzzy path search: the query never leaves this process (no fzf argv)."""
    terms = query_terms(query)
    if not terms:
        return []
    caches = load_name_caches()
    if caches is None:
        return []

    want = max(limit * 3, 120) if filter_kind == "dir" else limit
    ranked = rank_paths(caches, terms, want=want, accept=lambda rel: _kind_from_suffix_ok(rel, filter_kind))

    results: list[dict] = []
    for rel in ranked:
        p = HOME / rel
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
    try:
        cached_count = fts_stats(con)
        if cached_count < 20:
            try:
                opportunistic_index_recent(con, cfg, window="365d", budget_sec=0.28)
                cached_count = fts_stats(con)
            except sqlite3.OperationalError:
                pass

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
                            return results, cached_count
            except sqlite3.OperationalError:
                pass
    finally:
        con.close()

    # 2. Live ripgrep across plain-text / Markdown / CSV / code files (pattern passed via stdin -f -)
    if len(results) < limit and filter_kind in ("all", "doc", "xls") and query.strip():
        rg_cmd = [
            "rg",
            "--fixed-strings",
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
        rg_cmd.extend(["-f", "-", "--", "."])
        try:
            proc = subprocess.run(
                rg_cmd,
                cwd=str(HOME),
                input=(query + "\n").encode("utf-8", errors="replace"),
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
            rel = line[:sep].removeprefix("./")
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
    try:
        known = {
            row[0]: (row[1], row[2], row[3])
            for row in con.execute("SELECT path, mtime_ns, size, fts_rowid FROM docs_meta")
        }

        cmd = fd_base_cmd(cfg) + ["-t", "f"]
        for ext in sorted(DOC_EXTS):
            cmd.extend(["-e", ext.lstrip(".")])
        cmd.append(".")
        proc = subprocess.run(
            cmd,
            cwd=str(HOME),
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            encoding="utf-8",
            errors="replace",
            timeout=30.0,
            check=False,
        )

        all_docs: list[Path] = []
        todo: list[tuple[int, Path, int]] = []
        present_paths: set[str] = set()

        for rel in (proc.stdout or "").splitlines():
            if not rel or "\ufffd" in rel:
                continue
            p = HOME / rel
            if p.name.startswith("._") or "__MACOSX" in p.parts:
                continue
            try:
                st = p.stat()
            except OSError:
                continue
            if not stat.S_ISREG(st.st_mode):
                continue
            sp = str(p)
            present_paths.add(sp)
            all_docs.append(p)
            prev = known.get(sp)
            if prev and prev[0] == st.st_mtime_ns and prev[1] == st.st_size and prev[2] is not None:
                continue
            todo.append((st.st_mtime_ns, p, st.st_size))

        stale = [(sp, known[sp][2]) for sp in known if sp not in present_paths]
        if stale:
            with con:
                for sp, fts_rowid in stale:
                    if fts_rowid is not None:
                        con.execute("DELETE FROM docs_fts WHERE rowid = ?", (fts_rowid,))
                    else:
                        con.execute("DELETE FROM docs_fts WHERE path = ?", (sp,))
                    con.execute("DELETE FROM docs_meta WHERE path = ?", (sp,))

        todo.sort(key=lambda x: x[0], reverse=True)
        total_all = len(all_docs)
        already_cached = total_all - len(todo)
        print(json.dumps({"done": already_cached, "total": total_all, "finished": len(todo) == 0}), flush=True)

        if not todo:
            return 0

        max_bytes = int(cfg.get("maxIndexedTextBytes", 350_000))
        workers = max(2, min(4, (os.cpu_count() or 4) // 2))

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
                if len(batch) >= 60 or done == total_all:
                    _upsert_fts_batch(con, batch)
                    batch.clear()
                    print(json.dumps({"done": done, "total": total_all, "finished": done >= total_all}), flush=True)
    finally:
        con.close()
    return 0


# ---------------- Preview & Actions ----------------


def render_pdf_page1(path: Path, mtime_ns: int) -> str:
    ensure_dirs()
    key = hashlib.sha256(f"{path}:{mtime_ns}".encode("utf-8")).hexdigest()[:24]
    out_png = PREVIEW_DIR / f"{key}.png"
    if out_png.is_file() and not out_png.is_symlink():
        return str(out_png)
    tmp_prefix = PREVIEW_DIR / f".tmp.{key}.{os.getpid()}"
    tmp_png = PREVIEW_DIR / f".tmp.{key}.{os.getpid()}.png"
    src = open_pdf_for_child_stdin(path)
    if src is None:
        return ""
    try:
        # The PDF arrives as /dev/stdin; argv only carries the opaque hashed output prefix.
        with src:
            subprocess.run(
                ["pdftoppm", "-png", "-scale-to", "1000", "-f", "1", "-l", "1", "-singlefile", "/dev/stdin", str(tmp_prefix)],
                stdin=src,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=3.0,
                check=False,
            )
        if tmp_png.is_file() and not tmp_png.is_symlink():
            os.replace(tmp_png, out_png)
            return str(out_png)
    except Exception:
        pass
    finally:
        try:
            if tmp_png.exists() or tmp_png.is_symlink():
                tmp_png.unlink()
        except OSError:
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

    if stat.S_ISDIR(st.st_mode):
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

    if not stat.S_ISREG(st.st_mode):
        return {"kind": "info", "imagePath": "", "text": str(p), "meta": meta, "inDropbox": in_dbx}

    if ext in SAFE_PREVIEW_IMAGE_EXTS and st.st_size <= MAX_PREVIEW_IMAGE_BYTES:
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
                try:
                    body = _lookup_cached_body(con, str(p))
                finally:
                    con.close()
                if not body:
                    body = extract_pdf(p, 80_000, timeout_sec=1.5)
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
            try:
                body = _lookup_cached_body(con, str(p))
            finally:
                con.close()
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
            with open(p, "rb") as fh:
                raw = fh.read(48_000)
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
    abs_target = os.path.abspath(target)
    cmd = ["uwsm-app", "--", "xdg-open", abs_target] if shutil.which("uwsm-app") else ["xdg-open", abs_target]
    subprocess.Popen(
        cmd,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        umask=_ORIG_UMASK,
    )


def _sanitize_notification_text(text: str, max_len: int = 120) -> str:
    clean = re.sub(r"[<>&\r\n\x00]", " ", str(text or "")).strip()
    clean = " ".join(clean.split())[:max_len]
    if clean.startswith("-"):
        clean = " " + clean
    return clean


def notify(headline: str, description: str = "", glyph: str = "󰈔") -> None:
    if shutil.which("omarchy-notification-send"):
        safe_head = _sanitize_notification_text(headline, 80)
        safe_desc = _sanitize_notification_text(description, 140)
        subprocess.run(
            ["omarchy-notification-send", "-g", glyph, safe_head, safe_desc],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            check=False,
        )


def copy_to_clipboard(text: str) -> bool:
    """Pass clipboard payload over stdin so secret links/paths never appear in argv."""
    try:
        res = subprocess.run(
            ["wl-copy"],
            input=text,
            text=True,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=3.0,
            check=False,
        )
        return res.returncode == 0
    except Exception:
        return False


def run_action(action: str, path_str: str) -> int:
    p = Path(path_str)
    if not p.exists():
        return 1
    abs_p = os.path.abspath(p)

    if action == "open":
        launch_detached_xdg_open(p)
        return 0

    if action == "reveal":
        if p.is_dir():
            launch_detached_xdg_open(p)
            return 0
        uri = "file://" + urllib.parse.quote(abs_p)
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
            ["dropbox-cli", "sharelink", abs_p],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=6.0,
            check=False,
        )
        first_line = proc.stdout.splitlines()[0].strip() if proc.stdout else ""
        if proc.returncode == 0 and first_line.startswith("https://") and len(first_line) <= 2048:
            if copy_to_clipboard(first_line):
                notify("Lien Dropbox copié" if IS_FR else "Dropbox link copied", p.name, "󰇣")
                return 0
            notify(
                "Erreur presse-papiers" if IS_FR else "Clipboard error",
                "wl-copy indisponible" if IS_FR else "wl-copy failed",
                "󰇣",
            )
            return 1
        fallback_err = "Impossible de générer le lien" if IS_FR else "Unable to generate share link"
        err = proc.stderr.strip() or fallback_err
        notify("Erreur Dropbox" if IS_FR else "Dropbox error", err[:120], "󰇣")
        return 1

    if action == "copy-path":
        if copy_to_clipboard(abs_p):
            notify("Chemin copié" if IS_FR else "Path copied", p.name, "󰆏")
            return 0
        notify(
            "Erreur presse-papiers" if IS_FR else "Clipboard error",
            "wl-copy indisponible" if IS_FR else "wl-copy failed",
            "󰆏",
        )
        return 1

    if action == "trash":
        res = subprocess.run(["gio", "trash", "--", abs_p], check=False)
        if res.returncode == 0:
            notify("Déplacé dans la corbeille" if IS_FR else "Moved to trash", p.name, "󰩺")
        return res.returncode

    return 1


def main() -> int:
    _install_signal_handlers()
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("refresh-paths")
    sub.add_parser("index")

    pq = sub.add_parser("query")
    pq.add_argument("--mode", choices=("name", "content"), default="name")
    pq.add_argument("--limit", type=int, default=50)
    pq.add_argument("--text", default=None)
    pq.add_argument("query", nargs="?", default=None)

    pp = sub.add_parser("preview")
    pp.add_argument("--query", default=None)
    pp.add_argument("path", nargs="?", default=None)

    pa = sub.add_parser("action")
    pa.add_argument("kind", choices=("open", "reveal", "dropbox-link", "copy-path", "trash"))
    pa.add_argument("path", nargs="?", default=None)

    args = parser.parse_args()

    if args.cmd == "refresh-paths":
        count = refresh_paths_cache()
        con = open_fts()
        try:
            cached = fts_stats(con)
        finally:
            con.close()
        print(json.dumps({"paths": count, "cachedDocs": cached}))
        return 0

    if args.cmd == "index":
        return run_index()

    if args.cmd == "query":
        raw_q = (
            args.text
            if args.text is not None
            else (args.query if args.query is not None else os.environ.get("SPOTLIGHT_QUERY", ""))
        )
        eff_mode, filter_kind, clean_q = parse_prefix(raw_q, args.mode)
        if not clean_q:
            con = open_fts()
            try:
                cached = fts_stats(con)
            finally:
                con.close()
            items = [
                it for it in load_recent(args.limit)
                if matches_filter_kind(Path(it["path"]), it["isDir"], filter_kind)
            ]
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
        target_path = args.path if args.path is not None else os.environ.get("SPOTLIGHT_PATH", "")
        raw_q = args.query if args.query is not None else os.environ.get("SPOTLIGHT_QUERY", "")
        _, _, clean_q = parse_prefix(raw_q, "name")
        print(json.dumps(build_preview(target_path, clean_q), ensure_ascii=False))
        return 0

    if args.cmd == "action":
        target_path = args.path if args.path is not None else os.environ.get("SPOTLIGHT_PATH", "")
        if not target_path:
            return 1
        return run_action(args.kind, target_path)

    return 1


if __name__ == "__main__":
    sys.exit(main())

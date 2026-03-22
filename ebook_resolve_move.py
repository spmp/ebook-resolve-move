#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import unicodedata
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
import xml.etree.ElementTree as ET


def _reexec_into_local_venv() -> None:
    if os.environ.get("EBOOK_SKIP_VENV_REEXEC") == "1":
        return

    if os.environ.get("EBOOK_VENV_REEXEC_DONE") == "1":
        return

    if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
        return

    def _candidate_from_env() -> List[Path]:
        out: List[Path] = []

        env_python = os.environ.get("EBOOK_PYTHON")
        if env_python:
            out.append(Path(env_python))

        env_venv = os.environ.get("EBOOK_VENV")
        if env_venv:
            out.append(Path(env_venv) / "bin" / "python")

        return out

    script_path = Path(__file__).resolve()
    script_dir = script_path.parent
    cwd = Path.cwd().resolve()

    candidates: List[Path] = []
    candidates.extend(_candidate_from_env())
    candidates.append(script_dir / ".venv" / "bin" / "python")
    candidates.append(cwd / ".venv" / "bin" / "python")

    for parent in script_dir.parents:
        candidates.append(parent / ".venv" / "bin" / "python")

    current_python_raw = Path(sys.executable).absolute()

    seen = set()
    for candidate in candidates:
        key = str(candidate)
        if key in seen:
            continue
        seen.add(key)

        if not candidate.exists() or not os.access(candidate, os.X_OK):
            continue

        target_python = candidate.absolute()
        if current_python_raw == target_python:
            return

        env = dict(os.environ)
        env["EBOOK_VENV_REEXEC_DONE"] = "1"
        os.execve(
            str(target_python),
            [str(target_python), str(script_path), *sys.argv[1:]],
            env,
        )


_reexec_into_local_venv()

import httpx
from pypdf import PdfReader, PdfWriter
from rapidfuzz import fuzz
from watchdog.events import FileSystemEventHandler
from watchdog.observers import Observer

DEFAULT_API_BASE = "https://hardcover.bookinfo.pro"
DEFAULT_READARR_COMMAND = '{"name":"RescanFolders"}'

EPUB_NS = {
    "container": "urn:oasis:names:tc:opendocument:xmlns:container",
    "opf": "http://www.idpf.org/2007/opf",
    "dc": "http://purl.org/dc/elements/1.1/",
}

DOCX_NS = {
    "cp": "http://schemas.openxmlformats.org/package/2006/metadata/core-properties",
    "dc": "http://purl.org/dc/elements/1.1/",
    "dcterms": "http://purl.org/dc/terms/",
}

ODF_NS = {
    "office": "urn:oasis:names:tc:opendocument:xmlns:office:1.0",
    "meta": "urn:oasis:names:tc:opendocument:xmlns:meta:1.0",
    "dc": "http://purl.org/dc/elements/1.1/",
}

TEMP_SUFFIXES = (
    ".part",
    ".tmp",
    ".crdownload",
    ".swp",
    ".partial",
    ".download",
)


# --------------------------------------------------
# Utility helpers
# --------------------------------------------------
def log(msg: str) -> None:
    print(msg, flush=True)


def norm_text(value: Optional[str]) -> str:
    text = unicodedata.normalize("NFKD", value or "")
    text = text.encode("ascii", "ignore").decode("ascii")
    text = text.lower()
    text = re.sub(r"[^\w\s]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def safe_fs(value: str) -> str:
    text = (value or "").replace("\r", " ").replace("\n", " ").strip()
    text = re.sub(r'[\/\\:*?"<>|]', "-", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def unique_path(path: Path) -> Path:
    if not path.exists():
        return path
    i = 2
    while True:
        candidate = path.with_name(f"{path.stem} ({i}){path.suffix}")
        if not candidate.exists():
            return candidate
        i += 1


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def env_str(name: str, default: Optional[str] = None) -> Optional[str]:
    value = os.getenv(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def env_bool(name: str, default: bool) -> bool:
    value = env_str(name)
    if value is None:
        return default
    lowered = value.lower()
    if lowered in {"1", "true", "yes", "on"}:
        return True
    if lowered in {"0", "false", "no", "off"}:
        return False
    return default


def env_float(name: str, default: float) -> float:
    value = env_str(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError:
        return default

def title_score(a: Optional[str], b: Optional[str]) -> float:
    """
    Title scoring should strongly prefer exact matches and penalize
    'contains the title plus extra words' cases.
    """
    if not a or not b:
        return 0.0

    na = norm_text(a)
    nb = norm_text(b)

    if not na or not nb:
        return 0.0

    if na == nb:
        return 1.0

    ratio = fuzz.ratio(na, nb) / 100.0
    token_sort = fuzz.token_sort_ratio(na, nb) / 100.0

    words_a = na.split()
    words_b = nb.split()
    set_a = set(words_a)
    set_b = set(words_b)

    overlap = len(set_a & set_b) / max(len(set_a | set_b), 1)

    # Penalize subset/containment cases where one title has lots of extra words.
    length_penalty = min(len(words_a), len(words_b)) / max(len(words_a), len(words_b))

    # Weighted toward exact-ish phrase equality, not bag-of-words containment.
    return 0.50 * ratio + 0.20 * token_sort + 0.20 * overlap + 0.10 * length_penalty


def author_score(a: Optional[str], b: Optional[str]) -> float:
    """
    Author names benefit from being a bit fuzzier than titles,
    especially around punctuation and initials.
    """
    if not a or not b:
        return 0.0

    na = norm_text(a)
    nb = norm_text(b)

    if not na or not nb:
        return 0.0

    if na == nb:
        return 1.0

    ratio = fuzz.ratio(na, nb) / 100.0
    token_sort = fuzz.token_sort_ratio(na, nb) / 100.0
    token_set = fuzz.token_set_ratio(na, nb) / 100.0

    return 0.4 * ratio + 0.3 * token_sort + 0.3 * token_set

def is_temp_or_hidden(path: Path) -> bool:
    name = path.name
    if name.startswith("."):
        return True
    lower = name.lower()
    return lower.endswith(TEMP_SUFFIXES)


def file_state(path: Path) -> Optional[Tuple[int, int]]:
    try:
        stat = path.stat()
        return (stat.st_size, int(stat.st_mtime_ns))
    except FileNotFoundError:
        return None


def wait_for_stable_file(
    path: Path,
    settle_seconds: float,
    checks_required: int = 2,
    max_wait_seconds: float = 300.0,
) -> bool:
    start = time.time()
    stable_count = 0
    last_state: Optional[Tuple[int, int]] = None

    while True:
        state = file_state(path)
        if state is None:
            return False

        if state == last_state:
            stable_count += 1
            if stable_count >= checks_required:
                return True
        else:
            stable_count = 0
            last_state = state

        if time.time() - start > max_wait_seconds:
            return False

        time.sleep(settle_seconds)


# --------------------------------------------------
# Metadata models
# --------------------------------------------------
@dataclass
class EmbeddedMetadata:
    title: Optional[str] = None
    author: Optional[str] = None
    isbn: Optional[str] = None
    publisher: Optional[str] = None
    language: Optional[str] = None
    description: Optional[str] = None
    published_date: Optional[str] = None
    subjects: Optional[List[str]] = None
    source: str = "none"


@dataclass
class FilenameMetadata:
    title: Optional[str] = None
    author: Optional[str] = None


@dataclass
class WorkMetadata:
    work_id: int
    title: Optional[str] = None
    short_title: Optional[str] = None
    author: Optional[str] = None
    isbn13: Optional[str] = None
    publisher: Optional[str] = None
    language: Optional[str] = None
    full_title: Optional[str] = None
    description: Optional[str] = None
    published_date: Optional[str] = None
    subjects: Optional[List[str]] = None


@dataclass
class Candidate:
    work: WorkMetadata
    score_title: float
    score_author: float
    score_total: float
    title_match: bool = False
    author_match: bool = False


@dataclass
class AppConfig:
    api_base: str
    dry_run: bool
    min_score: float
    min_margin: float
    kavita_scan: bool
    kavita_url: str
    kavita_api_key: Optional[str]
    kavita_library_id: Optional[str]
    readarr_scan: bool
    readarr_url: str
    readarr_api_key: Optional[str]
    readarr_command_json: str
    settle_seconds: float


# --------------------------------------------------
# Native embedded metadata readers
# --------------------------------------------------
def read_epub_metadata(path: Path) -> EmbeddedMetadata:
    with zipfile.ZipFile(path, "r") as zf:
        container_xml = zf.read("META-INF/container.xml")
        container_root = ET.fromstring(container_xml)
        rootfile = container_root.find(".//container:rootfile", EPUB_NS)
        if rootfile is None:
            return EmbeddedMetadata(source="epub")
        opf_path = rootfile.attrib.get("full-path")
        if not opf_path:
            return EmbeddedMetadata(source="epub")

        opf_xml = zf.read(opf_path)
        opf_root = ET.fromstring(opf_xml)

    def text(xpath: str) -> Optional[str]:
        node = opf_root.find(xpath, EPUB_NS)
        if node is None or node.text is None:
            return None
        value = node.text.strip()
        return value or None

    isbn = None
    for ident in opf_root.findall(".//opf:metadata/dc:identifier", EPUB_NS):
        raw = (ident.text or "").strip()
        scheme = (
            ident.attrib.get("{http://www.idpf.org/2007/opf}scheme")
            or ident.attrib.get("scheme")
            or ""
        )
        cleaned = re.sub(r"[-\s]", "", raw)
        if "isbn" in scheme.lower() or re.fullmatch(r"(97[89])?\d{9}[\dXx]", cleaned):
            isbn = raw
            break

    subjects: List[str] = []
    for subject_node in opf_root.findall(".//opf:metadata/dc:subject", EPUB_NS):
        value = (subject_node.text or "").strip()
        if value:
            subjects.append(value)

    return EmbeddedMetadata(
        title=text(".//opf:metadata/dc:title"),
        author=text(".//opf:metadata/dc:creator"),
        isbn=isbn,
        publisher=text(".//opf:metadata/dc:publisher"),
        language=text(".//opf:metadata/dc:language"),
        description=text(".//opf:metadata/dc:description"),
        published_date=text(".//opf:metadata/dc:date"),
        subjects=subjects or None,
        source="epub",
    )


def read_opf_metadata_xml(opf_xml: bytes, source: str) -> EmbeddedMetadata:
    opf_root = ET.fromstring(opf_xml)

    def text(xpath: str) -> Optional[str]:
        node = opf_root.find(xpath, EPUB_NS)
        if node is None or node.text is None:
            return None
        value = node.text.strip()
        return value or None

    isbn = None
    for ident in opf_root.findall(".//opf:metadata/dc:identifier", EPUB_NS):
        raw = (ident.text or "").strip()
        scheme = (
            ident.attrib.get("{http://www.idpf.org/2007/opf}scheme")
            or ident.attrib.get("scheme")
            or ""
        )
        cleaned = re.sub(r"[-\s]", "", raw)
        if "isbn" in scheme.lower() or re.fullmatch(r"(97[89])?\d{9}[\dXx]", cleaned):
            isbn = raw
            break

    subjects: List[str] = []
    for subject_node in opf_root.findall(".//opf:metadata/dc:subject", EPUB_NS):
        value = (subject_node.text or "").strip()
        if value:
            subjects.append(value)

    return EmbeddedMetadata(
        title=text(".//opf:metadata/dc:title"),
        author=text(".//opf:metadata/dc:creator"),
        isbn=isbn,
        publisher=text(".//opf:metadata/dc:publisher"),
        language=text(".//opf:metadata/dc:language"),
        description=text(".//opf:metadata/dc:description"),
        published_date=text(".//opf:metadata/dc:date"),
        subjects=subjects or None,
        source=source,
    )


def extract_zip_opf_entry(path: Path) -> Optional[Tuple[str, bytes]]:
    with zipfile.ZipFile(path, "r") as zf:
        if "META-INF/container.xml" in zf.namelist():
            container_xml = zf.read("META-INF/container.xml")
            container_root = ET.fromstring(container_xml)
            rootfile = container_root.find(".//container:rootfile", EPUB_NS)
            if rootfile is not None:
                opf_path = rootfile.attrib.get("full-path")
                if opf_path and opf_path in zf.namelist():
                    return opf_path, zf.read(opf_path)

        for candidate in ("metadata.opf", "content.opf", "OEBPS/content.opf"):
            if candidate in zf.namelist():
                return candidate, zf.read(candidate)

        for name in zf.namelist():
            if name.lower().endswith(".opf"):
                return name, zf.read(name)

    return None


def read_zip_opf_metadata(path: Path, source: str) -> EmbeddedMetadata:
    found = extract_zip_opf_entry(path)
    if not found:
        return EmbeddedMetadata(source=source)
    _, opf_xml = found
    return read_opf_metadata_xml(opf_xml, source=source)


def read_pdf_metadata(path: Path) -> EmbeddedMetadata:
    reader = PdfReader(str(path))
    meta = reader.metadata or {}

    def get(*keys: str) -> Optional[str]:
        for key in keys:
            value = meta.get(key)
            if value is not None:
                text = str(value).strip()
                if text:
                    return text
        return None

    return EmbeddedMetadata(
        title=get("/Title"),
        author=get("/Author"),
        publisher=get("/Producer"),
        language=None,
        isbn=None,
        description=get("/Subject"),
        subjects=[part.strip() for part in (get("/Keywords") or "").split(",") if part.strip()] or None,
        source="pdf",
    )


def read_docx_metadata(path: Path) -> EmbeddedMetadata:
    with zipfile.ZipFile(path, "r") as zf:
        try:
            core_xml = zf.read("docProps/core.xml")
        except KeyError:
            return EmbeddedMetadata(source="docx")

    root = ET.fromstring(core_xml)

    def text(xpath: str) -> Optional[str]:
        node = root.find(xpath, DOCX_NS)
        if node is None or node.text is None:
            return None
        value = node.text.strip()
        return value or None

    subjects = [part.strip() for part in (text("./cp:keywords") or "").split(",") if part.strip()]

    return EmbeddedMetadata(
        title=text("./dc:title"),
        author=text("./dc:creator"),
        description=text("./dc:description"),
        published_date=text("./dcterms:created"),
        subjects=subjects or None,
        source="docx",
    )


def read_odt_metadata(path: Path) -> EmbeddedMetadata:
    with zipfile.ZipFile(path, "r") as zf:
        try:
            meta_xml = zf.read("meta.xml")
        except KeyError:
            return EmbeddedMetadata(source="odt")

    root = ET.fromstring(meta_xml)
    meta_node = root.find("./office:meta", ODF_NS)
    if meta_node is None:
        return EmbeddedMetadata(source="odt")

    def text(xpath: str) -> Optional[str]:
        node = meta_node.find(xpath, ODF_NS)
        if node is None or node.text is None:
            return None
        value = node.text.strip()
        return value or None

    keywords_raw = text("./meta:keyword")
    subjects = [part.strip() for part in (keywords_raw or "").split(",") if part.strip()]

    return EmbeddedMetadata(
        title=text("./dc:title"),
        author=text("./dc:creator"),
        description=text("./dc:description"),
        published_date=text("./dc:date"),
        language=text("./dc:language"),
        subjects=subjects or None,
        source="odt",
    )


def read_fb2_xml(xml_bytes: bytes, source: str) -> EmbeddedMetadata:
    root = ET.fromstring(xml_bytes)

    def find_text(paths: List[str]) -> Optional[str]:
        for path in paths:
            node = root.find(path)
            if node is not None and node.text:
                value = node.text.strip()
                if value:
                    return value
        return None

    title = find_text([
        ".//{*}description/{*}title-info/{*}book-title",
    ])

    author_node = root.find(".//{*}description/{*}title-info/{*}author")
    author = None
    if author_node is not None:
        first = (author_node.findtext("{*}first-name") or "").strip()
        middle = (author_node.findtext("{*}middle-name") or "").strip()
        last = (author_node.findtext("{*}last-name") or "").strip()
        parts = [part for part in [first, middle, last] if part]
        author = " ".join(parts) or None

    publisher = find_text([
        ".//{*}description/{*}publish-info/{*}publisher",
    ])
    language = find_text([
        ".//{*}description/{*}title-info/{*}lang",
    ])
    published_date = find_text([
        ".//{*}description/{*}publish-info/{*}year",
        ".//{*}description/{*}title-info/{*}date",
    ])
    description = find_text([
        ".//{*}description/{*}title-info/{*}annotation",
    ])

    subjects = []
    for node in root.findall(".//{*}description/{*}title-info/{*}genre"):
        value = (node.text or "").strip()
        if value:
            subjects.append(value)
    for node in root.findall(".//{*}description/{*}title-info/{*}keywords"):
        for part in (node.text or "").split(","):
            value = part.strip()
            if value:
                subjects.append(value)

    return EmbeddedMetadata(
        title=title,
        author=author,
        publisher=publisher,
        language=language,
        description=description,
        published_date=published_date,
        subjects=list(dict.fromkeys(subjects)) or None,
        source=source,
    )


def read_fb2_metadata(path: Path) -> EmbeddedMetadata:
    return read_fb2_xml(path.read_bytes(), source="fb2")


def read_fbz_metadata(path: Path) -> EmbeddedMetadata:
    with zipfile.ZipFile(path, "r") as zf:
        fb2_entries = [name for name in zf.namelist() if name.lower().endswith(".fb2")]
        if not fb2_entries:
            return EmbeddedMetadata(source="fbz")
        return read_fb2_xml(zf.read(fb2_entries[0]), source="fbz")


def read_rtf_metadata(path: Path) -> EmbeddedMetadata:
    text = path.read_text(errors="ignore")
    info_match = re.search(r"\{\\info(?P<body>.*?)\}", text, flags=re.DOTALL)
    if not info_match:
        return EmbeddedMetadata(source="rtf")

    body = info_match.group("body")

    def field(name: str) -> Optional[str]:
        m = re.search(rf"\\{name}\s+([^\\\{{\}}]+)", body)
        if not m:
            return None
        value = m.group(1).strip()
        return value or None

    subjects = [part.strip() for part in (field("keywords") or "").split(",") if part.strip()]
    return EmbeddedMetadata(
        title=field("title"),
        author=field("author"),
        description=field("subject"),
        subjects=subjects or None,
        source="rtf",
    )


def read_mobi_family_metadata(path: Path, source: str) -> EmbeddedMetadata:
    raw = path.read_bytes()
    if len(raw) < 128:
        return EmbeddedMetadata(source=source)

    title = raw[0:32].split(b"\x00", 1)[0].decode("latin-1", errors="ignore").strip() or None

    records = struct.unpack(">H", raw[76:78])[0]
    if records < 1:
        return EmbeddedMetadata(title=title, source=source)

    rec0_offset = struct.unpack(">L", raw[78:82])[0]
    if rec0_offset + 24 > len(raw):
        return EmbeddedMetadata(title=title, source=source)

    rec1_offset = struct.unpack(">L", raw[86:90])[0] if records > 1 else len(raw)
    rec0 = raw[rec0_offset:rec1_offset]

    if len(rec0) < 24 or rec0[16:20] != b"MOBI":
        return EmbeddedMetadata(title=title, source=source)

    mobi_len = struct.unpack(">L", rec0[20:24])[0]
    if 16 + mobi_len > len(rec0):
        return EmbeddedMetadata(title=title, source=source)

    exth_flags_offset = 16 + 0x80
    if exth_flags_offset + 4 > len(rec0):
        return EmbeddedMetadata(title=title, source=source)

    exth_flags = struct.unpack(">L", rec0[exth_flags_offset:exth_flags_offset + 4])[0]
    if not (exth_flags & 0x40):
        return EmbeddedMetadata(title=title, source=source)

    exth_start = 16 + mobi_len
    if exth_start + 12 > len(rec0) or rec0[exth_start:exth_start + 4] != b"EXTH":
        return EmbeddedMetadata(title=title, source=source)

    exth_len = struct.unpack(">L", rec0[exth_start + 4:exth_start + 8])[0]
    exth_end = min(exth_start + exth_len, len(rec0))
    pos = exth_start + 12

    records_map: Dict[int, List[str]] = {}
    while pos + 8 <= exth_end:
        rec_type = struct.unpack(">L", rec0[pos:pos + 4])[0]
        rec_len = struct.unpack(">L", rec0[pos + 4:pos + 8])[0]
        if rec_len < 8 or pos + rec_len > exth_end:
            break
        data = rec0[pos + 8:pos + rec_len].decode("utf-8", errors="ignore").strip()
        if data:
            records_map.setdefault(rec_type, []).append(data)
        pos += rec_len

    return EmbeddedMetadata(
        title=title or (records_map.get(503, [None])[0]),
        author=(records_map.get(100, [None])[0]),
        publisher=(records_map.get(101, [None])[0]),
        description=(records_map.get(103, [None])[0]),
        isbn=(records_map.get(104, [None])[0]),
        subjects=records_map.get(105),
        source=source,
    )


def read_embedded_metadata(path: Path) -> EmbeddedMetadata:
    lower_name = path.name.lower()
    ext = path.suffix.lower()
    if ext == ".epub" or ext == ".kepub" or lower_name.endswith(".kepub.epub"):
        return read_epub_metadata(path)
    if ext == ".pdf":
        return read_pdf_metadata(path)
    if ext == ".docx":
        return read_docx_metadata(path)
    if ext == ".odt":
        return read_odt_metadata(path)
    if ext == ".fb2":
        return read_fb2_metadata(path)
    if ext == ".fbz":
        return read_fbz_metadata(path)
    if ext in {".htmlz", ".txtz"}:
        return read_zip_opf_metadata(path, source=ext.lstrip("."))
    if ext == ".rtf":
        return read_rtf_metadata(path)
    if ext in {".mobi", ".azw", ".azw1", ".azw3", ".prc"}:
        return read_mobi_family_metadata(path, source=ext.lstrip("."))
    return EmbeddedMetadata(source="unsupported")


# --------------------------------------------------
# Filename parsing
# --------------------------------------------------
def parse_filename_metadata(path: Path) -> FilenameMetadata:
    stem = path.stem
    stem = re.sub(r"[_\.]+", " ", stem)
    stem = re.sub(r"\s+", " ", stem).strip()

    m = re.match(r"^\s*(.+?)\s*[-–—]\s*(.+?)\s*$", stem)
    if m:
        author = m.group(1).strip()
        title = m.group(2).strip()
        return FilenameMetadata(title=title or None, author=author or None)

    return FilenameMetadata(title=stem or None, author=None)


# --------------------------------------------------
# Native metadata writers, non-destructive
# --------------------------------------------------
def write_epub_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool
) -> List[str]:
    planned: List[Tuple[str, str]] = []

    if not existing.title and resolved.title:
        planned.append(("title", resolved.title))
    if not existing.author and resolved.author:
        planned.append(("creator", resolved.author))
    if not existing.publisher and resolved.publisher:
        planned.append(("publisher", resolved.publisher))
    if not existing.language and resolved.language:
        planned.append(("language", resolved.language))
    if not existing.isbn and resolved.isbn13:
        planned.append(("identifier", resolved.isbn13))
    if not existing.description and resolved.description:
        planned.append(("description", resolved.description))
    if not existing.published_date and resolved.published_date:
        planned.append(("date", resolved.published_date))
    if not existing.subjects and resolved.subjects:
        for subject in resolved.subjects:
            planned.append(("subject", subject))

    if not planned:
        return []

    if dry_run:
        return [f"WOULD_WRITE_EPUB {k}={v}" for k, v in planned]

    with zipfile.ZipFile(path, "r") as zf:
        container_xml = zf.read("META-INF/container.xml")
        container_root = ET.fromstring(container_xml)
        rootfile = container_root.find(".//container:rootfile", EPUB_NS)
        if rootfile is None:
            return []
        opf_path = rootfile.attrib.get("full-path")
        if not opf_path:
            return []

        opf_xml = zf.read(opf_path)
        opf_root = ET.fromstring(opf_xml)
        metadata_el = opf_root.find(".//opf:metadata", EPUB_NS)
        if metadata_el is None:
            return []

        wrote: List[str] = []

        def append_dc(local_name: str, value: str, xpath: str) -> None:
            existing_node = metadata_el.find(xpath, EPUB_NS)
            if existing_node is not None and (existing_node.text or "").strip():
                return
            node = ET.Element(f"{{{EPUB_NS['dc']}}}{local_name}")
            node.text = value
            metadata_el.append(node)
            wrote.append(f"WROTE {local_name}={value}")

        if not existing.title and resolved.title:
            append_dc("title", resolved.title, "./dc:title")
        if not existing.author and resolved.author:
            append_dc("creator", resolved.author, "./dc:creator")
        if not existing.publisher and resolved.publisher:
            append_dc("publisher", resolved.publisher, "./dc:publisher")
        if not existing.language and resolved.language:
            append_dc("language", resolved.language, "./dc:language")
        if not existing.isbn and resolved.isbn13:
            found_identifier = False
            for ident in metadata_el.findall("./dc:identifier", EPUB_NS):
                if (ident.text or "").strip():
                    found_identifier = True
                    break
            if not found_identifier:
                node = ET.Element(f"{{{EPUB_NS['dc']}}}identifier")
                node.text = resolved.isbn13
                metadata_el.append(node)
                wrote.append(f"WROTE identifier={resolved.isbn13}")
        if not existing.description and resolved.description:
            append_dc("description", resolved.description, "./dc:description")
        if not existing.published_date and resolved.published_date:
            append_dc("date", resolved.published_date, "./dc:date")
        if not existing.subjects and resolved.subjects:
            has_subject = any((node.text or "").strip() for node in metadata_el.findall("./dc:subject", EPUB_NS))
            if not has_subject:
                for subject in resolved.subjects:
                    value = (subject or "").strip()
                    if not value:
                        continue
                    node = ET.Element(f"{{{EPUB_NS['dc']}}}subject")
                    node.text = value
                    metadata_el.append(node)
                    wrote.append(f"WROTE subject={value}")

        new_opf = ET.tostring(opf_root, encoding="utf-8", xml_declaration=True)

        tmp_fd, tmp_name = tempfile.mkstemp(suffix=path.suffix)
        Path(tmp_name).unlink(missing_ok=True)
        tmp_path = Path(tmp_name)

        try:
            with zipfile.ZipFile(tmp_path, "w") as zout:
                for item in zf.infolist():
                    data = zf.read(item.filename)
                    if item.filename == opf_path:
                        data = new_opf
                    zout.writestr(item, data)
            shutil.move(str(tmp_path), str(path))
        finally:
            tmp_path.unlink(missing_ok=True)

    return wrote


def write_pdf_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool
) -> List[str]:
    updates: Dict[str, str] = {}

    if not existing.title and resolved.title:
        updates["/Title"] = resolved.title
    if not existing.author and resolved.author:
        updates["/Author"] = resolved.author
    if not existing.description and resolved.description:
        updates["/Subject"] = resolved.description
    if not existing.subjects and resolved.subjects:
        joined_subjects = ", ".join(s.strip() for s in resolved.subjects if s and s.strip())
        if joined_subjects:
            updates["/Keywords"] = joined_subjects

    if not updates:
        return []

    if dry_run:
        return [f"WOULD_WRITE_PDF {k}={v}" for k, v in updates.items()]

    reader = PdfReader(str(path))
    writer = PdfWriter()

    for page in reader.pages:
        writer.add_page(page)

    new_meta: Dict[str, str] = {}
    for k, v in (reader.metadata or {}).items():
        if v is not None:
            new_meta[str(k)] = str(v)

    for k, v in updates.items():
        if not new_meta.get(k):
            new_meta[k] = v

    writer.add_metadata(new_meta)

    tmp_fd, tmp_name = tempfile.mkstemp(suffix=path.suffix)
    Path(tmp_name).unlink(missing_ok=True)
    tmp_path = Path(tmp_name)

    try:
        with open(tmp_path, "wb") as f:
            writer.write(f)
        shutil.move(str(tmp_path), str(path))
    finally:
        tmp_path.unlink(missing_ok=True)

    return [f"WROTE {k}={v}" for k, v in updates.items()]


def write_zip_entry(path: Path, entry_name: str, new_data: bytes) -> None:
    with zipfile.ZipFile(path, "r") as zin:
        tmp_fd, tmp_name = tempfile.mkstemp(suffix=path.suffix)
        Path(tmp_name).unlink(missing_ok=True)
        tmp_path = Path(tmp_name)
        try:
            with zipfile.ZipFile(tmp_path, "w") as zout:
                for item in zin.infolist():
                    data = zin.read(item.filename)
                    if item.filename == entry_name:
                        data = new_data
                    zout.writestr(item, data)
            shutil.move(str(tmp_path), str(path))
        finally:
            tmp_path.unlink(missing_ok=True)


def write_docx_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool
) -> List[str]:
    updates: Dict[str, str] = {}
    if not existing.title and resolved.title:
        updates["title"] = resolved.title
    if not existing.author and resolved.author:
        updates["creator"] = resolved.author
    if not existing.description and resolved.description:
        updates["description"] = resolved.description
    if not existing.published_date and resolved.published_date:
        updates["created"] = resolved.published_date
    if not existing.subjects and resolved.subjects:
        updates["keywords"] = ", ".join(s for s in resolved.subjects if s)

    if not updates:
        return []
    if dry_run:
        return [f"WOULD_WRITE_DOCX {k}={v}" for k, v in updates.items()]

    with zipfile.ZipFile(path, "r") as zf:
        if "docProps/core.xml" not in zf.namelist():
            return []
        core_xml = zf.read("docProps/core.xml")

    root = ET.fromstring(core_xml)

    def upsert(tag: str, ns: str, key: str) -> None:
        if key not in updates:
            return
        node = root.find(f"./{tag}", DOCX_NS)
        if node is None:
            node = ET.SubElement(root, f"{{{DOCX_NS[ns]}}}{tag.split(':', 1)[1]}")
        if not (node.text or "").strip():
            node.text = updates[key]

    upsert("dc:title", "dc", "title")
    upsert("dc:creator", "dc", "creator")
    upsert("dc:description", "dc", "description")
    upsert("cp:keywords", "cp", "keywords")
    upsert("dcterms:created", "dcterms", "created")

    new_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    write_zip_entry(path, "docProps/core.xml", new_xml)
    return [f"WROTE {k}={v}" for k, v in updates.items()]


def write_odt_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool
) -> List[str]:
    updates: Dict[str, str] = {}
    if not existing.title and resolved.title:
        updates["title"] = resolved.title
    if not existing.author and resolved.author:
        updates["creator"] = resolved.author
    if not existing.description and resolved.description:
        updates["description"] = resolved.description
    if not existing.language and resolved.language:
        updates["language"] = resolved.language
    if not existing.published_date and resolved.published_date:
        updates["date"] = resolved.published_date
    if not existing.subjects and resolved.subjects:
        updates["keyword"] = ", ".join(s for s in resolved.subjects if s)

    if not updates:
        return []
    if dry_run:
        return [f"WOULD_WRITE_ODT {k}={v}" for k, v in updates.items()]

    with zipfile.ZipFile(path, "r") as zf:
        if "meta.xml" not in zf.namelist():
            return []
        meta_xml = zf.read("meta.xml")

    root = ET.fromstring(meta_xml)
    meta_node = root.find("./office:meta", ODF_NS)
    if meta_node is None:
        return []

    def upsert(tag: str, ns: str, key: str) -> None:
        if key not in updates:
            return
        node = meta_node.find(f"./{tag}", ODF_NS)
        if node is None:
            node = ET.SubElement(meta_node, f"{{{ODF_NS[ns]}}}{tag.split(':', 1)[1]}")
        if not (node.text or "").strip():
            node.text = updates[key]

    upsert("dc:title", "dc", "title")
    upsert("dc:creator", "dc", "creator")
    upsert("dc:description", "dc", "description")
    upsert("dc:language", "dc", "language")
    upsert("dc:date", "dc", "date")
    upsert("meta:keyword", "meta", "keyword")

    new_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    write_zip_entry(path, "meta.xml", new_xml)
    return [f"WROTE {k}={v}" for k, v in updates.items()]


def update_fb2_tree_non_destructive(root: ET.Element, existing: EmbeddedMetadata, resolved: WorkMetadata) -> List[str]:
    wrote: List[str] = []

    title_info = root.find(".//{*}description/{*}title-info")
    if title_info is None:
        return wrote

    def has_text(path: str) -> bool:
        node = title_info.find(path)
        return node is not None and bool((node.text or "").strip())

    if not existing.title and resolved.title and not has_text("{*}book-title"):
        node = ET.SubElement(title_info, "book-title")
        node.text = resolved.title
        wrote.append(f"WROTE title={resolved.title}")

    if not existing.author and resolved.author and title_info.find("{*}author") is None:
        author_el = ET.SubElement(title_info, "author")
        first = resolved.author.split()[0]
        last = " ".join(resolved.author.split()[1:]) or first
        ET.SubElement(author_el, "first-name").text = first
        ET.SubElement(author_el, "last-name").text = last
        wrote.append(f"WROTE author={resolved.author}")

    if not existing.language and resolved.language and not has_text("{*}lang"):
        node = ET.SubElement(title_info, "lang")
        node.text = resolved.language
        wrote.append(f"WROTE language={resolved.language}")

    if not existing.description and resolved.description and title_info.find("{*}annotation") is None:
        node = ET.SubElement(title_info, "annotation")
        node.text = resolved.description
        wrote.append(f"WROTE description={resolved.description}")

    if not existing.subjects and resolved.subjects and not title_info.findall("{*}genre"):
        for subject in resolved.subjects:
            value = (subject or "").strip()
            if not value:
                continue
            node = ET.SubElement(title_info, "genre")
            node.text = value
            wrote.append(f"WROTE subject={value}")

    publish_info = root.find(".//{*}description/{*}publish-info")
    if publish_info is None:
        description = root.find(".//{*}description")
        if description is not None:
            publish_info = ET.SubElement(description, "publish-info")

    if publish_info is not None:
        if not existing.publisher and resolved.publisher:
            pub_node = publish_info.find("{*}publisher")
            if pub_node is None or not (pub_node.text or "").strip():
                if pub_node is None:
                    pub_node = ET.SubElement(publish_info, "publisher")
                pub_node.text = resolved.publisher
                wrote.append(f"WROTE publisher={resolved.publisher}")

        if not existing.published_date and resolved.published_date:
            year_node = publish_info.find("{*}year")
            if year_node is None or not (year_node.text or "").strip():
                if year_node is None:
                    year_node = ET.SubElement(publish_info, "year")
                year_node.text = resolved.published_date
                wrote.append(f"WROTE date={resolved.published_date}")

    return wrote


def write_fb2_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool
) -> List[str]:
    if dry_run:
        planned = []
        if not existing.title and resolved.title:
            planned.append(f"WOULD_WRITE_FB2 title={resolved.title}")
        if not existing.author and resolved.author:
            planned.append(f"WOULD_WRITE_FB2 author={resolved.author}")
        if not existing.publisher and resolved.publisher:
            planned.append(f"WOULD_WRITE_FB2 publisher={resolved.publisher}")
        if not existing.language and resolved.language:
            planned.append(f"WOULD_WRITE_FB2 language={resolved.language}")
        if not existing.description and resolved.description:
            planned.append(f"WOULD_WRITE_FB2 description={resolved.description}")
        if not existing.published_date and resolved.published_date:
            planned.append(f"WOULD_WRITE_FB2 date={resolved.published_date}")
        if not existing.subjects and resolved.subjects:
            planned.extend(f"WOULD_WRITE_FB2 subject={s}" for s in resolved.subjects)
        return planned

    root = ET.fromstring(path.read_bytes())
    wrote = update_fb2_tree_non_destructive(root, existing, resolved)
    if not wrote:
        return []

    path.write_bytes(ET.tostring(root, encoding="utf-8", xml_declaration=True))
    return wrote


def write_fbz_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool
) -> List[str]:
    with zipfile.ZipFile(path, "r") as zf:
        fb2_entries = [name for name in zf.namelist() if name.lower().endswith(".fb2")]
        if not fb2_entries:
            return []
        fb2_name = fb2_entries[0]
        fb2_xml = zf.read(fb2_name)

    root = ET.fromstring(fb2_xml)
    wrote = update_fb2_tree_non_destructive(root, existing, resolved)
    if not wrote:
        return []

    if dry_run:
        return [line.replace("WROTE", "WOULD_WRITE_FBZ", 1) for line in wrote]

    new_xml = ET.tostring(root, encoding="utf-8", xml_declaration=True)
    write_zip_entry(path, fb2_name, new_xml)
    return wrote


def write_zip_opf_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool, source: str
) -> List[str]:
    found = extract_zip_opf_entry(path)
    if not found:
        return []
    opf_name, opf_xml = found

    opf_root = ET.fromstring(opf_xml)
    metadata_el = opf_root.find(".//opf:metadata", EPUB_NS)
    if metadata_el is None:
        return []

    wrote: List[str] = []

    def append_dc(local_name: str, value: str, xpath: str) -> None:
        existing_node = metadata_el.find(xpath, EPUB_NS)
        if existing_node is not None and (existing_node.text or "").strip():
            return
        node = ET.Element(f"{{{EPUB_NS['dc']}}}{local_name}")
        node.text = value
        metadata_el.append(node)
        wrote.append(f"WROTE {local_name}={value}")

    if not existing.title and resolved.title:
        append_dc("title", resolved.title, "./dc:title")
    if not existing.author and resolved.author:
        append_dc("creator", resolved.author, "./dc:creator")
    if not existing.publisher and resolved.publisher:
        append_dc("publisher", resolved.publisher, "./dc:publisher")
    if not existing.language and resolved.language:
        append_dc("language", resolved.language, "./dc:language")
    if not existing.description and resolved.description:
        append_dc("description", resolved.description, "./dc:description")
    if not existing.published_date and resolved.published_date:
        append_dc("date", resolved.published_date, "./dc:date")
    if not existing.subjects and resolved.subjects and not metadata_el.findall("./dc:subject", EPUB_NS):
        for subject in resolved.subjects:
            value = (subject or "").strip()
            if not value:
                continue
            node = ET.Element(f"{{{EPUB_NS['dc']}}}subject")
            node.text = value
            metadata_el.append(node)
            wrote.append(f"WROTE subject={value}")

    if not existing.isbn and resolved.isbn13:
        found_identifier = False
        for ident in metadata_el.findall("./dc:identifier", EPUB_NS):
            if (ident.text or "").strip():
                found_identifier = True
                break
        if not found_identifier:
            node = ET.Element(f"{{{EPUB_NS['dc']}}}identifier")
            node.text = resolved.isbn13
            metadata_el.append(node)
            wrote.append(f"WROTE identifier={resolved.isbn13}")

    if not wrote:
        return []

    if dry_run:
        return [line.replace("WROTE", f"WOULD_WRITE_{source.upper()}", 1) for line in wrote]

    new_xml = ET.tostring(opf_root, encoding="utf-8", xml_declaration=True)
    write_zip_entry(path, opf_name, new_xml)
    return wrote


def write_rtf_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool
) -> List[str]:
    updates: Dict[str, str] = {}
    if not existing.title and resolved.title:
        updates["title"] = resolved.title
    if not existing.author and resolved.author:
        updates["author"] = resolved.author
    if not existing.description and resolved.description:
        updates["subject"] = resolved.description
    if not existing.subjects and resolved.subjects:
        updates["keywords"] = ", ".join(s for s in resolved.subjects if s)

    if not updates:
        return []
    if dry_run:
        return [f"WOULD_WRITE_RTF {k}={v}" for k, v in updates.items()]

    text = path.read_text(errors="ignore")
    info_match = re.search(r"\{\\info(?P<body>.*?)\}", text, flags=re.DOTALL)
    body = info_match.group("body") if info_match else ""

    for key, value in updates.items():
        if re.search(rf"\\{key}\s+", body):
            continue
        body += f"\\{key} {value}"

    new_info = "{\\info" + body + "}"
    if info_match:
        text = text[:info_match.start()] + new_info + text[info_match.end():]
    else:
        insert_at = text.find("{")
        if insert_at == -1:
            text = "{\\rtf1" + new_info + text + "}"
        else:
            text = text[: insert_at + 1] + new_info + text[insert_at + 1 :]

    path.write_text(text)
    return [f"WROTE {k}={v}" for k, v in updates.items()]


def write_mobi_family_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool, source: str
) -> List[str]:
    updates: List[Tuple[str, str]] = []
    if not existing.title and resolved.title:
        updates.append(("title", resolved.title))
    if not existing.author and resolved.author:
        updates.append(("author", resolved.author))
    if not existing.publisher and resolved.publisher:
        updates.append(("publisher", resolved.publisher))
    if not existing.description and resolved.description:
        updates.append(("description", resolved.description))
    if not existing.isbn and resolved.isbn13:
        updates.append(("isbn", resolved.isbn13))
    if not existing.subjects and resolved.subjects:
        for subject in resolved.subjects:
            updates.append(("subject", subject))

    if not updates:
        return []

    # Safe placeholder: report planned enrichment but avoid mutating EXTH
    # until full binary rewrite coverage is added for all variants.
    if dry_run:
        return [f"WOULD_WRITE_{source.upper()} {k}={v}" for k, v in updates]
    return [f"SKIP_WRITE_{source.upper()} {k}={v}" for k, v in updates]


def write_metadata_non_destructive(
    path: Path, existing: EmbeddedMetadata, resolved: WorkMetadata, dry_run: bool
) -> List[str]:
    ext = path.suffix.lower()
    lower_name = path.name.lower()
    if ext == ".epub" or ext == ".kepub" or lower_name.endswith(".kepub.epub"):
        return write_epub_metadata_non_destructive(path, existing, resolved, dry_run)
    if ext == ".pdf":
        return write_pdf_metadata_non_destructive(path, existing, resolved, dry_run)
    if ext == ".docx":
        return write_docx_metadata_non_destructive(path, existing, resolved, dry_run)
    if ext == ".odt":
        return write_odt_metadata_non_destructive(path, existing, resolved, dry_run)
    if ext == ".fb2":
        return write_fb2_metadata_non_destructive(path, existing, resolved, dry_run)
    if ext == ".fbz":
        return write_fbz_metadata_non_destructive(path, existing, resolved, dry_run)
    if ext in {".htmlz", ".txtz"}:
        return write_zip_opf_metadata_non_destructive(path, existing, resolved, dry_run, source=ext.lstrip("."))
    if ext == ".rtf":
        return write_rtf_metadata_non_destructive(path, existing, resolved, dry_run)
    if ext in {".mobi", ".azw", ".azw1", ".azw3", ".prc"}:
        return write_mobi_family_metadata_non_destructive(path, existing, resolved, dry_run, source=ext.lstrip("."))
    return []


# --------------------------------------------------
# Hardcover API
# --------------------------------------------------
class HardcoverClient:
    def __init__(self, base_url: str, timeout: int = 20) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = httpx.Client(timeout=timeout, follow_redirects=True)

    def search(self, query: str) -> List[Dict[str, Any]]:
        response = self.client.get(f"{self.base_url}/search", params={"q": query})
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, list) else []

    def work(self, work_id: int) -> Dict[str, Any]:
        response = self.client.get(f"{self.base_url}/work/{work_id}")
        response.raise_for_status()
        data = response.json()
        return data if isinstance(data, dict) else {}

    def close(self) -> None:
        self.client.close()


# --------------------------------------------------
# Parse exact /work schema
# --------------------------------------------------
def parse_work_metadata(work_id: int, payload: Dict[str, Any]) -> WorkMetadata:
    def first_string(source: Dict[str, Any], *keys: str) -> Optional[str]:
        for key in keys:
            value = source.get(key)
            if isinstance(value, str):
                text = value.strip()
                if text:
                    return text
        return None

    def first_list_strings(source: Dict[str, Any], *keys: str) -> List[str]:
        out: List[str] = []
        seen = set()
        for key in keys:
            value = source.get(key)
            if not isinstance(value, list):
                continue
            for item in value:
                text = None
                if isinstance(item, str):
                    text = item.strip()
                elif isinstance(item, dict):
                    for candidate_key in ("Name", "name", "Tag", "tag", "Label", "label"):
                        raw = item.get(candidate_key)
                        if isinstance(raw, str) and raw.strip():
                            text = raw.strip()
                            break
                if text and text not in seen:
                    seen.add(text)
                    out.append(text)
        return out

    full_title = payload.get("FullTitle")
    short_title = payload.get("Title") or payload.get("ShortTitle")

    authors = payload.get("Authors") or []
    author = None
    if isinstance(authors, list) and authors:
        first = authors[0]
        if isinstance(first, dict):
            author = first.get("Name")

    books = payload.get("Books") or []
    isbn13 = None
    publisher = None
    language = None
    description = first_string(payload, "Description", "Summary", "Synopsis")
    published_date = first_string(
        payload,
        "ReleaseDate",
        "PublicationDate",
        "PublishedDate",
        "PubDate",
    )
    subjects = first_list_strings(payload, "Subjects", "Genres", "Tags")

    if isinstance(books, list) and books:
        first_book = books[0]
        if isinstance(first_book, dict):
            isbn13 = first_book.get("Isbn13")
            publisher = first_book.get("Publisher")
            language = first_book.get("Language")

            if not description:
                description = first_string(first_book, "Description", "Summary", "Synopsis")
            if not published_date:
                published_date = first_string(
                    first_book,
                    "ReleaseDate",
                    "PublicationDate",
                    "PublishedDate",
                    "PubDate",
                )
            if not subjects:
                subjects = first_list_strings(first_book, "Subjects", "Genres", "Tags")

            # Prefer book-level FullTitle too, if present
            if not full_title:
                full_title = first_book.get("FullTitle")
            if not short_title:
                short_title = first_book.get("Title") or first_book.get("ShortTitle")

    # Prefer full title for matching and final naming
    chosen_title = full_title or short_title

    return WorkMetadata(
        work_id=work_id,
        title=chosen_title,
        short_title=short_title,
        author=author,
        isbn13=isbn13,
        publisher=publisher,
        language=language,
        full_title=full_title,
        description=description,
        published_date=published_date,
        subjects=subjects or None,
    )


def work_title_candidates(work: WorkMetadata) -> List[str]:
    titles = [work.title, work.full_title, work.short_title]
    out: List[str] = []
    seen = set()
    for value in titles:
        normalized = norm_text(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        out.append(value or "")
    return out

# --------------------------------------------------
# Matching logic
# --------------------------------------------------
def normalize_search_query(value: str) -> str:
    value = unicodedata.normalize("NFKD", value)
    value = value.encode("ascii", "ignore").decode("ascii")
    value = re.sub(r"[^\w\s]", " ", value)
    value = re.sub(r"\s+", " ", value).strip()
    return value

def build_query(embedded: EmbeddedMetadata, filename_meta: FilenameMetadata, filename: str) -> str:
    parts: List[str] = []

    if embedded.author:
        parts.append(embedded.author)
    elif filename_meta.author:
        parts.append(filename_meta.author)

    if embedded.title:
        parts.append(embedded.title)
    elif filename_meta.title:
        parts.append(filename_meta.title)

    if not parts:
        stem = Path(filename).stem
        stem = re.sub(r"[_\-.]+", " ", stem)
        stem = re.sub(r"\s+", " ", stem).strip()
        parts.append(stem)

    return normalize_search_query(" ".join(parts))

def score_work(
    embedded: EmbeddedMetadata,
    filename_meta: FilenameMetadata,
    work: WorkMetadata,
) -> Candidate:
    compare_title = embedded.title or filename_meta.title
    compare_author = embedded.author or filename_meta.author

    norm_compare_title = norm_text(compare_title)
    norm_compare_author = norm_text(compare_author)
    norm_work_title = norm_text(work.title or work.full_title)
    norm_work_author = norm_text(work.author)

    if (
        norm_compare_title
        and norm_compare_author
        and norm_compare_title == norm_work_title
        and norm_compare_author == norm_work_author
    ):
        return Candidate(
            work=work,
            score_title=1.0,
            score_author=1.0,
            score_total=1.0,
            title_match=True,
            author_match=True,
        )

    title_options = work_title_candidates(work)
    if title_options:
        s_title = max(title_score(compare_title, option) for option in title_options)
    else:
        s_title = 0.0
    s_author = author_score(compare_author, work.author)

    def contains_match(a: Optional[str], b: Optional[str]) -> bool:
        na = norm_text(a)
        nb = norm_text(b)
        if not na or not nb:
            return False
        if na == nb:
            return True

        sa = set(na.split())
        sb = set(nb.split())
        if len(sa) >= 2 and sa.issubset(sb):
            return True
        if len(sb) >= 2 and sb.issubset(sa):
            return True

        shorter = na if len(na) <= len(nb) else nb
        longer = nb if len(na) <= len(nb) else na
        if len(shorter) >= 8 and shorter in longer:
            return True

        return False

    t_match = any(contains_match(compare_title, option) for option in title_options)
    a_match = contains_match(compare_author, work.author)

    if compare_title and compare_author:
        s_total = 0.55 * s_title + 0.45 * s_author
    elif compare_title:
        s_total = s_title
    elif compare_author:
        s_total = s_author
    else:
        s_total = 0.0

    return Candidate(
        work=work,
        score_title=s_title,
        score_author=s_author,
        score_total=s_total,
        title_match=t_match,
        author_match=a_match,
    )

def choose_candidate(
    candidates: List[Candidate],
    min_score: float,
    min_margin: float,
    require_author_match: bool,
    min_author_score: float,
) -> Optional[Candidate]:
    if not candidates:
        return None

    candidates = sorted(candidates, key=lambda c: c.score_total, reverse=True)
    top = candidates[0]

    if top.score_total < min_score:
        if not (top.title_match and top.author_match):
            return None

    if require_author_match and top.score_author < min_author_score and not top.author_match:
        return None

    if len(candidates) == 1:
        return top

    second = candidates[1]
    if (top.score_total - second.score_total) < min_margin:
        if not (top.title_match and top.author_match):
            return None

        close_candidates = [
            c for c in candidates if (top.score_total - c.score_total) < min_margin
        ]
        if not close_candidates:
            return top

        close_candidates.sort(
            key=lambda c: (
                c.title_match and c.author_match,
                c.score_author,
                c.score_title,
                c.score_total,
                -c.work.work_id,
            ),
            reverse=True,
        )
        return close_candidates[0]

    return top


# --------------------------------------------------
# Optional sync hooks
# --------------------------------------------------
def trigger_kavita_scan(
    kavita_url: str, kavita_api_key: str, kavita_library_id: Optional[str], dry_run: bool
) -> None:
    if kavita_library_id:
        endpoint = f"{kavita_url.rstrip('/')}/api/Library/scan?libraryId={kavita_library_id}"
    else:
        endpoint = f"{kavita_url.rstrip('/')}/api/Library/scan-all"

    if dry_run:
        log(f"WOULD_KAVITA_SCAN : {endpoint}")
        return

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.post(endpoint, headers={"x-api-key": kavita_api_key})
        response.raise_for_status()
    log("KAVITA_SCAN       : triggered")


def trigger_readarr_command(
    readarr_url: str, readarr_api_key: str, command_json: str, dry_run: bool
) -> None:
    endpoint = f"{readarr_url.rstrip('/')}/api/v1/command"

    if dry_run:
        log(f"WOULD_READARR_CMD : {command_json}")
        return

    with httpx.Client(timeout=30, follow_redirects=True) as client:
        response = client.post(
            endpoint,
            headers={"X-Api-Key": readarr_api_key, "Content-Type": "application/json"},
            content=command_json,
        )
        response.raise_for_status()
    log("READARR_COMMAND   : triggered")


# --------------------------------------------------
# Destination path
# --------------------------------------------------
def destination_path(library_root: Path, author: str, title: str, ext: str) -> Path:
    author_fs = safe_fs(author)
    title_fs = safe_fs(title)
    return library_root / author_fs / title_fs / f"{title_fs}{ext}"


# --------------------------------------------------
# Main processor
# --------------------------------------------------
def process_file(
    file_path: Path,
    library_root: Path,
    config: AppConfig,
) -> int:
    embedded = read_embedded_metadata(file_path)
    filename_meta = parse_filename_metadata(file_path)

    log(f"FILE              : {file_path}")
    log(f"EMBEDDED_SOURCE   : {embedded.source}")
    log(f"EMBEDDED_TITLE    : {embedded.title!r}")
    log(f"EMBEDDED_AUTHOR   : {embedded.author!r}")
    log(f"EMBEDDED_ISBN     : {embedded.isbn!r}")
    log(f"FILENAME_TITLE    : {filename_meta.title!r}")
    log(f"FILENAME_AUTHOR   : {filename_meta.author!r}")

    score_title_basis = embedded.title or filename_meta.title
    score_author_basis = embedded.author or filename_meta.author
    log(f"SCORE_TITLE_BASIS : {score_title_basis!r}")
    log(f"SCORE_AUTHOR_BASIS: {score_author_basis!r}")

    query = build_query(embedded, filename_meta, file_path.name)
    log(f"SEARCH_QUERY      : {query!r}")

    client = HardcoverClient(config.api_base)
    try:
        search_rows = client.search(query)
        log(f"SEARCH_ROWS       : {len(search_rows)}")

        work_ids: List[int] = []
        for row in search_rows:
            work_id = row.get("workId")
            if isinstance(work_id, int) and work_id > 0:
                work_ids.append(work_id)

        work_ids = list(dict.fromkeys(work_ids))
        log(f"WORK_IDS          : {work_ids}")

        candidates: List[Candidate] = []
        for work_id in work_ids[:10]:
            payload = client.work(work_id)
            work = parse_work_metadata(work_id, payload)
            candidate = score_work(embedded, filename_meta, work)
            candidates.append(candidate)

        candidates.sort(key=lambda c: c.score_total, reverse=True)

        for idx, candidate in enumerate(candidates[:5], start=1):
            log(
                f"CANDIDATE_{idx}       : "
                f"workId={candidate.work.work_id} "
                f"title={candidate.work.title!r} "
                f"author={candidate.work.author!r} "
                f"title_match={candidate.title_match!r} "
                f"author_match={candidate.author_match!r} "
                f"title_score={candidate.score_title:.2f} "
                f"author_score={candidate.score_author:.2f} "
                f"total={candidate.score_total:.2f}"
            )

        chosen = choose_candidate(
            candidates,
            min_score=config.min_score,
            min_margin=config.min_margin,
            require_author_match=bool(score_author_basis),
            min_author_score=0.45,
        )
        resolved: Optional[WorkMetadata] = None
        if chosen:
            resolved = chosen.work
            log(f"CHOSEN_WORK_ID     : {resolved.work_id}")
            log(f"RESOLVED_TITLE     : {resolved.title!r}")
            log(f"RESOLVED_AUTHOR    : {resolved.author!r}")
            log(f"RESOLVED_ISBN13    : {resolved.isbn13!r}")
            log(f"RESOLVED_PUBLISHER : {resolved.publisher!r}")
            log(f"RESOLVED_LANGUAGE  : {resolved.language!r}")
            log(f"RESOLVED_DATE      : {resolved.published_date!r}")
            log(f"RESOLVED_SUBJECTS  : {resolved.subjects!r}")
        elif embedded.title and embedded.author:
            log("DECISION          : no strong match; moving using embedded title/author")
        else:
            log("DECISION          : no non-ambiguous strong match; leaving untouched")
            return 0

        final_title = embedded.title or filename_meta.title or (resolved.title if resolved else None)
        final_author = embedded.author or filename_meta.author or (resolved.author if resolved else None)

        if not final_title or not final_author:
            log("DECISION          : insufficient final metadata; leaving untouched")
            return 0

        writes: List[str] = []
        if resolved:
            writes = write_metadata_non_destructive(file_path, embedded, resolved, config.dry_run)
        for line in writes:
            log(line)

        dest = destination_path(library_root, final_author, final_title, file_path.suffix.lower())
        dest = unique_path(dest)

        if config.dry_run:
            log(f"WOULD_MOVE        : {file_path} -> {dest}")
        else:
            ensure_parent(dest)
            shutil.move(str(file_path), str(dest))
            log(f"MOVED             : {file_path} -> {dest}")

        if config.kavita_scan:
            if not config.kavita_api_key:
                log("KAVITA_SCAN       : skipped, no API key")
            else:
                trigger_kavita_scan(
                    kavita_url=config.kavita_url,
                    kavita_api_key=config.kavita_api_key,
                    kavita_library_id=config.kavita_library_id,
                    dry_run=config.dry_run,
                )

        if config.readarr_scan:
            if not config.readarr_api_key:
                log("READARR_COMMAND   : skipped, no API key")
            else:
                trigger_readarr_command(
                    readarr_url=config.readarr_url,
                    readarr_api_key=config.readarr_api_key,
                    command_json=config.readarr_command_json,
                    dry_run=config.dry_run,
                )

        return 0
    finally:
        client.close()


# --------------------------------------------------
# Watch mode
# --------------------------------------------------
class WatchContext:
    def __init__(self, library_root: Path, config: AppConfig) -> None:
        self.library_root = library_root
        self.config = config
        self.recent: Dict[str, float] = {}

    def should_process(self, path: Path) -> bool:
        key = str(path.resolve())
        now = time.time()

        expired = [k for k, ts in self.recent.items() if now - ts > 60]
        for k in expired:
            self.recent.pop(k, None)

        if key in self.recent and now - self.recent[key] < 10:
            return False

        self.recent[key] = now
        return True

    def handle_path(self, path: Path) -> None:
        if not path.is_file():
            return
        if is_temp_or_hidden(path):
            log(f"SKIP              : temp/hidden {path}")
            return
        if not self.should_process(path):
            log(f"SKIP              : duplicate event {path}")
            return

        log(f"WATCH_EVENT       : {path}")
        stable = wait_for_stable_file(path, settle_seconds=self.config.settle_seconds)
        if not stable:
            log(f"SKIP              : file did not stabilize {path}")
            return

        try:
            process_file(path, self.library_root, self.config)
        except Exception as exc:
            log(f"ERROR             : processing failed for {path}: {exc}")


class IncomingHandler(FileSystemEventHandler):
    def __init__(self, context: WatchContext) -> None:
        super().__init__()
        self.context = context

    def on_created(self, event) -> None:
        if not event.is_directory:
            self.context.handle_path(Path(str(event.src_path)))

    def on_moved(self, event) -> None:
        if not event.is_directory:
            self.context.handle_path(Path(str(event.dest_path)))


def initial_scan(watch_dir: Path, recursive: bool, context: WatchContext) -> None:
    if recursive:
        paths = sorted(p for p in watch_dir.rglob("*") if p.is_file())
    else:
        paths = sorted(p for p in watch_dir.iterdir() if p.is_file())

    for path in paths:
        context.handle_path(path)


def run_watch_mode(
    watch_dir: Path,
    library_root: Path,
    config: AppConfig,
    recursive: bool,
    do_initial_scan: bool,
) -> int:
    context = WatchContext(library_root=library_root, config=config)

    if do_initial_scan:
        log(f"INITIAL_SCAN      : {watch_dir}")
        initial_scan(watch_dir, recursive, context)

    event_handler = IncomingHandler(context)
    observer = Observer()
    observer.schedule(event_handler, str(watch_dir), recursive=recursive)
    observer.start()

    log(f"WATCHING          : {watch_dir} (recursive={recursive})")

    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        log("STOPPING          : keyboard interrupt")
    finally:
        observer.stop()
        observer.join()

    return 0


# --------------------------------------------------
# CLI
# --------------------------------------------------
def build_config(args: argparse.Namespace) -> AppConfig:
    api_base = args.api_base or env_str("EBOOK_API_BASE", DEFAULT_API_BASE) or DEFAULT_API_BASE
    dry_run = args.dry_run if args.dry_run is not None else env_bool("EBOOK_DRY_RUN", False)
    min_score = args.min_score if args.min_score is not None else env_float("EBOOK_MIN_SCORE", 0.82)
    min_margin = args.min_margin if args.min_margin is not None else env_float("EBOOK_MIN_MARGIN", 0.08)

    kavita_scan = (
        args.kavita_scan if args.kavita_scan is not None else env_bool("EBOOK_KAVITA_SCAN", False)
    )
    kavita_url = args.kavita_url or env_str("EBOOK_KAVITA_URL", "http://127.0.0.1:5000") or "http://127.0.0.1:5000"
    kavita_api_key = args.kavita_api_key or env_str("EBOOK_KAVITA_API_KEY")
    kavita_library_id = args.kavita_library_id or env_str("EBOOK_KAVITA_LIBRARY_ID")

    readarr_scan = (
        args.readarr_scan if args.readarr_scan is not None else env_bool("EBOOK_READARR_SCAN", False)
    )
    readarr_url = args.readarr_url or env_str("EBOOK_READARR_URL", "http://127.0.0.1:8787") or "http://127.0.0.1:8787"
    readarr_api_key = args.readarr_api_key or env_str("EBOOK_READARR_API_KEY")
    readarr_command_json = (
        args.readarr_command_json
        or env_str("EBOOK_READARR_COMMAND_JSON", DEFAULT_READARR_COMMAND)
        or DEFAULT_READARR_COMMAND
    )

    settle_seconds = (
        args.settle_seconds
        if args.settle_seconds is not None
        else env_float("EBOOK_SETTLE_SECONDS", 1.5)
    )

    return AppConfig(
        api_base=api_base,
        dry_run=dry_run,
        min_score=min_score,
        min_margin=min_margin,
        kavita_scan=kavita_scan,
        kavita_url=kavita_url,
        kavita_api_key=kavita_api_key,
        kavita_library_id=kavita_library_id,
        readarr_scan=readarr_scan,
        readarr_url=readarr_url,
        readarr_api_key=readarr_api_key,
        readarr_command_json=readarr_command_json,
        settle_seconds=settle_seconds,
    )


def main() -> int:
    env_help = (
        "Configuration precedence: CLI switch -> environment variable -> built-in default.\n\n"
        "One-shot minimal usage:\n"
        "  EBOOK_LIBRARY_ROOT=/path/to/library ebook_resolve_move.py /path/to/book.epub\n\n"
        "Environment variables:\n"
        "  EBOOK_LIBRARY_ROOT (required unless --library-root is passed)\n"
        "  EBOOK_API_BASE\n"
        "  EBOOK_DRY_RUN\n"
        "  EBOOK_MIN_SCORE\n"
        "  EBOOK_MIN_MARGIN\n"
        "  EBOOK_KAVITA_SCAN\n"
        "  EBOOK_KAVITA_URL\n"
        "  EBOOK_KAVITA_API_KEY\n"
        "  EBOOK_KAVITA_LIBRARY_ID\n"
        "  EBOOK_READARR_SCAN\n"
        "  EBOOK_READARR_URL\n"
        "  EBOOK_READARR_API_KEY\n"
        "  EBOOK_READARR_COMMAND_JSON\n"
        "  EBOOK_SETTLE_SECONDS"
    )

    parser = argparse.ArgumentParser(
        description="Resolve ebook metadata via hardcover.bookinfo.pro, fill missing metadata, move to Author/Title/Title.ext, and optionally watch a directory.",
        epilog=env_help,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )

    parser.add_argument("file", nargs="?", help="Input ebook file for one-shot mode (required unless --watch-directory)")
    parser.add_argument("library_root_positional", nargs="?", help=argparse.SUPPRESS)
    parser.add_argument("--library-root", default=None, help="Library root directory (or set EBOOK_LIBRARY_ROOT)")

    parser.add_argument("--watch-directory", help="Watch a directory for new files instead of processing one file")
    parser.add_argument("--recursive", action="store_true", help="Watch recursively")
    parser.add_argument("--initial-scan", action="store_true", help="Process files already present in watch directory at startup")
    parser.add_argument("--settle-seconds", type=float, default=None, help="Seconds between file stability checks in watch mode (or EBOOK_SETTLE_SECONDS)")

    parser.add_argument("--api-base", default=None, help="Book metadata API base (or EBOOK_API_BASE)")
    parser.add_argument("--dry-run", action=argparse.BooleanOptionalAction, default=None, help="Show what would happen (or EBOOK_DRY_RUN)")
    parser.add_argument("--min-score", type=float, default=None, help="Minimum score to accept a match (or EBOOK_MIN_SCORE)")
    parser.add_argument("--min-margin", type=float, default=None, help="Minimum lead over second-best match (or EBOOK_MIN_MARGIN)")

    parser.add_argument("--kavita-scan", action=argparse.BooleanOptionalAction, default=None, help="Trigger Kavita scan after successful move (or EBOOK_KAVITA_SCAN)")
    parser.add_argument("--kavita-url", default=None, help="Kavita base URL (or EBOOK_KAVITA_URL)")
    parser.add_argument("--kavita-api-key", default=None, help="Kavita API key (or EBOOK_KAVITA_API_KEY)")
    parser.add_argument("--kavita-library-id", default=None, help="Kavita library ID; omit to scan all (or EBOOK_KAVITA_LIBRARY_ID)")

    parser.add_argument("--readarr-scan", action=argparse.BooleanOptionalAction, default=None, help="Trigger Readarr command after successful move (or EBOOK_READARR_SCAN)")
    parser.add_argument("--readarr-url", default=None, help="Readarr base URL (or EBOOK_READARR_URL)")
    parser.add_argument("--readarr-api-key", default=None, help="Readarr API key (or EBOOK_READARR_API_KEY)")
    parser.add_argument(
        "--readarr-command-json",
        default=None,
        help="Raw JSON body to POST to /api/v1/command (or EBOOK_READARR_COMMAND_JSON)",
    )

    args = parser.parse_args()
    library_root_value = args.library_root or args.library_root_positional or env_str("EBOOK_LIBRARY_ROOT")
    if not library_root_value:
        print("ERROR: set --library-root or EBOOK_LIBRARY_ROOT", file=sys.stderr)
        return 2

    library_root = Path(library_root_value)

    if not library_root.exists():
        print(f"ERROR: library_root does not exist: {library_root}", file=sys.stderr)
        return 2

    config = build_config(args)

    if args.watch_directory:
        watch_dir = Path(args.watch_directory)
        if not watch_dir.exists() or not watch_dir.is_dir():
            print(f"ERROR: watch directory does not exist: {watch_dir}", file=sys.stderr)
            return 2
        return run_watch_mode(
            watch_dir=watch_dir,
            library_root=library_root,
            config=config,
            recursive=args.recursive,
            do_initial_scan=args.initial_scan,
        )

    if not args.file:
        print("ERROR: either provide <file> or use --watch-directory", file=sys.stderr)
        return 2

    file_path = Path(args.file)
    if not file_path.is_file():
        print(f"ERROR: not a file: {file_path}", file=sys.stderr)
        return 2

    return process_file(file_path, library_root, config)


if __name__ == "__main__":
    raise SystemExit(main())

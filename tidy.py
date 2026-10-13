#!/usr/bin/env python3
"""
tidy — a local, low-RAM file organizer for macOS.

Cascade (cheapest first; each layer only sees what the previous one couldn't place):
  0. Duplicates   — size + SHA-256. Never deleted, moved to _Review/Duplicates.
  1. Rules        — extension, filename patterns, Spotlight camera metadata. No model.
  2. Embeddings   — tiny embedding model vs. category descriptions. Outputs vectors, not tokens.
  3. Small LLM    — only for low-confidence files; prompt lists the top-3 candidates and the
                    output is grammar-constrained to {"c": <int>} (a handful of tokens).
Models run through Ollama, which is started on demand and stopped/unloaded when the run ends.
Every move is logged; `tidy undo` reverses the last run.

Stdlib only. Optional: pypdf (better PDF text); falls back to macOS PDFKit via JXA.
"""
from __future__ import annotations

import argparse
import hashlib
import html
import json
import logging
import math
import os
import plistlib
import re
import shutil
import stat
import subprocess
import sys
import time
import urllib.request
import zipfile
from datetime import datetime
from pathlib import Path

APP_DIR = Path(os.environ.get("TIDY_HOME", str(Path.home() / ".tidy")))
CONFIG_PATH = APP_DIR / "config.json"
RUNS_DIR = APP_DIR / "runs"
PLAN_PATH = APP_DIR / "last_plan.json"
EMB_CACHE = APP_DIR / "category_vectors.json"
STATE_PATH = APP_DIR / "watch_state.json"
RUN_COMMAND = APP_DIR / "Run Tidy.command"
# Private values (client names) live in a gitignored .env, never in the repo. Checked in order;
# the real environment beats any file. The repo copy is found via the --dev symlink.
ENV_FILES = [APP_DIR / ".env", Path(__file__).resolve().parent / ".env"]
AGENT_LABEL = "com.tidy.watch"
AGENT_PLIST = Path.home() / "Library/LaunchAgents" / f"{AGENT_LABEL}.plist"

SF_DATALESS = 0x40000000  # iCloud placeholder whose content isn't downloaded
PARTIAL_EXTS = {".crdownload", ".download", ".part", ".partial", ".tmp", ".opdownload"}
JUNK_NAMES = {".DS_Store", ".localized", "Icon\r"}
GENERIC_WORDS = {"document", "doc", "file", "scan", "scanned", "untitled", "download",
                 "copy", "image", "img", "new", "final", "draft", "pdf", "print"}
PLAN_MAX_AGE = 24 * 3600
logging.getLogger("pypdf").setLevel(logging.CRITICAL)  # malformed PDFs are common in Downloads

DEFAULT_CONFIG = {
    "scan": ["~/Downloads", "~/Desktop"],
    "dest": "~/Documents",
    "settle_minutes": 2,
    "ollama_url": "http://127.0.0.1:11434",
    "embed_model": "all-minilm",
    "llm_model": "qwen2.5:1.5b",
    "embed_accept": 0.40,
    "embed_margin": 0.05,
    "llm_candidates": 3,
    "snippet_chars": 600,
    "notify_threshold": 5,
    "notify_cooldown_minutes": 30,
    "year_subfolders": ["screenshots", "photos", "finance"],
    # Relative paths sit under "dest"; a path starting with ~ or / is used as-is
    # (e.g. "photos": "~/Pictures/Photos"). {scope} becomes Corporate or Personal.
    "folders": {
        "finance": "{scope}/Finance",
        "legal": "{scope}/Legal & Contracts",
        "work": "{scope}/Work & Projects",
        "career": "{scope}/Career & HR",
        "reference": "{scope}/Reference",
        "travel": "{scope}/Travel & Household",
        "screenshots": "Media & Files/Screenshots",
        "photos": "Media & Files/Photos",
        "graphics": "Media & Files/Graphics",
        "video": "Media & Files/Video",
        "audio": "Media & Files/Audio",
        "installers": "Media & Files/Installers",
        "archives": "Media & Files/Archives",
        "code": "Media & Files/Code & Data",
        "design": "Media & Files/Design",
        "fonts": "Media & Files/Fonts",
        "other": "Media & Files/Other",
        "review": "_Review",
        "duplicates": "_Review/Duplicates",
        "unclear_scope": "_Review/Corporate or Personal",
    },
    # Categories the embedding/LLM layers can choose from. The description IS the classifier.
    "semantic": {
        "finance": "Finance: invoices, receipts, bank statements, billing, payments, quotations, purchase orders, payslips, taxes, budgets",
        "legal": "Legal and contracts: contracts, agreements, NDAs, MOAs, terms, permits, licenses, certificates, government and compliance forms",
        "work": "Work and projects: proposals, reports, specifications, requirements, meeting notes, project plans, presentations, client deliverables",
        "career": "Career: resumes, CVs, cover letters, job descriptions, job applications, interview and HR documents",
        "reference": "Reference and learning: manuals, guides, ebooks, research papers, tutorials, course materials, technical documentation",
        "travel": "Travel and household: travel bookings, tickets, itineraries, utilities, home, health, school, family and letters",
    },
    "semantic_exts": ["pdf", "doc", "docx", "rtf", "txt", "md", "odt", "pages", "xlsx", "xls",
                      "csv", "numbers", "pptx", "ppt", "key", "html", "htm"],
    "ext_map": {
        "photos": ["heic", "jpg", "jpeg", "dng", "cr2", "cr3", "nef", "arw", "raf", "orf", "rw2"],
        "graphics": ["png", "gif", "webp", "svg", "bmp", "tif", "tiff", "ico", "avif"],
        "video": ["mp4", "mov", "m4v", "avi", "mkv", "webm", "wmv"],
        "audio": ["mp3", "m4a", "wav", "aac", "flac", "ogg", "aiff", "opus"],
        "installers": ["dmg", "pkg", "mpkg", "iso", "exe", "msi", "apk"],
        "archives": ["zip", "rar", "7z", "tar", "gz", "tgz", "bz2", "xz"],
        "code": ["py", "js", "ts", "tsx", "jsx", "sql", "sh", "java", "cs", "go", "rb", "php",
                 "ipynb", "json", "yaml", "yml", "xml", "swift", "kt", "c", "cpp", "h", "css"],
        "design": ["psd", "ai", "fig", "sketch", "xd", "indd", "afdesign", "afphoto"],
        "fonts": ["ttf", "otf", "woff", "woff2"],
        "reference": ["epub", "mobi", "azw3"],
        "travel": ["ics", "vcf"],
    },
    # JPG/HEIC without a camera model in Spotlight metadata -> Graphics, not Photos.
    "camera_check_exts": ["jpg", "jpeg", "heic"],
    # [regex on lowercase filename, category, scope]  scope: image | doc | any
    "name_hints": [
        ["^(screen ?shot|screenshot|cleanshot|capture d)", "screenshots", "image"],
        ["(invoice|receipt|statement|billing|payslip|payroll|remittance|quotation|purchase.?order|\\bsoa\\b|\\bbir\\b|\\b2307\\b)", "finance", "doc"],
        ["(contract|agreement|\\bnda\\b|\\bmoa\\b|\\bmou\\b|addendum|affidavit|notari[sz]ed)", "legal", "doc"],
        ["(resume|\\bcv\\b|curriculum.?vitae|cover.?letter)", "career", "doc"],
        ["(boarding.?pass|itinerary|e-?ticket|booking|reservation)", "travel", "doc"],
    ],
    # ── Corporate vs Personal (applies to the document categories above) ──
    # Precedence: personal filename term > corporate filename term > corporate term in content
    # > embedding > small LLM > _Review/Corporate or Personal.
    # Corporate = YOUR companies and clients, not "issued by any company" (your bank is a corp too).
    # Kept out of the repo: set TIDY_CORPORATE_TERMS in .env (see .env.example).
    "corporate_terms": [],
    # Company words too generic to trust in content ("employee", "compliance") — filename only.
    # Set TIDY_CORPORATE_NAME_TERMS in .env.
    "corporate_name_terms": [],
    # Client/project folders inside Corporate (Corporate/<client>/<category>), decided by name only:
    # {name: [regex, note]}. Set TIDY_CLIENT_<KEY>="Name = regex = note" lines in .env. Client regexes
    # also count as corporate terms. Empty = no client folders.
    "clients": {},
    "internal_folder": "_Internal",   # Corporate files for no client (your own company)
    "company_name": "",               # TIDY_COMPANY_NAME: the model files documents as this company's CEO
    # Personal records that often mention your company (resume, payslip) — filename only.
    "personal_name_terms": ["resume", "\\bcv\\b", "cover.?letter", "payslip", "\\b2316\\b", "passport",
                            "\\bnbi\\b", "birth.?cert", "boarding.?pass", "medical", "\\blab.?result"],
    "scope_desc": {
        "corporate": "Company business document: clients, vendors, employees, projects, proposals, contracts, company finances and operations",
        "personal": "Personal document about my own private life: my own bank and bills, health, family, home, travel, job applications, identity documents",
    },
    "scope_margin": 0.06,
}


# ───────────────────────────── helpers ─────────────────────────────

def expand(p: str) -> Path:
    return Path(os.path.expanduser(p)).resolve()


def tty() -> bool:
    return sys.stdout.isatty()


def info(msg: str) -> None:
    if sys.stderr.isatty():
        print(msg, file=sys.stderr)


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text())
    except Exception:
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def load_env() -> dict:
    """KEY=VALUE lines from the first .env in ENV_FILES, overlaid by the real environment."""
    out = {}
    for f in ENV_FILES:
        if f.is_file():
            for line in f.read_text().splitlines():
                key, sep, val = line.strip().partition("=")
                if sep and not key.startswith("#"):
                    out[key.strip()] = val.strip().strip("'\"")
            break
    out.update({k: v for k, v in os.environ.items() if k.startswith("TIDY_")})
    return out


def load_config() -> dict:
    cfg = json.loads(json.dumps(DEFAULT_CONFIG))
    env = load_env()
    if env.get("TIDY_CORPORATE_TERMS"):  # one regex alternation, e.g. acme|globex\.?io
        cfg["corporate_terms"] = [env["TIDY_CORPORATE_TERMS"]]
    if env.get("TIDY_CORPORATE_NAME_TERMS"):
        cfg["corporate_name_terms"] = [env["TIDY_CORPORATE_NAME_TERMS"]]
    if env.get("TIDY_COMPANY_NAME"):
        cfg["company_name"] = env["TIDY_COMPANY_NAME"]
    env_clients = {}
    for k, v in env.items():
        if k.startswith("TIDY_CLIENT_"):
            name, _, rest = (x.strip() for x in v.partition("="))
            crx, _, desc = (x.strip() for x in rest.partition("="))
            if name:
                env_clients[name] = [crx or re.escape(name), desc]
    if env_clients:
        cfg["clients"] = env_clients
    user = load_json(CONFIG_PATH, {})
    cfg.update(user)
    cfg["_ext_to_cat"] = {e: c for c, exts in cfg["ext_map"].items() for e in exts}
    cfg["_image_exts"] = set(cfg["ext_map"].get("photos", []) + cfg["ext_map"].get("graphics", []))
    rx = lambda terms: re.compile("(" + "|".join(terms) + ")", re.I) if terms else None
    cfg["_clients"] = [(n, re.compile(v[0] if isinstance(v, list) else v, re.I),
                        v[1] if isinstance(v, list) and len(v) > 1 else "")
                       for n, v in cfg.get("clients", {}).items()]
    corp = cfg.get("corporate_terms", []) + [c.pattern for _, c, _ in cfg["_clients"]]
    cfg["_corp_rx"] = rx(corp)
    cfg["_corp_name_rx"] = rx(corp + cfg.get("corporate_name_terms", []))
    cfg["_personal_rx"] = rx(cfg.get("personal_name_terms", []))
    return cfg


def created_time(st) -> float:
    return getattr(st, "st_birthtime", None) or st.st_mtime


# ───────────────────────────── scanning ─────────────────────────────

def eligible(entry: os.DirEntry, settle_s: float) -> bool:
    name = entry.name
    if name.startswith((".", "~$")) or name in JUNK_NAMES:  # ~$ = Office owner/lock file
        return False
    if Path(name).suffix.lower() in PARTIAL_EXTS:
        return False
    try:
        st = entry.stat(follow_symlinks=False)
    except OSError:
        return False
    if not stat.S_ISREG(st.st_mode):          # skips folders, symlinks, .app bundles
        return False
    if getattr(st, "st_flags", 0) & SF_DATALESS:  # iCloud placeholder: reading would download it
        return False
    if time.time() - st.st_mtime < settle_s:  # still downloading / being written
        return False
    return True


def scan(cfg: dict) -> list:
    """Top-level loose files in each scan folder. Raises PermissionError if macOS blocks access."""
    dest = expand(cfg["dest"])
    settle = cfg["settle_minutes"] * 60
    out = []
    for root in cfg["scan"]:
        r = expand(root)
        if not r.is_dir() or r == dest or dest in r.parents:
            continue
        with os.scandir(r) as it:
            for e in it:
                if eligible(e, settle):
                    out.append(Path(e.path))
    return out


# ───────────────────────────── layer 0: duplicates ─────────────────────────────

def sha256(p: Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def find_duplicates(files: list) -> set:
    by_size = {}
    for p in files:
        try:
            s = p.stat().st_size
        except OSError:
            continue
        if s > 0:
            by_size.setdefault(s, []).append(p)
    dups = set()
    for group in by_size.values():
        if len(group) < 2:
            continue
        by_hash = {}
        for p in group:
            try:
                by_hash.setdefault(sha256(p), []).append(p)
            except OSError:
                pass
        for same in by_hash.values():
            if len(same) < 2:
                continue
            # keep the oldest; on ties prefer the shorter name (avoids keeping "file (1).pdf")
            same.sort(key=lambda q: (created_time(q.stat()), len(q.name)))
            dups.update(same[1:])
    return dups


# ───────────────────────────── layer 1: rules ─────────────────────────────

def has_camera(paths: list) -> dict:
    """Batch Spotlight lookup: does the image carry a camera model? Missing mdls => assume yes."""
    result = {p: True for p in paths}
    if not paths or not shutil.which("mdls"):
        return result
    try:
        r = subprocess.run(["mdls", "-raw", "-name", "kMDItemAcquisitionModel", *map(str, paths)],
                           capture_output=True, timeout=30)
        vals = r.stdout.decode("utf-8", "ignore").split("\0")
        for p, v in zip(paths, vals):
            result[p] = v.strip() not in ("", "(null)")
    except Exception:
        pass
    return result


def rule_pass(files: list, cfg: dict):
    decided, pending = {}, []
    sem_exts = set(cfg["semantic_exts"])
    hints = [(re.compile(rx, re.I), cat, scope) for rx, cat, scope in cfg["name_hints"]]
    for p in files:
        ext = p.suffix.lower().lstrip(".")
        name = match_name(p)
        hit = None
        for rx, cat, scope in hints:
            in_scope = (scope == "any" or (scope == "image" and ext in cfg["_image_exts"])
                        or (scope == "doc" and ext in sem_exts))
            if in_scope and rx.search(name):
                hit = cat
                break
        if hit:
            decided[p] = (hit, "name", None)
        elif ext in cfg["_ext_to_cat"]:
            decided[p] = (cfg["_ext_to_cat"][ext], "ext", None)
        elif ext in sem_exts:
            pending.append(p)
        else:
            decided[p] = ("other", "ext", None)

    check = set(cfg.get("camera_check_exts", []))
    photos = [p for p, (c, m, _) in decided.items() if c == "photos" and p.suffix.lower().lstrip(".") in check]
    for p, cam in has_camera(photos).items():
        if not cam:
            decided[p] = ("graphics", "metadata", None)
    return decided, pending


# ───────────────────────────── text extraction ─────────────────────────────

def strip_tags(s: str) -> str:
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    return html.unescape(re.sub(r"<[^>]+>", " ", s))


def ooxml_text(p: Path, ext: str) -> str:
    with zipfile.ZipFile(p) as z:
        names = set(z.namelist())
        if ext == "docx":
            members = ["word/document.xml"]
        elif ext == "xlsx":
            members = ["xl/sharedStrings.xml"]
        else:
            slides = [n for n in names if re.match(r"ppt/slides/slide\d+\.xml$", n)]
            members = sorted(slides, key=lambda n: int(re.findall(r"\d+", n)[-1]))[:3]
        parts = []
        for m in members:
            if m in names:
                with z.open(m) as f:
                    parts.append(strip_tags(f.read(300_000).decode("utf-8", "ignore")))
        return " ".join(parts)


JXA_PDF = (
    "function run(argv){ObjC.import('PDFKit');"
    "var d=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));"
    "if(d.isNil())return '';var s='';var n=Math.min(d.pageCount,2);"
    "for(var i=0;i<n;i++){var t=ObjC.unwrap(d.pageAtIndex(i).string);if(t)s+=t+' ';}"
    "return s.slice(0,4000);}"
)


def pdf_text(p: Path) -> str:
    try:
        from pypdf import PdfReader  # optional
        reader = PdfReader(str(p))
        txt = " ".join((pg.extract_text() or "") for pg in reader.pages[:2])
        if txt.strip():
            return txt
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            r = subprocess.run(["osascript", "-l", "JavaScript", "-e", JXA_PDF, str(p)],
                               capture_output=True, timeout=15)
            return r.stdout.decode("utf-8", "ignore")
        except Exception:
            pass
    return ""


def extract_text(p: Path, limit: int) -> str:
    ext = p.suffix.lower().lstrip(".")
    raw = ""
    try:
        if ext in ("txt", "md", "csv", "html", "htm"):
            with open(p, "rb") as f:
                raw = f.read(limit * 6).decode("utf-8", "ignore")
            if ext in ("html", "htm"):
                raw = strip_tags(raw)
        elif ext in ("docx", "pptx", "xlsx"):
            raw = ooxml_text(p, ext)
        elif ext == "pdf":
            raw = pdf_text(p)
        elif ext in ("doc", "rtf", "odt"):
            r = subprocess.run(["textutil", "-convert", "txt", "-stdout", str(p)],
                               capture_output=True, timeout=15)
            raw = r.stdout.decode("utf-8", "ignore")
    except Exception:
        raw = ""
    return re.sub(r"\s+", " ", raw).strip()[:limit]


def match_name(p: Path) -> str:
    """Filename for rule matching: separators -> spaces, so \\b works on 'JR_CV_2026.pdf'."""
    return re.sub(r"[_\-.]+", " ", p.name.lower())


def clean_name(p: Path) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-.()\[\]]+", " ", p.stem)).strip()


def informative(p: Path, snippet: str) -> bool:
    if snippet:
        return True
    words = [w.lower() for w in re.findall(r"[A-Za-z]{3,}", p.stem)]
    return any(w not in GENERIC_WORDS for w in words)


# ───────────────────────────── Ollama client ─────────────────────────────

class Ollama:
    CANDIDATE_BINS = ["/opt/homebrew/bin/ollama", "/usr/local/bin/ollama",
                      "/Applications/Ollama.app/Contents/Resources/ollama"]

    def __init__(self, cfg: dict):
        self.url = cfg["ollama_url"].rstrip("/")
        self.proc = None
        self.used = set()
        self.seconds = 0.0

    def _req(self, path: str, payload=None, timeout: float = 300):
        data = json.dumps(payload).encode() if payload is not None else None
        req = urllib.request.Request(self.url + path, data=data,
                                     headers={"Content-Type": "application/json"})
        t = time.time()
        try:
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read()
        finally:
            self.seconds += time.time() - t
        return json.loads(body or b"{}")

    def up(self) -> bool:
        try:
            self._req("/api/version", timeout=2)
            return True
        except Exception:
            return False

    @classmethod
    def binary(cls):
        return shutil.which("ollama") or next((b for b in cls.CANDIDATE_BINS if os.path.exists(b)), None)

    def ensure(self) -> bool:
        """Use a running server, or start one just for this run (stopped in close())."""
        if self.up():
            return True
        exe = self.binary()
        if not exe:
            return False
        self.proc = subprocess.Popen([exe, "serve"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(60):
            time.sleep(0.25)
            if self.up():
                return True
        return False

    def models(self) -> set:
        try:
            return {m["name"] for m in self._req("/api/tags", timeout=5).get("models", [])}
        except Exception:
            return set()

    def embed(self, model: str, texts: list) -> list:
        self.used.add(model)
        out = []
        for i in range(0, len(texts), 32):
            r = self._req("/api/embed", {"model": model, "input": texts[i:i + 32],
                                         "keep_alive": "30s", "truncate": True})
            out.extend(r["embeddings"])
        return out

    def ask(self, model: str, prompt: str, fields: dict) -> dict:
        """fields: {"c": 3, "s": 2} -> each answer is an int 0..n (0 = unsure).
        Output is grammar-constrained JSON, ~5-10 tokens."""
        self.used.add(model)
        schema = {"type": "object",
                  "properties": {k: {"type": "integer", "enum": list(range(n + 1))} for k, n in fields.items()},
                  "required": list(fields)}
        r = self._req("/api/generate", {
            "model": model, "prompt": prompt, "stream": False, "format": schema,
            "keep_alive": "30s",
            "options": {"temperature": 0, "num_predict": 20, "num_ctx": 1024},
        })
        try:
            ans = json.loads(r.get("response", "{}"))
        except Exception:
            ans = {}
        out = {}
        for k, n in fields.items():
            try:
                v = int(ans.get(k, 0))
            except Exception:
                v = 0
            out[k] = v if 0 <= v <= n else 0
        return out

    def close(self) -> None:
        for m in self.used:  # best-effort unload; a server we started is stopped anyway
            try:
                self._req("/api/generate", {"model": m, "keep_alive": 0}, timeout=10)
            except Exception:
                pass
        if self.proc:
            self.proc.terminate()
            try:
                self.proc.wait(timeout=5)
            except Exception:
                self.proc.kill()


def model_present(name: str, have: set) -> bool:
    return (name if ":" in name else name + ":latest") in have


def cosine(a: list, b: list) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def reference_vectors(ol: Ollama, cfg: dict) -> dict:
    """Embeddings of the category and scope descriptions, cached until the wording changes."""
    descs = {f"cat:{c}": d for c, d in cfg["semantic"].items()}
    descs.update({f"scope:{s}": d for s, d in cfg["scope_desc"].items()})
    key = hashlib.sha1((cfg["embed_model"] + json.dumps(descs, sort_keys=True)).encode()).hexdigest()
    cache = load_json(EMB_CACHE, {})
    if cache.get("key") == key:
        return cache["vectors"]
    keys = list(descs)
    out = dict(zip(keys, ol.embed(cfg["embed_model"], [descs[k] for k in keys])))
    save_json(EMB_CACHE, {"key": key, "vectors": out})
    return out


SCOPES = ["corporate", "personal"]  # LLM answer 1 = corporate, 2 = personal


def llm_prompt(p: Path, snippet: str, cfg: dict, cands=None, scope: bool = False) -> str:
    keys, parts = [], []
    company = cfg.get("company_name") or "the company"
    if cands:
        keys.append('"c"')
        parts.append("c = folder number (0 = none fit):\n"
                     + "\n".join(f"{i} {cfg['semantic'][c]}" for i, c in enumerate(cands, 1)))
    if scope:
        keys.append('"s"')
        parts.append(f"s = 1 if about {company}'s business, 2 if about your own private life, 0 if unsure.\n"
                     f"Company business: {cfg['scope_desc']['corporate']}\n"
                     f"Personal: {cfg['scope_desc']['personal']}")
    ceo = f"You are the CEO of {cfg['company_name']}, organizing your files. " if cfg.get("company_name") else ""
    return (f"{ceo}Classify this file. Answer JSON with {' and '.join(keys)}.\n" + "\n".join(parts)
            + f"\nFile: {p.name}\nText: {snippet[:cfg['snippet_chars']] or '(none)'}")


def scope_by_rules(p: Path, snippet: str, cat, cfg: dict):
    name = match_name(p)
    if cfg["_personal_rx"] and cfg["_personal_rx"].search(name):
        return "personal", "name"
    if cfg["_corp_name_rx"] and cfg["_corp_name_rx"].search(name):
        return "corporate", "name"
    # Content mentions are weaker: your resume mentions your company, so career docs don't count.
    if cat not in (None, "career") and snippet and cfg["_corp_rx"] and cfg["_corp_rx"].search(snippet):
        return "corporate", "content"
    return None, None


def client_by_rules(p: Path, snippet: str, cfg: dict):
    """The client named earliest in the filename, else earliest in the content."""
    def earliest(text):
        hits = [(m.start(), n) for n, rx, _ in cfg["_clients"] for m in [rx.search(text)] if m]
        return min(hits)[1] if hits else None
    name = earliest(match_name(p))
    if name:
        return name, "name"
    name = earliest(snippet) if snippet else None
    return (name, "content") if name else (None, None)


# ───────────────────────────── layers 2 + 3: semantic ─────────────────────────────

def semantic_pass(pending: list, known: dict, cfg: dict, stats: dict):
    """pending: documents needing a category. known: {path: category} already set by rules,
    needing only Corporate/Personal. Returns (categories, scopes, clients).
    categories[p] = (cat|None, method, score); scopes[p] = (scope|None, method);
    clients[p] = (client|None, method) for Corporate documents when clients are configured
    (None = the internal folder). Clients come from name rules only: a 1.5B model asked to pick
    one mostly guessed, and the extra question made its category answers worse."""
    cats, scopes, clients = {}, {}, {}
    if not pending and not known:
        return cats, scopes, clients
    allp = pending + list(known)
    info(f"Reading {len(allp)} document(s)…")
    snippets = {p: extract_text(p, cfg["snippet_chars"]) for p in allp}

    todo = []
    for p in pending:
        if informative(p, snippets[p]):
            todo.append(p)
        else:
            cats[p] = ("review", "no-signal", None)  # e.g. "document(3).pdf" with no text: don't guess
    for p, c in known.items():
        s_, m = scope_by_rules(p, snippets[p], c, cfg)
        if s_:
            scopes[p] = (s_, m)
    unscoped_known = [p for p in known if p not in scopes]

    def finish():
        if cfg["_clients"]:
            for p in allp:
                cat = known.get(p) or cats.get(p, (None,))[0]
                if cat in cfg["semantic"] and scopes.get(p, (None,))[0] == "corporate":
                    c, m = client_by_rules(p, snippets[p], cfg)
                    clients[p] = (c, m) if c else (None, "no-client")
        return cats, scopes, clients

    if not todo and not unscoped_known:
        return finish()

    ol = Ollama(cfg)
    try:
        if not ol.ensure():
            info("Ollama isn't available — leaving documents that need it in place. Run `tidy doctor`.")
            for p in todo:
                cats[p] = (None, "no-model", None)
            for p in unscoped_known:
                scopes[p] = (None, "no-model")
            return finish()
        have = ol.models()
        emb_ok = model_present(cfg["embed_model"], have)
        llm_ok = model_present(cfg["llm_model"], have)
        if not emb_ok or not llm_ok:
            missing = [m for m, ok in ((cfg["embed_model"], emb_ok), (cfg["llm_model"], llm_ok)) if not ok]
            info(f"Missing model(s): {missing} — run `tidy doctor`.")

        sem_cats = list(cfg["semantic"])
        vecs, refs = {}, {}
        if emb_ok:
            try:
                info("Embedding…")
                refs = reference_vectors(ol, cfg)
                need = todo + unscoped_known
                texts = [f"File name: {clean_name(p)}. Content: {snippets[p]}" for p in need]
                vecs = dict(zip(need, ol.embed(cfg["embed_model"], texts)))
            except Exception as e:
                info(f"Embedding failed ({e}); falling back to the small model only.")
                vecs, refs = {}, {}

        def ask(p, cands=None, scope=False):
            fields = {}
            if cands:
                fields["c"] = len(cands)
            if scope:
                fields["s"] = 2
            try:
                return ol.ask(cfg["llm_model"], llm_prompt(p, snippets[p], cfg, cands, scope), fields)
            except Exception:
                return {k: 0 for k in fields}

        k = max(2, int(cfg["llm_candidates"]))
        asked = 0
        # ── category for pending documents (scope asked in the same call when needed) ──
        for p in todo:
            cands = sem_cats
            if p in vecs:
                ranked = sorted(((cosine(vecs[p], refs[f"cat:{c}"]), c) for c in sem_cats), reverse=True)
                (s1, c1), (s2, _) = ranked[0], ranked[1]
                if s1 >= cfg["embed_accept"] and s1 - s2 >= cfg["embed_margin"]:
                    cats[p] = (c1, "embed", round(s1, 3))
                    continue
                cands = [c for _, c in ranked[:k]]
            if not llm_ok:
                cats[p] = ("review", "low-confidence", None)
                continue
            s_, m = scope_by_rules(p, snippets[p], None, cfg)
            if s_:
                scopes[p] = (s_, m)
            if asked == 0:
                info("Asking the small model about uncertain files…")
            asked += 1
            ans = ask(p, cands, scope=not s_)
            cats[p] = (cands[ans["c"] - 1], "llm", None) if ans["c"] else ("review", "llm-unsure", None)
            if not s_:
                # A rule hit (e.g. a client name in the content) beats the small model's guess.
                rs, rm = scope_by_rules(p, snippets[p], cats[p][0], cfg)
                if rs:
                    scopes[p] = (rs, rm)
                elif "s" in ans:
                    scopes[p] = (SCOPES[ans["s"] - 1], "llm") if ans["s"] else (None, "llm-unsure")

        # ── Corporate / Personal for everything categorised but not yet scoped ──
        for p in todo + unscoped_known:
            cat = known.get(p) or cats.get(p, (None,))[0]
            if cat not in cfg["semantic"] or p in scopes:
                continue
            s_, m = scope_by_rules(p, snippets[p], cat, cfg)
            if s_:
                scopes[p] = (s_, m)
                continue
            if p in vecs:
                sc = cosine(vecs[p], refs["scope:corporate"])
                sp = cosine(vecs[p], refs["scope:personal"])
                if abs(sc - sp) >= cfg["scope_margin"]:
                    scopes[p] = ("corporate" if sc > sp else "personal", "embed")
                    continue
            if llm_ok:
                ans = ask(p, scope=True)
                scopes[p] = (SCOPES[ans["s"] - 1], "llm") if ans["s"] else (None, "llm-unsure")
            else:
                scopes[p] = (None, "low-confidence")
    finally:
        ol.close()
        stats["model_seconds"] = round(ol.seconds, 1)
    return finish()


# ───────────────────────────── plan / apply / undo ─────────────────────────────

def destination(p: Path, cat: str, scope, cfg: dict, client=None) -> Path:
    folder = cfg["folders"].get(cat, cfg["folders"]["other"])
    if "{scope}" in folder:
        if scope:
            label = scope.capitalize()
            if scope == "corporate" and cfg.get("_clients"):
                label += "/" + (client or cfg["internal_folder"])
            folder = folder.replace("{scope}", label)
        else:
            folder = cfg["folders"]["unclear_scope"] + "/" + folder.split("/")[-1]
    d = expand(folder) if folder.startswith(("~", "/")) else expand(cfg["dest"]) / folder
    if cat in cfg.get("year_subfolders", []):
        d = d / str(datetime.fromtimestamp(created_time(p.stat())).year)
    return d / p.name


def build_plan(cfg: dict) -> dict:
    t0 = time.time()
    files = scan(cfg)
    info(f"Found {len(files)} loose file(s).")
    stats = {"left_in_place": 0}
    dups = find_duplicates(files)
    decided = {p: ("duplicates", "hash", None) for p in dups}
    rule_decided, pending = rule_pass([p for p in files if p not in dups], cfg)
    decided.update(rule_decided)
    known = {p: c for p, (c, _, _) in rule_decided.items() if c in cfg["semantic"]}
    cats, scopes, clients = semantic_pass(pending, known, cfg, stats)
    decided.update(cats)

    items = []
    for p, (cat, method, score) in sorted(decided.items(), key=lambda kv: str(kv[0])):
        scope, smethod = scopes.get(p, (None, None)) if cat in cfg["semantic"] else (None, None)
        client, cmethod = clients.get(p, (None, None)) if scope == "corporate" else (None, None)
        if cat is None or smethod == "no-model":
            stats["left_in_place"] += 1
            continue
        try:
            items.append({"src": str(p), "dst": str(destination(p, cat, scope, cfg, client)), "cat": cat,
                          "method": method, "score": score, "scope": scope, "scope_method": smethod,
                          "client": client, "client_method": cmethod, "mtime": p.stat().st_mtime})
        except OSError:
            pass
    stats["seconds"] = round(time.time() - t0, 1)
    return {"created": time.time(), "dest": str(expand(cfg["dest"])), "items": items, "stats": stats}


def print_plan(plan: dict, cfg: dict, verbose: bool) -> None:
    items = plan["items"]
    s = plan["stats"]
    left = f"{s['left_in_place']} document(s) left in place: model unavailable." if s.get("left_in_place") else ""
    if not items:
        print("Nothing to organize. " + left)
        return
    home = str(Path.home())
    short = lambda x: x.replace(home, "~", 1)
    dest = plan["dest"]
    print(f"Plan: {len(items)} file(s) → {short(dest)}\n")
    by_folder = {}
    for it in items:
        folder = str(Path(it["dst"]).parent)
        if folder.startswith(dest + "/"):
            folder = folder[len(dest) + 1:]
        folder = re.sub(r"/\d{4}$", "/<year>", short(folder))
        by_folder.setdefault(folder, []).append(it)
    for folder in sorted(by_folder):
        group = by_folder[folder]
        methods = {}
        for it in group:
            key = it["method"] + "".join(f"+{it[k]}" for k in ("scope_method", "client_method") if it.get(k))
            methods[key] = methods.get(key, 0) + 1
        m = ", ".join(f"{k} {v}" for k, v in sorted(methods.items()))
        print(f"  {folder:<40} {len(group):>4}   ({m})")
        if verbose:
            for it in group:
                sc = f" {it['score']}" if it["score"] is not None else ""
                print(f"      {short(it['src'])}  [{it['method']}{sc}]")
    if left:
        print("\n" + left)
    print("\nMethod key: category method + scope method + client method (name, content, embed, llm).")
    print(f"Planned in {s.get('seconds', 0)}s (model time {s.get('model_seconds', 0)}s). Nothing has moved yet.")


def unique_path(p: Path) -> Path:
    if not p.exists():
        return p
    i = 2
    while True:
        q = p.with_name(f"{p.stem} ({i}){p.suffix}")
        if not q.exists():
            return q
        i += 1


def apply_plan(plan: dict) -> None:
    if time.time() - plan.get("created", 0) > PLAN_MAX_AGE:
        print("Plan is older than 24h — run `tidy plan` again.")
        return
    RUNS_DIR.mkdir(parents=True, exist_ok=True)
    run_file = RUNS_DIR / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    manifest = {"started": time.time(), "moves": [], "created_dirs": [], "undone": False}
    skipped = errors = 0
    try:
        for it in plan["items"]:
            src = Path(it["src"])
            try:
                if not src.exists() or abs(src.stat().st_mtime - it["mtime"]) > 1:
                    skipped += 1  # gone or changed since planning
                    continue
                dst = unique_path(Path(it["dst"]))
                missing, d = [], dst.parent
                while not d.exists():
                    missing.append(str(d))
                    d = d.parent
                dst.parent.mkdir(parents=True, exist_ok=True)
                manifest["created_dirs"].extend(missing)
                os.rename(src, dst)  # same-volume rename only: atomic and cleanly undoable
                manifest["moves"].append([str(src), str(dst)])
            except OSError as e:
                errors += 1
                info(f"  ! {src.name}: {e.strerror or e}")
    finally:
        save_json(run_file, manifest)
        PLAN_PATH.unlink(missing_ok=True)
    print(f"Moved {len(manifest['moves'])} file(s). Skipped {skipped}, errors {errors}. "
          f"Undo with `tidy undo`.")


def undo_last() -> None:
    runs = sorted(RUNS_DIR.glob("*.json"), reverse=True)
    for rf in runs:
        m = load_json(rf, {})
        if m.get("moves") and not m.get("undone"):
            break
    else:
        print("Nothing to undo.")
        return
    restored = skipped = 0
    for src, dst in reversed(m["moves"]):
        s, d = Path(src), Path(dst)
        if d.exists() and not s.exists():
            s.parent.mkdir(parents=True, exist_ok=True)
            os.rename(d, s)
            restored += 1
        else:
            skipped += 1
    # Remove only folders this run created, and only if now empty. Your own folders in
    # Documents are never touched, even if they end up empty.
    for d in sorted(set(m.get("created_dirs", [])), key=lambda x: len(Path(x).parts), reverse=True):
        try:
            Path(d).rmdir()
        except OSError:
            pass
    m["undone"] = True
    save_json(rf, m)
    print(f"Restored {restored} file(s) from run {rf.stem}. Skipped {skipped}.")


# ───────────────────────────── watching ─────────────────────────────

def notify(title: str, msg: str) -> None:
    tn = shutil.which("terminal-notifier") or next(
        (b for b in ("/opt/homebrew/bin/terminal-notifier", "/usr/local/bin/terminal-notifier")
         if os.path.exists(b)), None)
    try:
        if tn and RUN_COMMAND.exists():
            subprocess.run([tn, "-title", title, "-message", msg, "-group", "tidy",
                            "-execute", f"open '{RUN_COMMAND}'"], timeout=10)
        else:
            subprocess.run(["osascript", "-e",
                            f"display notification {json.dumps(msg)} with title {json.dumps(title)}"],
                           timeout=10)
    except Exception:
        pass


def watch_check(cfg: dict) -> None:
    """Called by launchd when a watched folder changes. No models, just a directory listing."""
    state = load_json(STATE_PATH, {})
    try:
        files = scan(cfg)
    except PermissionError:
        if not state.get("perm_warned"):
            notify("Tidy can't read your folders",
                   "Grant Full Disk Access to Python. Run `tidy doctor` for the path.")
            state["perm_warned"] = True
            save_json(STATE_PATH, state)
        return
    pending = {str(p) for p in files}
    new = pending - set(state.get("notified", []))
    cooled = time.time() - state.get("last", 0) >= cfg["notify_cooldown_minutes"] * 60
    if len(pending) >= cfg["notify_threshold"] and new and cooled:
        where = sorted({Path(p).parent.name for p in pending})
        notify("Tidy", f"{len(pending)} files waiting in {', '.join(where)}. Run `tidy` to organize.")
        state.update(last=time.time(), notified=sorted(pending), perm_warned=False)
    save_json(STATE_PATH, state)


def python_tcc_paths() -> list:
    paths = [os.path.realpath(sys.executable)]
    app = Path(sys.base_prefix) / "Resources/Python.app"
    if app.exists():
        paths.append(str(app))
    return paths


def install_watch(cfg: dict) -> None:
    APP_DIR.mkdir(parents=True, exist_ok=True)
    script = Path(__file__).resolve()
    RUN_COMMAND.write_text(f'#!/bin/bash\n"{sys.executable}" "{script}"\n'
                           'echo; read -n 1 -s -r -p "Press any key to close"\n')
    RUN_COMMAND.chmod(0o755)
    plist = {
        "Label": AGENT_LABEL,
        "ProgramArguments": [sys.executable, str(script), "watch-check"],
        "WatchPaths": [str(expand(p)) for p in cfg["scan"]],
        "ThrottleInterval": 60,
        "ProcessType": "Background",
        "LowPriorityIO": True,
        "Nice": 10,
        "StandardErrorPath": str(APP_DIR / "watch.log"),
        "EnvironmentVariables": {"TIDY_HOME": str(APP_DIR)},
    }
    AGENT_PLIST.parent.mkdir(parents=True, exist_ok=True)
    with open(AGENT_PLIST, "wb") as f:
        plistlib.dump(plist, f)
    domain = f"gui/{os.getuid()}"
    subprocess.run(["launchctl", "bootout", domain, str(AGENT_PLIST)], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", domain, str(AGENT_PLIST)], capture_output=True, text=True)
    if r.returncode != 0:
        print(f"launchctl bootstrap failed: {r.stderr.strip()}")
        return
    print("Watching:", ", ".join(plist["WatchPaths"]))
    print("\nOne manual step: macOS blocks background scripts from reading Downloads/Desktop.")
    print("System Settings → Privacy & Security → Full Disk Access → + → add (⌘⇧G to paste a path):")
    for p in python_tcc_paths():
        print("   ", p)
    print("\nOptional: `brew install terminal-notifier` makes the notification clickable (opens Tidy).")


def uninstall_watch() -> None:
    subprocess.run(["launchctl", "bootout", f"gui/{os.getuid()}", str(AGENT_PLIST)], capture_output=True)
    AGENT_PLIST.unlink(missing_ok=True)
    print("Watcher removed.")


# ───────────────────────────── doctor / init ─────────────────────────────

def doctor(cfg: dict) -> None:
    def sh(cmd):
        try:
            return subprocess.run(cmd, capture_output=True, text=True, timeout=5).stdout.strip()
        except Exception:
            return ""
    mem = sh(["sysctl", "-n", "hw.memsize"])
    gb = int(mem) // 2**30 if mem.isdigit() else None
    print(f"Mac:     {sh(['uname', '-m']) or '?'}, {gb or '?'} GB RAM")
    if gb:
        rec = "qwen2.5:0.5b" if gb <= 8 else "qwen2.5:1.5b"
        print(f"         suggested LLM for this RAM: {rec} (configured: {cfg['llm_model']})")
    print(f"Python:  {sys.version.split()[0]}  ({sys.executable})")
    try:
        import pypdf  # noqa
        print("PDF:     pypdf installed")
    except Exception:
        print("PDF:     pypdf missing — using macOS PDFKit fallback")
    exe = Ollama.binary()
    print(f"Ollama:  {exe or 'NOT INSTALLED (brew install ollama)'}")
    ol = Ollama(cfg)
    started = False
    if exe and not ol.up():
        started = ol.ensure()
    if ol.up():
        have = ol.models()
        for m in (cfg["embed_model"], cfg["llm_model"]):
            print(f"         {m}: {'ok' if model_present(m, have) else 'MISSING → ollama pull ' + m}")
    if started:
        ol.close()
    for root in cfg["scan"]:
        r = expand(root)
        try:
            os.listdir(r)
            print(f"Access:  {r} ok")
        except PermissionError:
            print(f"Access:  {r} DENIED (grant Full Disk Access)")
        except FileNotFoundError:
            print(f"Access:  {r} not found")
    print(f"Watcher: {'installed' if AGENT_PLIST.exists() else 'not installed (tidy install-watch)'}")
    print("For background watching, Full Disk Access must include:")
    for p in python_tcc_paths():
        print("   ", p)


def init(args) -> None:
    user = load_json(CONFIG_PATH, {})
    if not user:
        # Seed the keys people actually edit; everything else stays in DEFAULT_CONFIG.
        # corporate_terms / corporate_name_terms are left out so they don't shadow .env.
        user = {k: DEFAULT_CONFIG[k] for k in ("scan", "dest", "embed_model", "llm_model",
                                               "personal_name_terms",
                                               "embed_accept", "embed_margin", "scope_margin",
                                               "notify_threshold")}
    for key in ("llm", "embed", "dest"):
        v = getattr(args, key, None)
        if v:
            user[{"llm": "llm_model", "embed": "embed_model", "dest": "dest"}[key]] = v
    if args.scan:
        user["scan"] = args.scan
    save_json(CONFIG_PATH, user)
    print(f"Config: {CONFIG_PATH}")


# ───────────────────────────── CLI ─────────────────────────────

def main() -> None:
    ap = argparse.ArgumentParser(prog="tidy", description="Local, low-RAM file organizer for macOS.")
    ap.add_argument("-v", "--verbose", action="store_true", help="list every file in the plan")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("plan", help="dry run: show where files would go").add_argument("-v", "--verbose", action="store_true")
    sub.add_parser("apply", help="execute the last plan")
    sub.add_parser("undo", help="reverse the last applied run")
    sub.add_parser("doctor", help="check models, permissions, watcher")
    sub.add_parser("install-watch", help="notify when files pile up (launchd, no resident process)")
    sub.add_parser("uninstall-watch")
    sub.add_parser("watch-check", help="(used by the watcher)")
    p_init = sub.add_parser("init", help="write config")
    p_init.add_argument("--llm")
    p_init.add_argument("--embed")
    p_init.add_argument("--dest")
    p_init.add_argument("--scan", action="append")
    args = ap.parse_args()
    cfg = load_config()
    cmd = args.cmd or "run"

    if cmd in ("run", "plan"):
        try:
            plan = build_plan(cfg)
        except PermissionError as e:
            print(f"macOS denied access to {e.filename}. Allow it for your terminal and retry.")
            sys.exit(1)
        save_json(PLAN_PATH, plan)
        print_plan(plan, cfg, args.verbose)
        if cmd == "run" and plan["items"] and sys.stdin.isatty():
            if input("\nApply this plan? [y/N] ").strip().lower() == "y":
                apply_plan(plan)
            else:
                print("Not applied. `tidy apply` will run this plan later; `tidy -v` shows each file.")
    elif cmd == "apply":
        plan = load_json(PLAN_PATH, None)
        if not plan:
            print("No plan yet — run `tidy plan` first.")
            return
        apply_plan(plan)
    elif cmd == "undo":
        undo_last()
    elif cmd == "doctor":
        doctor(cfg)
    elif cmd == "install-watch":
        install_watch(cfg)
    elif cmd == "uninstall-watch":
        uninstall_watch()
    elif cmd == "watch-check":
        watch_check(cfg)
    elif cmd == "init":
        init(args)


if __name__ == "__main__":
    main()

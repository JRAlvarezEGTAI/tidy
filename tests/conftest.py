"""Shared fixtures: an isolated tidy home, scan folders, and a fake Ollama server.

The fake server speaks just enough of Ollama's API (/api/version, /api/tags, /api/embed,
/api/generate) to exercise every layer without downloading a model. Embeddings are a
hashed bag-of-words, so texts sharing words are similar. That's crude, but deterministic.
"""
import hashlib
import json
import math
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import tidy  # noqa: E402

OLD = time.time() - 3600  # older than the settle window


def fake_vec(text: str) -> list:
    v = [0.0] * 256
    for w in re.findall(r"[a-z]{3,}", text.lower()):
        v[int(hashlib.md5(w[:6].encode()).hexdigest(), 16) % 256] += 1
    n = math.sqrt(sum(x * x for x in v)) or 1
    return [x / n for x in v]


class FakeOllama:
    """Answers: c=1 (top candidate) unless the file text contains 'zzqx' (then 0 = unsure);
    s=2 (personal) if the text mentions bank/mom/family/grocery, else 1 (corporate)."""

    def __init__(self):
        self.generate_calls = []
        self.models = ["all-minilm:latest", "qwen2.5:1.5b"]
        fake = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, obj):
                b = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/version":
                    self._send({"version": "fake"})
                elif self.path == "/api/tags":
                    self._send({"models": [{"name": m} for m in fake.models]})

            def do_POST(self):
                d = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/api/embed":
                    inp = d["input"] if isinstance(d["input"], list) else [d["input"]]
                    self._send({"embeddings": [fake_vec(t) for t in inp]})
                elif self.path == "/api/generate":
                    if "prompt" not in d:  # unload request
                        return self._send({})
                    fake.generate_calls.append(d)
                    body = d["prompt"].split("\nFile:")[-1].lower()
                    props = d["format"]["properties"]
                    ans = {}
                    if "c" in props:
                        ans["c"] = 0 if "zzqx" in body else 1
                    if "s" in props:
                        personal = any(w in body for w in ("bank", "mom", "family", "grocery"))
                        ans["s"] = 0 if "zzqx" in body else (2 if personal else 1)
                    self._send({"response": json.dumps(ans)})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def ollama():
    f = FakeOllama()
    yield f
    f.close()


@pytest.fixture
def env(tmp_path, monkeypatch):
    """Isolated app dir + Downloads/Desktop/Documents under tmp_path. Returns a helper."""
    app = tmp_path / "app"
    for name, rel in [("APP_DIR", ""), ("CONFIG_PATH", "config.json"), ("RUNS_DIR", "runs"),
                      ("PLAN_PATH", "last_plan.json"), ("EMB_CACHE", "category_vectors.json"),
                      ("STATE_PATH", "watch_state.json"), ("RUN_COMMAND", "Run Tidy.command")]:
        monkeypatch.setattr(tidy, name, app / rel if rel else app)
    # Never read the developer's real .env; tests use fictional client names.
    monkeypatch.setattr(tidy, "ENV_FILES", [app / ".env"])
    monkeypatch.delenv("TIDY_CORPORATE_TERMS", raising=False)
    monkeypatch.delenv("TIDY_CORPORATE_NAME_TERMS", raising=False)
    # Don't call Spotlight in tests; treat every photo as camera-made.
    monkeypatch.setattr(tidy, "has_camera", lambda paths: {p: True for p in paths})
    # Never launch a real `ollama serve`; "server down" must stay down.
    monkeypatch.setattr(tidy.Ollama, "binary", classmethod(lambda cls: None))
    # Never pop real notifications.
    sent = []
    monkeypatch.setattr(tidy, "notify", lambda title, msg: sent.append((title, msg)))

    class Env:
        root = tmp_path
        downloads = tmp_path / "Downloads"
        desktop = tmp_path / "Desktop"
        documents = tmp_path / "Documents"
        notifications = sent

        def config(self, **over):
            cfg = {"scan": [str(self.downloads), str(self.desktop)], "dest": str(self.documents),
                   "ollama_url": "http://127.0.0.1:1", "embed_accept": 0.25, "embed_margin": 0.03,
                   "corporate_terms": ["acme", "globex"], "corporate_name_terms": ["employee"]}
            cfg.update(over)
            app.mkdir(parents=True, exist_ok=True)
            (app / "config.json").write_text(json.dumps(cfg))
            return tidy.load_config()

        def file(self, name, content="x", folder=None, age=OLD):
            p = (folder or self.downloads) / name
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_bytes(content.encode() if isinstance(content, str) else content)
            os.utime(p, (age, age))
            return p

    for d in (Env.downloads, Env.desktop, Env.documents):
        d.mkdir()
    return Env()


def placed(plan) -> dict:
    """{filename: path relative to dest} for easy assertions."""
    dest = Path(plan["dest"])
    return {Path(it["src"]).name: str(Path(it["dst"]).relative_to(dest).parent) for it in plan["items"]}

#!/bin/bash
# Installs tidy into ~/.tidy, picks models sized to this Mac's RAM, pulls them via Ollama.
#   bash install.sh         copy tidy.py into ~/.tidy
#   bash install.sh --dev   symlink ~/.tidy/tidy.py to this repo (edits take effect immediately)
set -euo pipefail
DEV=""; [[ "${1:-}" == "--dev" ]] && DEV=1

HERE="$(cd "$(dirname "$0")" && pwd)"
TIDY_HOME="$HOME/.tidy"

[[ "$(uname)" == "Darwin" ]] || { echo "macOS only."; exit 1; }

MEM_GB=$(( $(sysctl -n hw.memsize) / 1073741824 ))
ARCH=$(uname -m)
if (( MEM_GB <= 8 )); then LLM="qwen2.5:0.5b"; else LLM="qwen2.5:1.5b"; fi
EMBED="all-minilm"
echo "Mac: $ARCH, ${MEM_GB} GB RAM → embed=$EMBED, llm=$LLM"

# ── Python ──
PY="$(command -v python3 || true)"
if [[ -z "$PY" ]]; then echo "python3 not found. Run: xcode-select --install"; exit 1; fi
"$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 9) else 1)' \
  || { echo "Python 3.9+ required."; exit 1; }

mkdir -p "$TIDY_HOME/bin"
"$PY" -m venv "$TIDY_HOME/venv"
if [[ -n "$DEV" ]]; then
  "$TIDY_HOME/venv/bin/pip" install -q --upgrade pip pypdf pytest
  ln -sf "$HERE/tidy.py" "$TIDY_HOME/tidy.py"
else
  "$TIDY_HOME/venv/bin/pip" install -q --upgrade pip pypdf
  rm -f "$TIDY_HOME/tidy.py"; cp "$HERE/tidy.py" "$TIDY_HOME/tidy.py"
fi
cat > "$TIDY_HOME/bin/tidy" <<EOF
#!/bin/bash
exec "$TIDY_HOME/venv/bin/python" "$TIDY_HOME/tidy.py" "\$@"
EOF
chmod +x "$TIDY_HOME/bin/tidy"
# Private client names: a copied install can't see the repo's .env, so bring it along.
if [[ -z "$DEV" && -f "$HERE/.env" ]]; then cp "$HERE/.env" "$TIDY_HOME/.env"; chmod 600 "$TIDY_HOME/.env"; fi

# ── Ollama (binary only; tidy starts/stops the server per run) ──
OLLAMA="$(command -v ollama || true)"
[[ -z "$OLLAMA" && -x /Applications/Ollama.app/Contents/Resources/ollama ]] \
  && OLLAMA=/Applications/Ollama.app/Contents/Resources/ollama
if [[ -z "$OLLAMA" ]]; then
  if command -v brew >/dev/null; then
    brew install ollama
    OLLAMA="$(command -v ollama)"
  else
    echo "Install Ollama (https://ollama.com/download) or Homebrew, then re-run."; exit 1
  fi
fi

STARTED=""
if ! curl -sf http://127.0.0.1:11434/api/version >/dev/null; then
  "$OLLAMA" serve >/dev/null 2>&1 &
  STARTED=$!
  for _ in $(seq 1 60); do curl -sf http://127.0.0.1:11434/api/version >/dev/null && break; sleep 0.25; done
fi
"$OLLAMA" pull "$EMBED"
"$OLLAMA" pull "$LLM"
[[ -n "$STARTED" ]] && kill "$STARTED" 2>/dev/null || true

if [[ -f "$TIDY_HOME/config.json" ]]; then
  echo "Keeping existing config: $TIDY_HOME/config.json"
else
  "$TIDY_HOME/bin/tidy" init --llm "$LLM" --embed "$EMBED"
fi

# ── PATH ──
if ! grep -q '.tidy/bin' "$HOME/.zshrc" 2>/dev/null; then
  echo 'export PATH="$HOME/.tidy/bin:$PATH"' >> "$HOME/.zshrc"
fi

cat <<'MSG'

Installed. Open a new terminal, then:
  tidy doctor          # verify models + permissions
  tidy plan -v         # dry run, every file listed
  tidy                 # plan, then asks before moving anything
  tidy undo            # reverse the last run
  tidy install-watch   # notifications when files pile up
MSG

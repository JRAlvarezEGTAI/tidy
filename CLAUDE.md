# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## tidy

A local file organizer for macOS. It moves loose files from `~/Downloads` and `~/Desktop` into `~/Documents`, sorted by category and by **Corporate vs Personal**. It is designed to use as little RAM as possible and to emit as few model tokens as possible.

## Commands

```bash
python3 -m pytest -q                 # full suite, ~3s, needs no models (uses a fake Ollama)
python3 -m pytest -q tests/test_tidy.py::test_apply_and_undo_round_trip   # single test
python3 -m pytest -q -k scope        # tests matching a keyword
python3 tidy.py plan -v              # dry run against the real config (~/.tidy/config.json)
python3 tidy.py doctor               # models, permissions, watcher status
bash install.sh --dev                # install, symlinking ~/.tidy/tidy.py to this repo
```

pytest isn't a runtime dep. `install.sh --dev` puts it in `~/.tidy/venv` (use `~/.tidy/venv/bin/python -m pytest`), or `pip install -e '.[dev]'`.

`TIDY_HOME` overrides the app dir (`~/.tidy`: config, `last_plan.json`, `runs/`, embedding cache, watch state). Set it to a scratch dir to experiment without touching the real config or undo history. `tidy` with no subcommand runs `plan` and then prompts `[y/N]` to apply.

## Architecture (all in `tidy.py`, stdlib only)

The pipeline is a cascade. Each layer only sees files the previous one couldn't place:

0. **Duplicates** (`find_duplicates`): size, then SHA-256. Keep the oldest copy; the others go to `_Review/Duplicates`.
1. **Rules** (`rule_pass`): `name_hints` regexes (each gated to `image`/`doc`/`any` extensions), then `ext_map`. Extensions in `semantic_exts` with no hit become `pending`; anything else unknown is `other`. JPG/HEIC with no camera model in Spotlight (`has_camera`, via `mdls`) are demoted from Photos to Graphics (method `metadata`).
2. **Embeddings** (`semantic_pass`): embed the cleaned filename plus a 600-char snippet, and compare by cosine against the `semantic` category descriptions. Accept only if the score is ≥ `embed_accept` and the gap to the runner-up is ≥ `embed_margin`.
3. **LLM** (`Ollama.ask`): send the top-3 candidates. Output is JSON-schema constrained to `{"c": int, "s": int}`, with `num_predict` ≤ 20. 0 means unsure, and the file goes to `_Review`. When scope is also unresolved, `s` is asked in the same call rather than a second one.

`Ollama` starts `ollama serve` itself if nothing is listening and stops it in `close()`; if the user already runs Ollama.app it reuses it and only unloads models.

**Scope** (Corporate/Personal) is decided by `scope_by_rules`, then the embedding margin, then the LLM. The order of precedence is:
1. `personal_name_terms` in the filename
2. `corporate_terms` or `corporate_name_terms` (generic company words, filename only) in the filename
3. `corporate_terms` in the content (career docs are excluded)

A rule hit always overrides the LLM's scope guess. `{scope}` in a `folders` path is replaced with Corporate or Personal. If scope is unknown, the file goes to `_Review/Corporate or Personal/`.

**Clients** (optional, `clients` / `TIDY_CLIENT_*`): when configured, `{scope}` becomes `Corporate/<client>` for Corporate documents, or `Corporate/<internal_folder>` (`_Internal`) when there's no client. `client_by_rules` takes the client named earliest in the filename, then in the content; otherwise `_Internal` (method `no-client`). Clients are rules only, by design: qwen2.5:1.5b asked to pick a client index answered "none" or the same client almost every time, and adding the question to the category call made its category answers worse. Don't reintroduce an LLM client pick without measuring on real files. With `company_name` set, every prompt starts "You are the CEO of …". Client regexes are merged into the corporate terms.

**Lifecycle:** `build_plan` writes the plan to `~/.tidy/last_plan.json`. `apply_plan` does `os.rename` only and writes `~/.tidy/runs/<ts>.json` with `moves` and `created_dirs`. `undo_last` replays the moves in reverse and removes only the `created_dirs` that are now empty.

**Watcher:** `install_watch` writes a launchd agent with `WatchPaths`. It calls `watch-check`, which only lists the scan folders and notifies when the count reaches `notify_threshold` (with a cooldown). It never loads a model.

## Invariants (tests enforce these; don't break them)

- **Never delete user files.** Moves only. Duplicates go to `_Review`, not to the trash.
- **Never overwrite.** On a name clash, `unique_path` appends ` (2)`.
- **Undo must not remove folders tidy didn't create**, even if they're empty.
- **Only top-level regular files** are scanned. Skip hidden files, Office lock files (`~$…`), partial downloads, files modified within `settle_minutes`, symlinks, directories (including `.app` bundles), and iCloud dataless placeholders (`SF_DATALESS`).
- **If the model is unavailable, leave documents in place.** Don't send them to `_Review`. Missing models (server up, model not pulled) *do* go to `_Review`.
- **Plan/apply race:** skip any file whose mtime changed since the plan. Refuse plans older than 24h.
- **Keep model output tiny:** integer enums only, temperature 0. Don't add free-text fields to the schema.
- **Keep runtime deps at zero.** `pypdf` is optional, with a PDFKit/JXA fallback. Don't add packages to the runtime path.

## Conventions

- Configuration lives in `DEFAULT_CONFIG`. User overrides in `~/.tidy/config.json` are merged shallowly, so overriding `folders` replaces the whole dict. `load_config` then adds derived `_`-prefixed keys (`_ext_to_cat`, `_image_exts`, `_corp_rx`, `_personal_rx`); put new derived values there rather than recomputing per file. `corporate_terms` / `personal_name_terms` are regex fragments joined into one alternation. Filename rules run on `match_name` (lowercased, with `_`, `-` and `.` turned into spaces), so a pattern needs `globex.?io`, not `globex\.?io`, to match `Globex.io_deck.pdf`. `tidy init` seeds only the commonly edited keys.
- Every user-visible decision carries a `method` (and `scope_method` / `client_method` for documents) (`hash`, `name`, `ext`, `metadata`, `embed`, `llm`, `content`, `no-signal`, `llm-unsure`, `low-confidence`, `no-model`, `no-client`). `plan -v` prints it. Any new path through the code needs one.
- Tests use the `env` fixture, which isolates `APP_DIR` and creates temp Downloads/Desktop/Documents, and the `ollama` fixture, a fake server on a random port. Its embeddings are a hashed bag-of-words, and its LLM replies follow keyword rules: `zzqx` in the file text → unsure (0), `bank`/`mom`/`family`/`grocery` → Personal, otherwise Corporate. `env` also stubs `has_camera` (always true), `notify` (collected in `env.notifications`) and `Ollama.binary` (so a "down" server is never really launched). Use `env.config(**overrides)` and `env.file(name, content)` (backdated past the settle window), and `placed(plan)` for assertions. Give test files distinct contents, or the duplicate layer will catch them.
- Client/company names are private: they come from `TIDY_CORPORATE_TERMS` / `TIDY_CORPORATE_NAME_TERMS` / `TIDY_CLIENT_*` / `TIDY_COMPANY_NAME` in a gitignored `.env` (`load_env`, `ENV_FILES`), never from `DEFAULT_CONFIG`. Tests use fictional names (`acme`, `globex`), and `env` points `ENV_FILES` at the temp dir so a developer's real `.env` never leaks in. Don't commit real names to code, tests or docs.
- macOS-only behavior (`mdls`, `osascript`, `launchctl`, `textutil`) must degrade gracefully on other platforms so the tests run on Linux CI.

## Known gaps / next work

- The thresholds (`embed_accept` 0.40, `embed_margin` 0.05, `scope_margin` 0.06) are untuned against real `all-minilm`. Tune them with `plan -v` on real files.
- `.pages`, `.numbers`, and `.key` files are classified by filename only (they're zip bundles with a protobuf inside).
- Scanned PDFs with no text layer would need OCR (Vision framework via JXA is the low-RAM option).
- The Corporate/Personal split is ambiguous for the owner's own company finances. Bank-account-specific terms are the planned fix.

# tidy

A local, low-RAM file organizer for macOS. It moves loose files out of Downloads and Desktop into `~/Documents`, and sorts documents into **Corporate** and **Personal**. Nothing leaves your Mac.

## Where files go

```
~/Documents/
├── Corporate/                     ← your companies and clients
│   ├── Finance/<year>/  Legal & Contracts/  Work & Projects/
│   └── Career & HR/  Reference/  Travel & Household/
├── Personal/                      ← same subfolders, your own life
├── Media & Files/                 ← Screenshots, Photos, Graphics, Video, Audio,
│                                    Installers, Archives, Code & Data, Design, Fonts, Other
└── _Review/                       ← low confidence, for you to check
    ├── Duplicates/
    └── Corporate or Personal/     ← category known, scope unclear
```

Your existing folders in Documents are merged into, never renamed. A name clash becomes `file (2).pdf`, so nothing is overwritten.

## Corporate vs Personal

"Corporate" means **your** businesses and clients, not "issued by a company". Your bank and utility bills come from corporations too. Each document is checked in this order, and the first match wins:

1. **Personal filename terms**: `resume`, `cv`, `payslip`, `2316`, `passport`, and so on. These are your own records, even though they mention your company.
2. **Corporate terms in the filename**: your company and client names.
3. **Corporate terms in the content**. Career documents are excluded, because your CV mentions your company.
4. **Embeddings** against the two scope descriptions.
5. **The small LLM.** If a category question is already being asked, the scope question goes in the same call.
6. **Unclear**: the file goes to `_Review/Corporate or Personal/`.

Keep `corporate_terms` current. When you sign a new client, add their name. This list is the most accurate signal the tool has.

## How it decides

Each layer only handles the files the previous, cheaper layer couldn't place.

| Layer | Handles | Cost |
|---|---|---|
| 0. Duplicates | Identical files (size + SHA-256). They go to `_Review/Duplicates` and are never deleted. | No model |
| 1. Rules | File extension, filename patterns (`Screenshot…`, `Invoice…`, `resume…`), and Spotlight camera metadata (a JPG with no camera model goes to Graphics instead of Photos). | No model |
| 2. Embeddings | Documents, compared against category descriptions. Uses `all-minilm` (~46 MB). It returns vectors, not text. | ~100 MB RAM |
| 3. Small LLM | Only files where the embedding layer isn't confident. The prompt lists the top 3 candidate folders, and the output is grammar-constrained to `{"c": N}`, about 5 tokens. | ~0.5–1.2 GB RAM, only while running |

Documents with no extractable text and a meaningless name, such as `document(3).pdf`, go to `_Review`. The tool doesn't guess.

**RAM profile:**
- When idle, tidy uses 0 MB. No daemon runs.
- Ollama is started when a run needs a model and stopped when the run ends. If you already run Ollama.app, tidy uses it and lets the models unload after 30 seconds.
- The watcher is a launchd `WatchPaths` agent. macOS wakes it when a folder changes. It only lists the folder and never loads a model.

## Development

This is a Claude Code project. `CLAUDE.md` describes the architecture and the invariants the tests enforce.

```bash
python3 -m pytest -q        # 21 tests, ~3s, uses a fake Ollama (no models needed)
bash install.sh --dev       # ~/.tidy/tidy.py symlinks here, so edits apply immediately
```

## Install

```bash
cd tidy && bash install.sh
```

The installer:
- picks `qwen2.5:0.5b` on Macs with 8 GB of RAM or less, and `qwen2.5:1.5b` otherwise
- installs Ollama via Homebrew if it's missing
- sets up a venv with `pypdf`
- adds `~/.tidy/bin` to your PATH

## Use

```bash
tidy doctor          # check models, folder access, watcher
tidy plan -v         # dry run: every file, its folder, and which layer decided
tidy                 # plan, then asks [y/N] before moving anything
tidy apply           # execute the last plan (plans expire after 24h)
tidy undo            # reverse the last run
tidy install-watch   # notification when ≥5 new files pile up (30-min cooldown)
tidy uninstall-watch
```

## Safety

These protections are built in:
- It only touches **top-level loose files** in the scan folders. It never touches subfolders, project folders, `.app` bundles, symlinks, or hidden files.
- It skips in-progress downloads (`.crdownload`, `.part`, and so on), files changed in the last 2 minutes, and iCloud placeholders. Reading a placeholder would force a download.
- Moves are same-volume renames. Every run writes a manifest to `~/.tidy/runs/`, and `undo` replays it in reverse.
- If a file changed between `plan` and `apply`, it's skipped.
- If Ollama is unavailable, documents stay where they are. They aren't dumped into `_Review`.
- `undo` removes only folders that tidy itself created, and only if they're empty. Your own folders in Documents are never removed.

## Private settings (`.env`)

Your company and client names are the one sensitive input, so they live in a gitignored `.env` rather than in `tidy.py`:

```bash
cp .env.example .env    # then edit TIDY_CORPORATE_TERMS
```

`TIDY_CORPORATE_TERMS` is a single regex alternation, such as `acme|globex\.?io`. tidy reads `~/.tidy/.env` first, then the repo's `.env`. A real environment variable beats both, and `corporate_terms` in `config.json` beats all of them. `install.sh` copies `.env` into `~/.tidy` (with `--dev`, the symlinked script finds the repo copy).

## Config (`~/.tidy/config.json`)

Any key in `DEFAULT_CONFIG` at the top of `tidy.py` can be overridden here. Common ones:

- `scan`: folders to organize. Default is `["~/Downloads", "~/Desktop"]`. Only add Documents if it isn't iCloud-synced.
- `dest`: where organized files go. Default is `~/Documents`.
- `corporate_terms`: regexes for your companies and clients. Empty by default; set them in `.env` instead (see below) so client names stay out of git.
- `personal_name_terms`: filename terms that are always Personal.
- `scope_desc` / `scope_margin`: what Corporate and Personal mean to the embedding and LLM layers, and how confident the embedding layer must be.
- `folders`: a path starting with `~` or `/` is used as-is. For example, `"photos": "~/Pictures/Photos"` keeps photos out of Documents.
- `semantic`: category descriptions. **The description is the classifier.** Edit the wording to change what lands where.
- `embed_accept` / `embed_margin`: confidence thresholds for the embedding layer. Run `tidy plan -v` and look at the scores. If good matches are going to the LLM, lower `embed_accept`. If wrong matches are being accepted, raise it.
- `name_hints`: regex → folder rules. These are the cheapest way to fix a recurring mistake.

## Known limits

- **Background watching needs Full Disk Access for Python.** macOS silently blocks launchd scripts from reading Downloads and Desktop. `tidy doctor` prints the exact binary path to add. Running `tidy` from Terminal works without this.
- Images are sorted by metadata only. There's no vision model, which keeps RAM low.
- `.pages`, `.numbers`, and `.key` files are classified by filename only.
- Scanned PDFs with no text layer are classified by filename, or go to `_Review` if the name is generic.

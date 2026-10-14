import json
import time
import zipfile
from datetime import datetime
from pathlib import Path

import tidy
from conftest import REAL_NOTIFY, placed

YEAR = str(datetime.fromtimestamp(time.time() - 3600).year)


# ───────────── scanning ─────────────

def test_scan_skips_hidden_partial_fresh_and_folders(env):
    cfg = env.config()
    env.file("keep.pdf")
    env.file(".hidden")
    env.file("~$port.docx", "Office lock file")
    env.file("movie.mp4.crdownload")
    env.file("just_saved.pdf", age=time.time())
    (env.downloads / "a_folder").mkdir()
    (env.downloads / "link.pdf").symlink_to(env.downloads / "keep.pdf")
    assert [p.name for p in tidy.scan(cfg)] == ["keep.pdf"]


def test_scan_never_rescans_destination(env):
    cfg = env.config(scan=[str(env.downloads), str(env.documents)])
    env.file("a.pdf")
    env.file("b.pdf", folder=env.documents)
    assert [p.name for p in tidy.scan(cfg)] == ["a.pdf"]


# ───────────── layer 0 + 1 ─────────────

def test_duplicates_keep_oldest(env):
    a = env.file("report.pdf", "same", age=time.time() - 7200)
    b = env.file("report (1).pdf", "same")
    env.file("other.pdf", "different")
    assert tidy.find_duplicates([a, b, env.downloads / "other.pdf"]) == {b}


def test_rules_by_name_and_extension(env):
    cfg = env.config()
    files = [env.file(n) for n in ["Screenshot 2026-10-01 at 10.00.png", "IMG_1.jpg", "x.dmg",
                                   "weird.xyz", "SOA_Oct.pdf", "JR_CV_2026.pdf", "mystery.pdf"]]
    decided, pending = tidy.rule_pass(files, cfg)
    cats = {p.name: c for p, (c, _, _) in decided.items()}
    assert cats == {"Screenshot 2026-10-01 at 10.00.png": "screenshots", "IMG_1.jpg": "photos",
                    "x.dmg": "installers", "weird.xyz": "other", "SOA_Oct.pdf": "finance",
                    "JR_CV_2026.pdf": "career"}
    assert [p.name for p in pending] == ["mystery.pdf"]


def test_screenshot_hint_only_applies_to_images(env):
    cfg = env.config()
    decided, pending = tidy.rule_pass([env.file("screenshot_guide.pdf")], cfg)
    assert not decided and len(pending) == 1


# ───────────── corporate / personal rules ─────────────

def test_scope_rules_precedence(env):
    cfg = env.config()
    r = lambda name, text="", cat="finance": tidy.scope_by_rules(Path(name), text, cat, cfg)
    assert r("JR_CV_2026.pdf", "CEO, Acme Global", "career") == ("personal", "name")
    assert r("payslip_acme_sept.pdf") == ("personal", "name")      # personal name beats company name
    assert r("Acme-Proposal.docx", cat="work") == ("corporate", "name")
    assert r("inv.pdf", "Billed to Globex Forex") == ("corporate", "content")
    assert r("cv.txt", "Acme", "career") == ("personal", "name")
    assert r("letter.pdf", "worked at Acme", "career") == (None, None)  # content alone never makes career corporate
    assert r("cvs_receipt.pdf") == (None, None)                     # 'cv' needs a word boundary
    assert r("Employee_Change_Form.pdf") == ("corporate", "name")    # filename-only company term...
    assert r("form.pdf", "employee number 42") == (None, None)       # ...never matched in content
    assert r("payslip_employee.pdf") == ("personal", "name")         # personal name still wins


def test_env_file_supplies_private_terms(env, monkeypatch):
    env.config()                                    # creates the app dir...
    tidy.CONFIG_PATH.write_text("{}")              # ...but with no terms in config.json
    (tidy.APP_DIR / ".env").write_text("# private\nTIDY_CORPORATE_TERMS=initech|hooli\n"
                                       "TIDY_CORPORATE_NAME_TERMS='user.?stor(y|ies)'\n"
                                       "TIDY_COMPANY_NAME=Acme Corp\n"
                                       "TIDY_CLIENT_PIED=Pied Piper = pied.?piper = compression startup\n"
                                       "TIDY_CLIENT_ROSS=Ross\n"
                                       "TIDY_CLIENT_STOCK=Stockroom = stockroom = ERP = inventory|item.?master\n"
                                       "TIDY_CLIENT_MESA=Mesa = = a town name too = \\bmesa\\b\n")
    cfg = tidy.load_config()
    assert cfg["company_name"] == "Acme Corp"
    assert cfg["clients"] == {"Pied Piper": ["pied.?piper", "compression startup", ""], "Ross": ["Ross", "", ""],
                              "Stockroom": ["stockroom", "ERP", "inventory|item.?master"],
                              "Mesa": ["", "a town name too", "\\bmesa\\b"]}
    by = lambda name, text="": tidy.client_by_rules(Path(name), text, cfg)
    assert by("Parts_Inventory.xlsx") == ("Stockroom", "name")                # filename-only pattern...
    assert by("notes.pdf", "inventory of ideas") == (None, None)              # ...never matched in text
    assert by("notes.pdf", "the stockroom move") == ("Stockroom", "content")
    assert by("Mesa_quote.pdf") == ("Mesa", "name")                           # filename-only client...
    assert by("letter.pdf", "office in Mesa, Arizona") == (None, None)        # ...ignores the text
    assert tidy.scope_by_rules(Path("letter.pdf"), "office in Mesa", "legal", cfg) == (None, None)
    assert tidy.scope_by_rules(Path("item_master.csv"), "", "finance", cfg) == ("corporate", "name")
    assert tidy.scope_by_rules(Path("pied_piper_sow.pdf"), "", "work", cfg) == ("corporate", "name")
    assert tidy.scope_by_rules(Path("x.pdf"), "for Hooli", "work", cfg) == ("corporate", "content")
    assert tidy.scope_by_rules(Path("User Stories.xlsx"), "", "work", cfg) == ("corporate", "name")
    monkeypatch.setenv("TIDY_CORPORATE_TERMS", "umbrella")          # real environment beats .env
    assert tidy.scope_by_rules(Path("x.pdf"), "for Hooli", "work", tidy.load_config()) == (None, None)


# ───────────── text extraction ─────────────

def test_extracts_docx_and_pptx(env):
    d = env.downloads / "a.docx"
    with zipfile.ZipFile(d, "w") as z:
        z.writestr("word/document.xml", "<w:t>Project proposal for Acme</w:t>")
    p = env.downloads / "b.pptx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("ppt/slides/slide1.xml", "<a:t>Quarterly report</a:t>")
    assert tidy.extract_text(d, 600) == "Project proposal for Acme"
    assert tidy.extract_text(p, 600) == "Quarterly report"


def test_generic_name_without_text_is_not_informative():
    assert not tidy.informative(Path("document(3).pdf"), "")
    assert tidy.informative(Path("aws_bill.pdf"), "")
    assert tidy.informative(Path("document.pdf"), "some text")


# ───────────── full plan with models ─────────────

def test_plan_sorts_corporate_and_personal(env, ollama):
    cfg = env.config(ollama_url=ollama.url)
    env.file("Invoice_AWS_Oct.txt", "Invoice to Acme Global Technologies for cloud services")
    env.file("Invoice_Grocery.txt", "Invoice for groceries delivered to my mom")
    env.file("JR_CV_2026.txt", "Experience: CEO Acme Global Technologies")
    env.file("api_spec.txt", "Globex Forex API specification requirements")
    env.file("bank_stmt_sept.txt", "Bank statement savings account monthly")
    env.file("zzqx_notes.txt", "zzqx qqqq")
    env.file("document.pdf", b"")
    env.file("meeting_notes.txt", "project meeting notes", folder=env.desktop)
    where = placed(tidy.build_plan(cfg))
    assert where["Invoice_AWS_Oct.txt"] == f"Corporate/Finance/{YEAR}"
    assert where["Invoice_Grocery.txt"] == f"Personal/Finance/{YEAR}"
    assert where["JR_CV_2026.txt"] == "Personal/Career & HR"
    assert where["api_spec.txt"].startswith("Corporate/")
    assert where["bank_stmt_sept.txt"].startswith("Personal/")
    assert where["zzqx_notes.txt"] == "_Review"
    assert where["document.pdf"] == "_Review"           # no text + generic name: never guessed
    assert "meeting_notes.txt" in where                 # Desktop is scanned too


def test_llm_output_is_grammar_constrained(env, ollama):
    cfg = env.config(ollama_url=ollama.url, embed_accept=0.99)  # force every doc to the LLM
    env.file("notes_on_thing.txt", "random words about a thing")
    tidy.build_plan(cfg)
    call = ollama.generate_calls[0]
    assert call["format"]["properties"]["c"]["enum"] == [0, 1, 2, 3]   # top-3 candidates + unsure
    assert call["format"]["properties"]["s"]["enum"] == [0, 1, 2]
    assert call["options"]["num_predict"] <= 20 and call["options"]["temperature"] == 0


def test_unclear_scope_goes_to_review(env, ollama):
    cfg = env.config(ollama_url=ollama.url, scope_margin=9)  # embeddings never decide scope
    env.file("Receipt_zzqx.txt", "zzqx")
    assert placed(tidy.build_plan(cfg))["Receipt_zzqx.txt"] == f"_Review/Corporate or Personal/Finance/{YEAR}"


def test_ollama_down_leaves_documents_in_place(env):
    cfg = env.config()  # points at a dead port
    env.file("mystery_notes.txt", "some content")
    env.file("Receipt_Shell.pdf", "shell")  # category from rules, but scope still needs a model
    env.file("Screenshot 1.png", "png")
    plan = tidy.build_plan(cfg)
    assert list(placed(plan)) == ["Screenshot 1.png"]
    assert plan["stats"]["left_in_place"] == 2


def test_missing_models_send_documents_to_review(env, ollama):
    ollama.models = []
    cfg = env.config(ollama_url=ollama.url)
    env.file("mystery_notes.txt", "some content")
    assert placed(tidy.build_plan(cfg))["mystery_notes.txt"] == "_Review"


def test_absolute_folder_override(env):
    cfg = env.config(folders={**tidy.DEFAULT_CONFIG["folders"], "photos": str(env.root / "Pictures")})
    env.file("IMG_2.heic")
    plan = tidy.build_plan(cfg)
    assert plan["items"][0]["dst"] == str(env.root / "Pictures" / YEAR / "IMG_2.heic")

def test_client_folders_inside_corporate(env, ollama):
    cfg = env.config(ollama_url=ollama.url, company_name="Acme",
                     clients={"Initech": ["initech", "payroll software platform"], "Hooli": ["hooli", ""]})
    env.file("Initech_SOW.txt", "statement of work and deliverables")
    env.file("rollout_notes.txt", "Acme meeting notes about the Hooli rollout")
    env.file("backlog.txt", "Acme sprint backlog for the payroll release")      # no client named
    env.file("Acme_policy.txt", "Acme leave policy handbook for staff")
    env.file("Invoice_Grocery.txt", "Invoice for groceries delivered to my mom")
    env.file("Invoice_Hooli_Oct.txt", "Invoice for consulting hours in October")
    plan = tidy.build_plan(cfg)
    where = placed(plan)
    assert where["Initech_SOW.txt"].startswith("Corporate/Initech/")        # client in the filename
    assert where["rollout_notes.txt"].startswith("Corporate/Hooli/")        # client in the content
    assert where["backlog.txt"].startswith("Corporate/_Internal/")          # never guessed by the model
    assert where["Acme_policy.txt"].startswith("Corporate/_Internal/")
    assert where["Invoice_Grocery.txt"] == f"Personal/Finance/{YEAR}"       # Personal never gets a client
    assert where["Invoice_Hooli_Oct.txt"] == f"Corporate/Hooli/Finance/{YEAR}"  # rule-decided category too
    methods = {Path(i["src"]).name: i["client_method"] for i in plan["items"]}
    assert (methods["Initech_SOW.txt"], methods["rollout_notes.txt"], methods["backlog.txt"]) == ("name", "content", "no-client")
    assert ollama.generate_calls and all(c["prompt"].startswith("You are the CEO of Acme") for c in ollama.generate_calls)
    assert all(set(c["format"]["properties"]) <= {"c", "s"} for c in ollama.generate_calls)


def test_no_clients_keeps_flat_corporate_layout(env, ollama):
    cfg = env.config(ollama_url=ollama.url)
    env.file("Invoice_Acme_Oct.txt", "Invoice to Acme for October services")
    assert placed(tidy.build_plan(cfg))["Invoice_Acme_Oct.txt"] == f"Corporate/Finance/{YEAR}"


def test_client_folders_need_no_model(env):
    cfg = env.config(clients={"Initech": ["initech", ""]})                   # Ollama is down
    env.file("Invoice_Acme_Oct.txt", "Invoice to Acme for October services")
    env.file("Invoice_Initech_Oct.txt", "Invoice for October services rendered")
    where = placed(tidy.build_plan(cfg))
    assert where["Invoice_Acme_Oct.txt"] == f"Corporate/_Internal/Finance/{YEAR}"
    assert where["Invoice_Initech_Oct.txt"] == f"Corporate/Initech/Finance/{YEAR}"


# ───────────── apply / undo ─────────────

def test_apply_and_undo_round_trip(env, ollama):
    cfg = env.config(ollama_url=ollama.url)
    mine = env.documents / "Personal"            # the user's own pre-existing, empty folder
    mine.mkdir()
    (env.documents / "Old").mkdir()
    env.file("keep.txt", "mine", folder=env.documents / "Old")
    env.file("Screenshot 1.png")
    env.file("Invoice_Grocery.txt", "groceries for my mom")
    env.file("Acme_proposal.txt", "proposal")
    before = sorted(p.name for p in env.downloads.iterdir())

    tidy.apply_plan(tidy.build_plan(cfg))
    assert not list(env.downloads.iterdir())
    assert (env.documents / "Personal" / "Finance" / YEAR / "Invoice_Grocery.txt").exists()

    tidy.undo_last()
    assert sorted(p.name for p in env.downloads.iterdir()) == before
    assert mine.is_dir()                                  # user's folder survives, though empty
    assert (env.documents / "Old" / "keep.txt").exists()
    assert not (env.documents / "Corporate").exists()     # folders tidy created are gone
    assert not (env.documents / "Media & Files").exists()


def test_apply_never_overwrites(env):
    cfg = env.config()
    env.file("Screenshot 1.png", "new")
    plan = tidy.build_plan(cfg)
    target = Path(plan["items"][0]["dst"])
    target.parent.mkdir(parents=True)
    target.write_text("old")
    tidy.apply_plan(plan)
    assert target.read_text() == "old"
    assert (target.parent / "Screenshot 1 (2).png").read_text() == "new"


def test_apply_skips_files_changed_since_plan(env):
    cfg = env.config()
    p = env.file("Screenshot 1.png")
    plan = tidy.build_plan(cfg)
    p.write_text("edited")
    tidy.apply_plan(plan)
    assert p.exists()


def test_stale_plan_is_refused(env):
    cfg = env.config()
    p = env.file("Screenshot 1.png")
    plan = tidy.build_plan(cfg)
    plan["created"] -= 2 * tidy.PLAN_MAX_AGE
    tidy.apply_plan(plan)
    assert p.exists()


def test_undo_with_nothing_to_undo(env, capsys):
    env.config()
    tidy.undo_last()
    assert "Nothing to undo" in capsys.readouterr().out


# ───────────── watcher ─────────────

def test_watch_notifies_once_per_batch_with_cooldown(env):
    cfg = env.config(notify_threshold=3)
    for i in range(3):
        env.file(f"f{i}.pdf")
    tidy.watch_check(cfg)
    tidy.watch_check(cfg)                       # same files: no repeat
    assert len(env.notifications) == 1
    env.file("f3.pdf")
    tidy.watch_check(cfg)                       # new file but inside cooldown
    assert len(env.notifications) == 1
    state = json.loads(tidy.STATE_PATH.read_text())
    state["last"] = 0
    tidy.STATE_PATH.write_text(json.dumps(state))
    tidy.watch_check(cfg)
    assert len(env.notifications) == 2 and "4 files" in env.notifications[1][1]


def test_notify_falls_back_when_terminal_notifier_is_blocked(env, monkeypatch):
    calls = []
    def run(cmd, **kw):
        calls.append(cmd[0])
        return type("R", (), {"returncode": 3 if cmd[0] == "/bin/tn" else 0})()
    tidy.APP_DIR.mkdir(parents=True, exist_ok=True)
    tidy.RUN_COMMAND.write_text("#!/bin/bash\n")      # inside env's temp app dir
    monkeypatch.setattr(tidy.shutil, "which", lambda name: "/bin/tn")
    monkeypatch.setattr(tidy.subprocess, "run", run)
    REAL_NOTIFY("Tidy", "5 files waiting")
    assert calls == ["/bin/tn", "osascript"]


def test_watch_below_threshold_is_silent(env):
    cfg = env.config(notify_threshold=5)
    env.file("a.pdf")
    tidy.watch_check(cfg)
    assert env.notifications == []

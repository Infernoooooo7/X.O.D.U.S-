"""Browser tests of the built static UI against a real backend (uvicorn on a free loopback port).

Playwright drives the installed Google Chrome (channel="chrome"); no browser is downloaded. Ollama is replaced by
FakeOllama (a local HTTP server speaking its API) or a scripted model, except in the tests marked `ollama`, which use
the real local model. Every flow runs at desktop width and at 360 px. Each view is also scanned with axe-core
(WCAG 2.0/2.1/2.2 A and AA rules). Screenshots go to experiments/results/gate-e/ (gitignored).
"""

from __future__ import annotations

import json
import re
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from tests.conftest import _ollama_up, free_port
from tests.helpers import ScriptedProvider, WriteNote, tool_call
from tests.kb_helpers import topic_markdown
from tests.test_knowledge_api import fake_chat
from xodus.api.app import UI_DIR, create_app
from xodus.config.settings import CONFIG_DIR, REPO_ROOT, load_settings
from xodus.providers.base import Completion

playwright_api = pytest.importorskip("playwright.sync_api")
expect = playwright_api.expect
pytestmark = pytest.mark.e2e

SHOTS = REPO_ROOT / "experiments" / "results" / "gate-e"
AXE = REPO_ROOT / "frontend" / "node_modules" / "axe-core" / "axe.min.js"
VIEWPORTS = {"desktop": {"width": 1280, "height": 900}, "mobile": {"width": 360, "height": 780}}
VIEWS = ["", "history/", "knowledge/", "memory/", "capabilities/", "approvals/", "activity/", "models/", "resources/",
         "experiments/", "settings/"]
AXE_RESULTS: dict[str, object] = {}


@pytest.fixture(autouse=True)
def ui_port(monkeypatch) -> int:
    """Each test serves on a free port, set in the configuration so the same-origin check accepts the page's requests."""
    port = free_port()
    monkeypatch.setenv("XODUS_SERVER__PORT", str(port))
    return port


@contextmanager
def serve(app) -> Iterator[str]:
    if not UI_DIR.is_dir():
        pytest.skip("UI not built (cd frontend; npm run build)")
    port = app.state.settings.server.port
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 30
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=20)


@pytest.fixture(scope="module")
def browser():
    with playwright_api.sync_playwright() as p:
        try:
            b = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # Chrome not installed
            pytest.skip(f"Google Chrome not available to Playwright: {exc}")
        yield b
        b.close()


@pytest.fixture(params=list(VIEWPORTS))
def page(request, browser):
    ctx = browser.new_context(viewport=VIEWPORTS[request.param], color_scheme="light")
    pg = ctx.new_page()
    pg.viewport_name = request.param
    errors: list[str] = []
    pg.on("pageerror", lambda e: errors.append(str(e)))
    yield pg
    ctx.close()
    assert not errors, f"uncaught errors in the page: {errors}"


def shot(page, name: str) -> None:
    SHOTS.mkdir(parents=True, exist_ok=True)
    page.screenshot(path=SHOTS / f"{name}-{page.viewport_name}.png", full_page=True)


def no_horizontal_scroll(page) -> None:
    assert page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 0


def axe(page, label: str) -> None:
    """Run axe-core on the current page; any WCAG A/AA violation fails the test (results kept for the report)."""
    page.add_script_tag(path=str(AXE))
    violations = page.evaluate("""async () => (await axe.run(document, {runOnly: {type: 'tag',
        values: ['wcag2a', 'wcag2aa', 'wcag21a', 'wcag21aa', 'wcag22aa']}})).violations.map(v => ({id: v.id,
        impact: v.impact, help: v.help, nodes: v.nodes.slice(0, 3).map(n => n.target.join(' '))}))""")
    AXE_RESULTS[f"{label}-{page.viewport_name}"] = violations
    assert violations == [], json.dumps(violations, indent=1)


def system_status(page):
    """The sidebar card's live health lines (the workspace core has its own status line, which also mentions Ollama)."""
    return page.get_by_role("region", name="System status").get_by_role("status")


def core(page):
    """The workspace core: its data-core-state names the state the page derived from the backend."""
    return page.locator("[data-core-state]").first


def kb_toml_settings(kb_settings, fake_ollama, extra: str = ""):
    return kb_settings(extra=extra, base_url=fake_ollama.url)


@pytest.fixture
def fake(fake_ollama, monkeypatch):
    monkeypatch.setenv("XODUS_INGESTION__MONITOR_INTERVAL_S", "0.2")
    fake_ollama.models = [{"name": "test-model:latest", "digest": "aaa111", "size": 2_000_000_000},
                          {"name": "other-model:latest", "digest": "bbb222", "size": 1_000_000_000},
                          {"name": "fake-embed:latest", "digest": "def", "size": 1}]
    return fake_ollama


# ---------------------------------------------------------------------------------------------- chat


def test_chat_streams_is_saved_and_appears_in_history(page, make_settings, fake):
    fake.chat_answer = lambda messages: "Bonjour from the local model."
    fake.chat_delay_s = 1.5  # the reply is visibly in progress before the backend finishes it
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        page.goto(url)
        page.get_by_label("Message").fill("Say hello in French.")
        page.get_by_role("button", name="Send").click()
        expect(page.get_by_text("Writing, not finished")).to_be_visible()
        expect(page.get_by_text("Finished", exact=True)).to_have_count(0)  # never shown before the backend confirms
        expect(page.get_by_text("Bonjour from the local model.")).to_be_visible(timeout=15_000)
        expect(page.get_by_text("Writing, not finished")).to_have_count(0)
        expect(page.get_by_role("article").filter(has_text="Bonjour from the local model.").get_by_text("test-model:latest")).to_be_visible()
        assert re.search(r"\?c=\d+", page.url)
        no_horizontal_scroll(page)
        shot(page, "chat")
        axe(page, "workspace-after-chat")
        page.goto(url + "/history/")
        expect(page.get_by_role("link", name="Say hello in French.", exact=True)).to_be_visible()
        page.get_by_role("link", name="Open Say hello in French.").click()
        expect(page.get_by_text("Bonjour from the local model.")).to_be_visible()
        assert len(fake.chat_requests) == 1


HOSTILE = ('<img src=x onerror="window.__pwned=1"><script>window.__pwned=2</script>'
           '<a href="javascript:window.__pwned=3">click</a> [chunk:١٢] **bold** {{7*7}}')


def test_model_output_that_looks_like_html_is_shown_as_text(page, make_settings, fake):
    fake.chat_answer = lambda messages: HOSTILE
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        page.goto(url)
        page.get_by_label("Message").fill(HOSTILE)  # the user's own turn is rendered too
        page.get_by_role("button", name="Send").click()
        reply = page.get_by_role("article").filter(has_text="window.__pwned=2")
        expect(reply).to_be_visible(timeout=15_000)
        assert page.evaluate("() => window.__pwned") is None  # nothing ran
        assert page.locator("main img, main script:not([src]), main a[href^='javascript']").count() == 0
        expect(reply).to_contain_text('<img src=x onerror="window.__pwned=1">')  # shown literally
        expect(reply.get_by_role("button", name=re.compile("Open source passage"))).to_have_count(0)  # not a citation
        page.goto(url + "/history/")  # and again when rendered from the saved conversation and the history list
        page.get_by_role("link", name=re.compile("^Open ")).first.click()
        expect(page.get_by_role("article").filter(has_text="window.__pwned=2")).to_be_visible()
        assert page.evaluate("() => window.__pwned") is None


def test_ollama_down_shows_a_clear_error_and_the_backend_stays_up(page, make_settings):
    with serve(create_app(make_settings(base_url=f"http://127.0.0.1:{free_port()}"))) as url:
        page.goto(url)
        page.get_by_label("Message").fill("Hello?")
        page.get_by_role("button", name="Send").click()
        alert = page.get_by_role("alert").filter(has_text="Ollama is not available")
        expect(alert).to_be_visible(timeout=15_000)
        expect(alert).to_contain_text("The backend is still running")
        expect(alert).to_contain_text("provider_unavailable")
        if page.viewport_name == "desktop":
            expect(system_status(page)).to_contain_text("unreachable")
        expect(core(page)).to_have_attribute("data-core-state", "offline")
        expect(page.get_by_text("Ollama is not reachable", exact=True)).to_be_visible()
        page.goto(url + "/capabilities/")  # the rest of XODUS keeps working
        expect(page.get_by_role("heading", name="List files")).to_be_visible()
        shot(page, "ollama-down")


def test_unsafe_ollama_settings_are_shown_until_they_are_fixed(page, make_settings, fake, monkeypatch):
    """The fake Ollama listens inside this process, so the backend reads this process's environment as Ollama's."""
    monkeypatch.delenv("OLLAMA_NO_CLOUD")
    monkeypatch.setenv("OLLAMA_ORIGINS", "*")
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        page.goto(url)
        alert = page.get_by_role("alert").filter(has_text="OLLAMA_NO_CLOUD is not set")
        expect(alert).to_be_visible(timeout=10_000)
        expect(alert).to_contain_text("OLLAMA_ORIGINS contains a wildcard (*)")
        expect(alert).to_contain_text("scripts\\start-ollama.ps1")
        if page.viewport_name == "desktop":
            expect(system_status(page)).to_contain_text("Ollama settings unsafe")
        no_horizontal_scroll(page)
        shot(page, "ollama-settings-unsafe")
        axe(page, "ollama-settings-unsafe")
        monkeypatch.setenv("OLLAMA_NO_CLOUD", "1")
        monkeypatch.delenv("OLLAMA_ORIGINS")
        expect(alert).to_have_count(0, timeout=12_000)  # the shell polls health every 5 s
        if page.viewport_name == "desktop":
            expect(system_status(page)).to_contain_text("Ollama settings checked")


def test_a_model_that_runs_elsewhere_is_named_and_refused(page, make_settings, fake):
    fake.models[0] = {**fake.models[0], "remote_model": "gpt-oss:120b", "remote_host": "https://ollama.com:443"}
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        page.goto(url)
        alert = page.get_by_role("alert").filter(has_text="does not run on this computer")
        expect(alert).to_be_visible(timeout=10_000)
        expect(alert).to_contain_text("test-model:latest")
        page.get_by_label("Message").fill("Hello?")
        page.get_by_role("button", name="Send").click()
        expect(page.get_by_role("alert").filter(has_text="remote_model_refused")).to_be_visible(timeout=15_000)
        assert fake.chat_requests == []  # nothing was sent to the model


# ---------------------------------------------------------------------------------------------- knowledge


def test_ingest_a_file_and_get_an_answer_with_a_checkable_citation(page, kb_settings, fake, docs_root):
    fake.chat_answer = fake_chat
    (docs_root / "astro.md").write_text(topic_markdown("astronomy", 3, 3), encoding="utf-8")
    with serve(create_app(kb_toml_settings(kb_settings, fake))) as url:
        page.goto(url + "/knowledge/")
        page.get_by_label("New collection name").fill("space")
        page.get_by_role("button", name="Create collection").click()
        expect(page.get_by_text("0 ready of 0 documents")).to_be_visible()
        page.get_by_label("Folder", exact=True).select_option("docs")
        page.get_by_label("File or subfolder (relative to the folder)").fill("astro.md")
        page.get_by_role("button", name="Queue for ingestion").click()
        expect(page.get_by_text("1 file(s) queued.")).to_be_visible()
        jobs = page.locator("section", has=page.get_by_role("heading", name="Ingestion jobs"))
        expect(jobs.get_by_text("Done", exact=True)).to_be_visible(timeout=60_000)
        expect(page.get_by_text("Ready", exact=True)).to_be_visible(timeout=15_000)
        no_horizontal_scroll(page)
        shot(page, "knowledge")
        axe(page, "knowledge-after-ingest")

        page.goto(url + "/")
        page.locator("label").filter(has_text="Ask documents").click()
        page.get_by_label("Message").fill("telescope nebula redshift supernova")
        page.get_by_role("button", name="Send").click()
        cite = page.get_by_role("button", name=re.compile(r"^Open source passage \d+$")).first
        expect(cite).to_be_visible(timeout=20_000)
        cite.click()
        dialog = page.get_by_role("dialog")
        expect(dialog).to_contain_text("Source passage")
        expect(dialog).to_contain_text("astro.md")
        shot(page, "citation")
        axe(page, "citation-dialog")


# ---------------------------------------------------------------------------------------------- approvals


def approval_app(make_settings, fake, notes: Path, provider):
    notes.mkdir(exist_ok=True)
    settings = make_settings(extra=f'[[permissions.roots]]\nid = "notes"\npath = "{notes.as_posix()}"\ngrants = ["fs.read", "fs.write"]\n',
                             base_url=fake.url)
    app = create_app(settings, provider=provider)
    app.state.registry.register(WriteNote("notes", notes), "test")  # a capability with a side effect: needs approval
    return app


def test_a_task_waits_for_approval_and_completes_only_after_it(page, make_settings, fake, tmp_path):
    notes = tmp_path / "notes"
    provider = ScriptedProvider(tool_turns=[tool_call("test__write_note", {"path": "hello.txt", "text": "Hi from XODUS"}),
                                            Completion("I wrote hello.txt.")])
    with serve(approval_app(make_settings, fake, notes, provider)) as url:
        page.goto(url)
        page.locator("label").filter(has_text="Run task").click()
        page.get_by_label("Message").fill("Write a note saying hi.")
        page.get_by_role("button", name="Start task").click()
        timeline = page.get_by_role("region", name="Task timeline")
        expect(timeline.get_by_text("Needs your approval")).to_be_visible(timeout=15_000)
        expect(timeline).to_contain_text("Step 1 wants to run test.write_note")  # the first step is step 1
        expect(timeline).to_contain_text('"path": "hello.txt"')
        expect(timeline).to_contain_text("cannot be undone")
        expect(page.get_by_role("link", name=re.compile("1 action is waiting for your approval"))).to_be_visible()
        expect(timeline.get_by_role("status").first).not_to_have_text("Completed")
        expect(core(page)).to_have_attribute("data-core-state", "waiting")  # the core says what the task says
        expect(page.get_by_role("group", name="Approval needed for step 1")).to_be_focused()  # the decision has the keyboard
        assert not (notes / "hello.txt").exists()  # nothing happened before approval
        no_horizontal_scroll(page)
        shot(page, "approval-pending")
        axe(page, "approval-pending")
        timeline.get_by_role("button", name="Approve this action").click()
        expect(timeline.get_by_role("status").first).to_have_text("Completed", timeout=15_000)
        expect(timeline).to_contain_text("returned")
        expect(timeline).to_contain_text("I wrote hello.txt.")
        assert (notes / "hello.txt").read_text(encoding="utf-8") == "Hi from XODUS"
        expect(page.get_by_text("The task is paused until you decide.")).to_have_count(0)
        expect(core(page)).to_have_attribute("data-core-state", "idle")
        shot(page, "approval-done")


def test_denying_from_the_inbox_means_nothing_is_written(page, make_settings, fake, tmp_path):
    notes = tmp_path / "notes"
    provider = ScriptedProvider(tool_turns=[tool_call("test__write_note", {"path": "nope.txt", "text": "x"}),
                                            Completion("I could not write the note.")])
    app = approval_app(make_settings, fake, notes, provider)
    with serve(app) as url:
        page.goto(url)
        page.locator("label").filter(has_text="Run task").click()
        page.get_by_label("Message").fill("Write a note.")
        page.get_by_role("button", name="Start task").click()
        expect(page.get_by_role("region", name="Task timeline").get_by_text("Needs your approval")).to_be_visible(timeout=15_000)
        page.goto(url + "/approvals/")
        card = page.get_by_role("listitem").filter(has_text="test.write_note")
        expect(card).to_contain_text("nope.txt")
        expect(card).to_contain_text("expires in")
        axe(page, "approvals-inbox")
        shot(page, "approvals-inbox")
        card.get_by_role("button", name="Deny").click()
        expect(page.get_by_text("Nothing is waiting")).to_be_visible(timeout=10_000)
        expect(page.get_by_text("Denied", exact=True)).to_be_visible()
        deadline = time.time() + 15
        while time.time() < deadline and app.state.tasks.list()[0].status not in ("completed", "blocked", "error", "failed_safe"):
            time.sleep(0.1)
        assert not (notes / "nope.txt").exists()


# ---------------------------------------------------------------------------------------------- memory


def test_memory_create_approve_a_proposal_and_delete(page, make_settings, fake):
    app = create_app(make_settings(base_url=fake.url))
    with serve(app) as url:
        page.goto(url + "/memory/")
        page.get_by_label("Text").fill("I prefer short answers.")
        page.get_by_role("button", name="Save item").click()
        expect(page.get_by_text("I prefer short answers.")).to_be_visible()
        app.state.knowledge.memory.propose("fact", "The user lives in Oslo.", source="task:ab12", model="qwen3", prompt_version="extract-1")
        page.reload()
        expect(page.get_by_text("waiting for your decision")).to_be_visible()
        page.get_by_role("button", name="Show them").click()
        item = page.get_by_role("listitem").filter(has_text="The user lives in Oslo.")
        expect(item).to_contain_text("Proposed by a model: not used yet")
        expect(item).to_contain_text("extract-1")
        axe(page, "memory-proposed")
        item.get_by_role("button", name="Approve").click()
        page.get_by_label("Status", exact=True).select_option("")  # not the sidebar's "System status" region
        item = page.get_by_role("listitem").filter(has_text="The user lives in Oslo.")
        expect(item).to_contain_text("Approved")
        shot(page, "memory")
        item.get_by_role("button", name=re.compile("Delete memory item")).click()
        page.get_by_role("alertdialog").get_by_role("button", name="Delete item").click()
        expect(page.get_by_text("The user lives in Oslo.")).to_have_count(0)
        expect(page.get_by_text("Deleted.")).to_be_visible()
        remaining = [m["content"] for m in app.state.knowledge.memory.store.list()]
        assert remaining == ["I prefer short answers."]  # deleted in the database, not just hidden


# ---------------------------------------------------------------------------------------------- models


MODEL_PROFILES = """
[[models.profiles]]
id = "other"
label = "Other model"
model = "other-model:latest"
[[models.profiles]]
id = "missing"
label = "Missing model"
model = "not-downloaded:latest"
"""


def test_switching_the_model_profile(page, make_settings, fake):
    app = create_app(make_settings(extra=MODEL_PROFILES, base_url=fake.url))
    with serve(app) as url:
        page.goto(url + "/models/")
        missing = page.get_by_role("article").filter(has=page.get_by_role("heading", name="Missing model"))
        expect(missing).to_contain_text("ollama pull not-downloaded:latest")
        expect(missing.get_by_role("button")).to_have_count(0)  # no button that could only fail
        other = page.get_by_role("article").filter(has=page.get_by_role("heading", name="Other model"))
        other.get_by_role("button", name="Switch to this profile").click()
        dialog = page.get_by_role("alertdialog")
        expect(dialog).to_contain_text("Unload the current model")
        axe(page, "model-switch-dialog")
        dialog.get_by_role("button", name="Switch to Other model").click()
        expect(page.get_by_text(re.compile(r"Switched to Other model in"))).to_be_visible(timeout=20_000)
        expect(other).to_contain_text("Active")
        assert app.state.models.active_id == "other"
        assert "other-model:latest" in fake.loaded and "test-model:latest" not in fake.loaded
        no_horizontal_scroll(page)
        shot(page, "models")
        expect(page.get_by_text("default to other")).to_be_visible()


# ---------------------------------------------------------------------------------------------- resources and extensions


def test_resource_view_extensions_and_process_control(page, kb_settings, fake, monkeypatch):
    monkeypatch.setenv("XODUS_PLUGINS__DIR", str(REPO_ROOT / "plugins"))
    with serve(create_app(kb_toml_settings(kb_settings, fake))) as url:
        page.goto(url + "/capabilities/")
        card = page.get_by_role("article").filter(has=page.get_by_role("heading", name="Text statistics", exact=True)).first
        card.get_by_role("button", name="Try it").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label("Text").fill("One two three. Four five.")
        dialog.get_by_role("button", name="Run text.stats").click()
        expect(dialog.get_by_role("status")).to_contain_text('"words": 5', timeout=20_000)
        axe(page, "capability-try-dialog")
        page.keyboard.press("Escape")

        page.goto(url + "/resources/")
        expect(page.get_by_text("Levels are evaluated and shown only")).to_be_visible()
        expect(page.get_by_text("Optional helpers and plugins").filter(visible=True).first).to_be_visible()
        expect(page.get_by_text("Browser tab").filter(visible=True).first).to_be_visible()
        plugin = page.get_by_role("listitem").filter(has_text="plugin: text.stats")
        expect(plugin).to_have_count(3)  # venv launcher, interpreter and console host, each counted once, all optional
        for i in range(3):
            expect(plugin.nth(i)).to_contain_text("tier optional")
        no_horizontal_scroll(page)
        shot(page, "resources")
        axe(page, "resources")
        plugin.get_by_role("button", name="Stop process").click()  # only the root process of the job carries it
        page.get_by_role("alertdialog").get_by_role("button", name="Stop process").click()
        expect(page.get_by_role("listitem").filter(has_text="plugin: text.stats")).to_have_count(0, timeout=10_000)

        page.get_by_role("button", name="Pause ingestion").click()
        expect(page.get_by_text("Paused", exact=True)).to_be_visible()
        page.get_by_role("button", name="Resume ingestion").click()
        expect(page.get_by_text("Idle", exact=True)).to_be_visible()


def test_list_files_refuses_an_escape(page, make_settings, fake):
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        page.goto(url + "/capabilities/")
        page.get_by_role("article").filter(has=page.get_by_role("heading", name="List files")).get_by_role("button", name="Try it").click()
        dialog = page.get_by_role("dialog")
        dialog.get_by_label(re.compile("^Path")).fill("..")
        dialog.get_by_role("button", name="Run fs.list_files").click()
        expect(dialog.get_by_role("alert")).to_contain_text("'..' is not allowed")


# ---------------------------------------------------------------------------------------------- every view: accessibility


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("view", VIEWS)
def test_every_view_passes_axe_and_fits_the_width(browser, make_settings, fake, view, scheme):
    for name, size in VIEWPORTS.items():
        ctx = browser.new_context(viewport=size, color_scheme=scheme)
        ctx.add_init_script(f"localStorage.setItem('xodus-theme', '{scheme}')")  # dark is the default whatever the OS says
        pg = ctx.new_page()
        pg.viewport_name = f"{name}-{scheme}"
        with serve(create_app(make_settings(extra=MODEL_PROFILES, base_url=fake.url))) as url:
            pg.goto(url + "/" + view)
            expect(pg.get_by_role("heading", level=1)).to_be_visible()
            pg.wait_for_timeout(800)
            no_horizontal_scroll(pg)
            shot(pg, f"view-{view.strip('/') or 'workspace'}")
            axe(pg, view.strip("/") or "workspace")
        ctx.close()


def test_keyboard_and_mobile_navigation(browser, make_settings, fake):
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        ctx = browser.new_context(viewport=VIEWPORTS["desktop"])
        pg = ctx.new_page()
        pg.goto(url)
        pg.keyboard.press("Tab")
        expect(pg.get_by_role("link", name="Skip to content")).to_be_focused()
        nav = pg.get_by_role("navigation", name="Views")
        expect(nav.get_by_role("link", name=re.compile("Workspace"))).to_have_attribute("aria-current", "page")
        ctx.close()
        ctx = browser.new_context(viewport=VIEWPORTS["mobile"])
        pg = ctx.new_page()
        pg.goto(url)
        pg.get_by_role("button", name="Open the list of views").click()
        pg.get_by_role("dialog").get_by_role("link", name=re.compile("Knowledge")).click()
        expect(pg.get_by_role("heading", level=1, name="Knowledge")).to_be_visible()
        assert pg.url.endswith("/knowledge/")
        ctx.close()


# ---------------------------------------------------------------------------------------------- core, motion, palette, theme

INK = """() => { const c = document.querySelector('[data-core-state] canvas'); const d = c.getContext('2d')
    .getImageData(0, 0, c.width, c.height).data; let s = 0; for (let i = 3; i < d.length; i += 4) s += d[i]; return s; }"""


def test_the_core_follows_real_states_only(page, make_settings, fake):
    fake.chat_answer = lambda messages: "one two three four five six seven eight"
    fake.chat_delay_s = 1.5  # nothing has arrived yet: thinking
    fake.chat_word_delay_s = 0.3  # then the words arrive one by one: writing
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        page.goto(url)
        expect(core(page)).to_have_attribute("data-core-state", "idle", timeout=10_000)
        expect(page.get_by_role("status").filter(has_text="Ready")).to_be_visible()
        expect(core(page)).to_have_attribute("data-animating", "true")
        shot(page, "workspace-hero")
        page.get_by_label("Message").fill("Count to eight.")
        page.get_by_role("button", name="Send").click()
        expect(core(page)).to_have_attribute("data-core-state", "thinking")
        expect(page.get_by_text("Waiting for the first token")).to_be_visible()
        expect(core(page)).to_have_attribute("data-core-state", "streaming", timeout=10_000)
        expect(page.get_by_text("Writing", exact=True)).to_be_visible()
        question = page.locator(".prose-reply").filter(has_text="Count to eight.")
        expect(question).to_have_count(1)  # the live copy and the saved copy are never shown together
        shot(page, "workspace-streaming")
        expect(core(page)).to_have_attribute("data-core-state", "idle", timeout=15_000)  # only after the backend's "done"
        expect(page.get_by_role("article").filter(has_text="one two three four five six seven eight")).to_have_count(1)
        expect(question).to_have_count(1)
        assert page.evaluate(INK) > 0


def test_reduced_motion_keeps_the_core_still_and_says_the_state_in_words(browser, make_settings, fake):
    fake.chat_answer = lambda messages: "still here"
    fake.chat_delay_s = 1.5
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        ctx = browser.new_context(viewport=VIEWPORTS["desktop"], reduced_motion="reduce")
        pg = ctx.new_page()
        pg.goto(url)
        expect(core(pg)).to_have_attribute("data-core-state", "idle", timeout=10_000)
        pg.wait_for_timeout(600)
        expect(core(pg)).to_have_attribute("data-animating", "false")  # the frame loop never started
        assert pg.evaluate(INK) > 0  # one still frame was drawn instead
        assert pg.evaluate("() => parseFloat(getComputedStyle(document.querySelector('.enter')).animationDuration)") < 0.001
        pg.get_by_label("Message").fill("Hello?")
        pg.get_by_role("button", name="Send").click()
        expect(core(pg)).to_have_attribute("data-core-state", "thinking")
        expect(core(pg)).to_have_attribute("data-animating", "false")
        expect(pg.get_by_text("Waiting for the first token")).to_be_visible()
        expect(pg.get_by_role("article").filter(has_text="still here")).to_be_visible(timeout=15_000)
        ctx.close()


def test_the_core_stops_drawing_when_it_is_scrolled_out_of_view(browser, make_settings, fake):
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        ctx = browser.new_context(viewport={"width": 1280, "height": 420})
        pg = ctx.new_page()
        pg.goto(url)
        expect(core(pg)).to_have_attribute("data-animating", "true", timeout=10_000)
        # room to scroll the core fully out of view (the page alone may be too short at this height)
        pg.evaluate("() => document.querySelector('main').insertAdjacentHTML('beforeend', '<div style=\"height:3000px\"></div>')")
        pg.evaluate("() => window.scrollTo(0, document.documentElement.scrollHeight)")
        expect(core(pg)).to_have_attribute("data-animating", "false")
        pg.evaluate("() => window.scrollTo(0, 0)")
        expect(core(pg)).to_have_attribute("data-animating", "true")
        ctx.close()


def html_dark(page) -> bool:
    return "dark" in (page.locator("html").get_attribute("class") or "")


def test_command_palette_navigates_and_runs_real_actions(page, make_settings, fake):
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        page.goto(url)
        expect(core(page)).to_have_attribute("data-core-state", "idle", timeout=10_000)
        assert html_dark(page)  # dark is the default
        page.keyboard.press("Control+k")
        dialog = page.get_by_role("dialog", name="Command palette")
        box = dialog.get_by_role("combobox", name="Search views and actions")
        expect(box).to_be_focused()
        shot(page, "command-palette")
        axe(page, "command-palette")
        box.fill("knowledge")
        expect(dialog.get_by_role("option").first).to_have_attribute("aria-selected", "true")
        page.keyboard.press("Enter")
        expect(page.get_by_role("heading", level=1, name="Knowledge")).to_be_visible()
        expect(dialog).to_have_count(0)

        page.keyboard.press("Control+k")  # an action with a real effect, confirmed by the backend
        box.fill("pause ingestion")
        page.keyboard.press("Enter")
        expect(dialog.get_by_role("status")).to_contain_text("Background ingestion paused.")
        assert page.request.get(url + "/api/health").json()["knowledge"]["ingestion"]["paused"] is True
        box.fill("resume")
        page.keyboard.press("Enter")
        expect(dialog.get_by_role("status")).to_contain_text("Background ingestion resumed.")
        assert page.request.get(url + "/api/health").json()["knowledge"]["ingestion"]["paused"] is False
        box.fill("verify audit")
        page.keyboard.press("Enter")
        expect(dialog.get_by_role("status")).to_contain_text("The audit log is intact")

        box.fill("")  # arrow keys move the choice; Escape closes
        page.keyboard.press("ArrowDown")
        expect(dialog.get_by_role("option").nth(1)).to_have_attribute("aria-selected", "true")
        page.keyboard.press("Escape")
        expect(dialog).to_have_count(0)

        page.keyboard.press("Control+k")  # the theme, kept across a reload, and the top-bar toggle
        box.fill("light theme")
        page.keyboard.press("Enter")
        assert not html_dark(page)
        page.reload()
        expect(page.get_by_role("heading", level=1, name="Knowledge")).to_be_visible()
        assert not html_dark(page)
        page.get_by_role("button", name="Switch to dark theme").click()
        assert html_dark(page)


def test_keyboard_only_chat_from_the_palette(browser, make_settings, fake):
    fake.chat_answer = lambda messages: "Typed and sent without a mouse."
    with serve(create_app(make_settings(base_url=fake.url))) as url:
        ctx = browser.new_context(viewport=VIEWPORTS["desktop"])
        pg = ctx.new_page()
        pg.goto(url + "/history/")
        expect(pg.get_by_role("heading", level=1, name="History")).to_be_visible()
        pg.keyboard.press("Control+k")
        pg.keyboard.type("ask your documents")
        pg.keyboard.press("Enter")  # from another view: opens the workspace in that mode, in the message box
        expect(pg.get_by_label("Message")).to_be_focused()
        expect(pg.locator("label").filter(has_text="Ask documents").locator("input")).to_be_checked()
        pg.keyboard.press("Control+k")
        pg.keyboard.type("chat with")
        pg.keyboard.press("Enter")  # in the workspace: switches the mode and puts the cursor back in the box
        expect(pg.get_by_label("Message")).to_be_focused()
        expect(pg.locator("label").filter(has_text="Chat").locator("input")).to_be_checked()
        pg.keyboard.type("Hello from the keyboard.")
        pg.keyboard.press("Control+Enter")
        expect(pg.get_by_role("article").filter(has_text="Typed and sent without a mouse.")).to_be_visible(timeout=15_000)
        ctx.close()


def test_zz_write_axe_results():
    SHOTS.mkdir(parents=True, exist_ok=True)
    (SHOTS / "axe.json").write_text(json.dumps(AXE_RESULTS, indent=1), encoding="utf-8")


# ---------------------------------------------------------------------------------------------- the real model


@pytest.fixture
def real_app(tmp_path, monkeypatch):
    if not _ollama_up():
        pytest.skip("Ollama is not running on 127.0.0.1:11434")
    monkeypatch.setenv("XODUS_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("XODUS_LOG_DIR", str(tmp_path / "logs"))
    return create_app(load_settings([CONFIG_DIR / "default.toml"]))


@pytest.mark.ollama
def test_live_chat_streams_a_real_answer(browser, real_app):
    with serve(real_app) as url:
        pg = browser.new_context(viewport=VIEWPORTS["desktop"]).new_page()
        pg.goto(url)
        pg.get_by_label("Message").fill("List ten fruits, one per line, with one short sentence about each.")
        pg.get_by_role("button", name="Send").click()
        expect(pg.get_by_text("Writing, not finished")).to_be_visible(timeout=30_000)
        # streaming is visible: text grows on screen before the backend confirms completion
        pg.wait_for_function("() => [...document.querySelectorAll('article[aria-live] .prose-reply')].some(e => e.textContent.length > 60)",
                             timeout=120_000)
        expect(pg.get_by_text("Writing, not finished")).to_have_count(0, timeout=180_000)
        expect(pg.get_by_text(re.compile(r"ttft \d+ ms"))).to_be_visible()
        pg.viewport_name = "desktop-live"
        shot(pg, "live-chat")


@pytest.mark.ollama
def test_live_models_view_offers_only_qwen35(browser, real_app):
    """The checked-in configuration has one generation model, qwen3.5:4b: installed, digest-pinned and active."""
    with serve(real_app) as url:
        pg = browser.new_context(viewport=VIEWPORTS["desktop"]).new_page()
        pg.goto(url + "/models/")
        cards = pg.get_by_role("article")
        expect(cards).to_have_count(1)
        expect(cards.first).to_contain_text("qwen3.5:4b")
        expect(cards.first).to_contain_text("matches the pin")
        expect(cards.first.get_by_text("Active", exact=True)).to_be_visible()
        expect(cards.first.get_by_role("button")).to_have_count(0)  # nothing to switch to
        pg.viewport_name = "desktop-live"
        shot(pg, "live-models")

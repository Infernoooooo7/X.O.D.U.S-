"""Browser tests of the built static UI served by a real backend (uvicorn on a free loopback port).

Uses the installed Google Chrome through Playwright (channel="chrome"); no browser is downloaded.
Screenshots go to experiments/results/gate-b/ (gitignored).
"""

from __future__ import annotations

import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
import uvicorn

from tests.conftest import _ollama_up, free_port
from xodus.api.app import UI_DIR, create_app
from xodus.config.settings import CONFIG_DIR, REPO_ROOT, load_settings

playwright_api = pytest.importorskip("playwright.sync_api")
pytestmark = pytest.mark.e2e

SHOTS = REPO_ROOT / "experiments" / "results" / "gate-b"


@contextmanager
def backend(tmp_path: Path, ollama_url: str = "http://127.0.0.1:11434") -> Iterator[str]:
    if not UI_DIR.is_dir():
        pytest.skip("UI not built (cd frontend; npx next build)")
    port = free_port()
    override = tmp_path / "override.toml"
    override.write_text(
        f'log_dir = "{(tmp_path / "logs").as_posix()}"\n[server]\nport = {port}\n[ollama]\nbase_url = "{ollama_url}"\n',
        encoding="utf-8",
    )
    settings = load_settings([CONFIG_DIR / "default.toml", override])
    server = uvicorn.Server(uvicorn.Config(create_app(settings), host="127.0.0.1", port=port, log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.time() + 20
    while not server.started and time.time() < deadline:
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=15)


@pytest.fixture
def page():
    with playwright_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome", headless=True)
        except Exception as exc:  # Chrome not installed
            pytest.skip(f"Google Chrome not available to Playwright: {exc}")
        context = browser.new_context(viewport={"width": 1100, "height": 900})
        yield context.new_page()
        browser.close()


def test_ui_streams_a_real_answer(tmp_path, page):
    if not _ollama_up():
        pytest.skip("Ollama is not running on 127.0.0.1:11434")
    expect = playwright_api.expect
    with backend(tmp_path) as url:
        page.goto(url)
        expect(page.get_by_role("status").first).to_contain_text("Ollama · ok", timeout=15_000)
        page.get_by_label("Message").fill("List ten fruits, one per line, with one short sentence about each.")
        page.get_by_role("button", name="Send").click()
        # Streaming is visible: text is on screen before the backend confirms completion.
        page.wait_for_function(
            "() => { const r = document.querySelector('.reply');"
            " return r && r.textContent.length > 40 && !document.querySelector('.ticket'); }",
            timeout=60_000,
        )
        expect(page.locator(".meta[aria-live]")).to_contain_text("Streaming")
        expect(page.locator(".ticket")).to_contain_text("TTFT", timeout=120_000)
        expect(page.locator(".meta[aria-live]")).to_contain_text("Completed")
        assert len(page.locator(".reply").inner_text()) > 100
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=SHOTS / "ui-chat-streamed.png", full_page=True)


def test_ui_shows_clear_error_when_ollama_is_down(tmp_path, page):
    expect = playwright_api.expect
    with backend(tmp_path, ollama_url=f"http://127.0.0.1:{free_port()}") as url:
        page.goto(url)
        expect(page.get_by_role("status").first).to_contain_text("Ollama · unreachable", timeout=15_000)
        page.get_by_label("Message").fill("Hello?")
        page.get_by_role("button", name="Send").click()
        alert = page.locator(".error")
        expect(alert).to_contain_text("Ollama is not available")
        expect(alert).to_contain_text("backend is still running")
        expect(alert).to_contain_text("provider_unavailable")
        # The backend is still serving: health keeps answering and the capability still works.
        expect(page.get_by_role("status").first).to_contain_text("Backend · ok")
        page.get_by_role("button", name="List").click()
        expect(page.locator(".files")).to_contain_text("welcome.txt")
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=SHOTS / "ui-ollama-down.png", full_page=True)


def test_ui_runs_list_files_capability(tmp_path, page):
    expect = playwright_api.expect
    with backend(tmp_path) as url:
        page.goto(url)
        page.get_by_label("Folder (relative to root)").fill("notes")
        page.get_by_role("button", name="List").click()
        expect(page.locator(".files")).to_contain_text("ideas.md")
        page.get_by_label("Folder (relative to root)").fill("..")
        page.get_by_role("button", name="List").click()
        expect(page.locator(".error")).to_contain_text("'..' is not allowed")

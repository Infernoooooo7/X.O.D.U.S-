"""The ingestion worker as a real process: processing, forced stop and resume, attempt limits, watchdog, outages."""

from __future__ import annotations

import os
import subprocess
import sys
import time
from pathlib import Path

import psutil
import pytest

from tests.kb_helpers import HashEmbedder, topic_markdown
from xodus.ingestion.worker import SETTINGS_ENV, owner_alive, owner_id
from xodus.storage.knowledge import KnowledgeStore

BACKEND_DIR = Path(__file__).resolve().parents[1]
NO_WINDOW = 0x08000000


def wait_for(condition, timeout: float = 30.0, interval: float = 0.05, what: str = "condition"):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = condition()
        if value:
            return value
        time.sleep(interval)
    raise AssertionError(f"timed out waiting for {what}")


class WorkerProcess:
    def __init__(self, settings, tmp_path: Path, tag: str = "w") -> None:
        env = {**os.environ, SETTINGS_ENV: settings.model_dump_json()}
        self.log = open(tmp_path / f"worker-{tag}.err", "wb")
        self.popen = subprocess.Popen([sys.executable, "-m", "xodus.ingestion.worker"], cwd=BACKEND_DIR, env=env,
                                      stdout=subprocess.DEVNULL, stderr=self.log, creationflags=NO_WINDOW)

    def kill(self) -> None:
        if self.popen.poll() is None:
            psutil.Process(self.popen.pid).kill()  # TerminateProcess: no cleanup code runs
        self.popen.wait(timeout=15)

    def alive(self) -> bool:
        return self.popen.poll() is None


@pytest.fixture
def workers():
    started: list[WorkerProcess] = []
    yield started
    for w in started:
        w.kill()
        w.log.close()


@pytest.fixture
def setup(kb_settings, kb_store, fake_ollama, docs_root, tmp_path, workers):
    settings = kb_settings(base_url=fake_ollama.url)

    def start(cfg=None, tag="w") -> WorkerProcess:
        w = WorkerProcess(cfg or settings, tmp_path, tag)
        workers.append(w)
        return w

    class Ctx:
        pass

    c = Ctx()
    c.settings, c.store, c.fake, c.docs, c.start, c.tmp = settings, kb_store, fake_ollama, docs_root, start, tmp_path
    return c


def with_ingestion(settings, **kw):
    return settings.model_copy(update={"ingestion": settings.ingestion.model_copy(update=kw)})


def queue(c, rel: str, text: str | None = None, collection: str = "main") -> dict:
    if text is not None:
        (c.docs / rel).write_text(text, encoding="utf-8")
    coll = c.store.collection_by_name(collection) or c.store.create_collection(collection)
    return c.store.enqueue_job(coll["id"], "docs", rel)


def expected_chunks(c, text: str) -> list[tuple[int, str, str]]:
    """What an uninterrupted in-process run produces for this text."""
    from xodus.ingestion.pipeline import PipelineDeps, Progress, RootInfo, process_job
    from xodus.storage.sqlite import Database

    db = Database(c.tmp / "expected.db", embedding_dim=c.settings.embedding.dimensions)
    db.open()
    store = KnowledgeStore(db)
    coll = store.create_collection("x")
    (c.docs / "expected.md").write_text(text, encoding="utf-8")
    store.enqueue_job(coll["id"], "docs", "expected.md")
    deps = PipelineDeps(c.settings, store, HashEmbedder(), {"docs": RootInfo(c.docs, frozenset({"fs.read"}))}, "x:1", Progress(), lambda s: None)
    result = process_job(deps, store.claim_next_job("x:1"))
    chunks = [(ch["ordinal"], ch["text"], ch["heading_path"]) for ch in store.list_chunks(result["document_id"], 200)]
    db.close()
    (c.docs / "expected.md").unlink()
    return chunks


def test_owner_identity_distinguishes_a_reused_pid():
    me = owner_id()
    assert owner_alive(me)
    pid, created = me.split(":")
    assert not owner_alive(f"{pid}:{float(created) - 100:.3f}")  # same pid, different process: not the owner
    assert not owner_alive("999999:1.000") and not owner_alive("garbage")


def test_worker_process_ingests_a_queued_file_and_idles(setup):
    job = queue(setup, "a.md", topic_markdown("astronomy", 4, 3))
    w = setup.start()
    done = wait_for(lambda: (j := setup.store.get_job(job["id"]))["status"] in ("completed", "failed") and j, what="job")
    assert done["status"] == "completed", done["error"]
    assert done["outcome"] == "ingested" and done["chunks_done"] > 2 and done["attempts"] == 1 and done["claimed_by"] is None
    stats = setup.store.stats()
    assert stats["chunks"] == stats["vectors"] == done["chunks_done"] and stats["documents"] == {"ready": 1}
    assert w.alive()  # it keeps polling; the backend's idle reaper ends it, not the worker
    sent = setup.fake.embedded_texts()
    assert len(sent) == done["chunks_done"] and all(c["options"] == {"num_gpu": 0} for c in setup.fake.embed_calls)  # CPU placement
    assert max(len(c["input"]) for c in setup.fake.embed_calls) <= 4 and setup.fake.embed_calls[0]["dimensions"] == 128


def test_forced_stop_then_a_new_worker_resumes_without_repeating_work(setup):
    text = topic_markdown("astronomy", 8, 4)
    expected = expected_chunks(setup, text)
    job = queue(setup, "big.md", text)
    setup.fake.embed_delay_s = 1.0  # hold each batch long enough to kill the worker while one is in flight
    w1 = setup.start(tag="first")
    wait_for(lambda: len(setup.fake.embed_calls) >= 3, what="the third batch to arrive")
    w1.kill()  # hard kill while the third batch is being embedded
    partial = setup.store.get_job(job["id"])
    assert partial["status"] == "running" and partial["claimed_by"]  # the dead worker's lease is still on the job
    doc = setup.store.find_document(partial["collection_id"], "docs", "big.md")
    assert doc["status"] == "ingesting" and doc["chunks_done"] == 8  # two committed batches of four
    assert setup.store.stats()["chunks"] == 8 == setup.store.stats()["vectors"]

    seen_before = len(setup.fake.embed_calls)
    setup.fake.embed_delay_s = 0.0
    setup.start(tag="second")
    done = wait_for(lambda: (j := setup.store.get_job(job["id"]))["status"] in ("completed", "failed") and j, what="resumed job")
    assert done["status"] == "completed" and done["attempts"] == 2
    after = [t for call in setup.fake.embed_calls[seen_before:] for t in call["input"]]
    assert len(after) == len(expected) - 8  # only the unfinished chunks were embedded again
    stored = [(c["ordinal"], c["text"], c["heading_path"]) for c in setup.store.list_chunks(doc["id"], 200)]
    assert stored == expected  # exactly what an uninterrupted run produces
    assert setup.store.stats()["chunks"] == setup.store.stats()["vectors"] == len(expected)


def test_a_file_that_keeps_killing_its_worker_is_eventually_failed(setup):
    settings = with_ingestion(setup.settings, max_attempts=2)
    job = queue(setup, "poison.md", topic_markdown("music", 4, 3))
    setup.fake.embed_hang_s = 60  # every batch hangs: the worker never gets past the first one
    for attempt in (1, 2):
        w = setup.start(settings, tag=f"a{attempt}")
        wait_for(lambda n=attempt: len(setup.fake.embed_calls) >= n, what=f"attempt {attempt}")
        wait_for(lambda: setup.store.get_job(job["id"])["attempts"] == attempt, what="attempt count")
        w.kill()
    setup.start(settings, tag="third")  # its start-up sweep sees a dead owner whose attempts are used up
    failed = wait_for(lambda: (j := setup.store.get_job(job["id"]))["status"] == "failed" and j, what="job failure")
    assert failed["error_code"] == "worker_died" and "2 times" in failed["error"] and "malformed" in failed["error"]


def test_watchdog_ends_a_worker_that_stops_making_progress(setup):
    settings = with_ingestion(setup.settings, watchdog_s=2.0)
    queue(setup, "hang.md", topic_markdown("music", 4, 3))
    setup.fake.embed_hang_s = 60  # looks exactly like a parser or embedder that never returns
    w = setup.start(settings, tag="dog")
    wait_for(lambda: len(setup.fake.embed_calls) >= 1, what="the first batch")
    code = wait_for(lambda: w.popen.poll(), timeout=15, what="the watchdog exit")
    assert code == 75  # WATCHDOG_EXIT_CODE


def test_embedding_service_errors_keep_the_checkpoint_and_a_requeue_resumes(setup):
    text = topic_markdown("astronomy", 6, 4)
    job = queue(setup, "a.md", text)
    setup.fake.embed_fail = 1000  # Ollama answers HTTP 503 to everything
    setup.start()
    failed = wait_for(lambda: (j := setup.store.get_job(job["id"]))["status"] == "failed" and j, what="job failure")
    assert failed["error_code"] == "embedding_error" and "503" in failed["error"]
    doc = setup.store.find_document(failed["collection_id"], "docs", "a.md")
    assert doc["status"] == "ingesting" and doc["chunks_done"] == 0
    setup.fake.embed_fail = 0
    again = queue(setup, "a.md")
    done = wait_for(lambda: (j := setup.store.get_job(again["id"]))["status"] in ("completed", "failed") and j, what="second job")
    assert done["status"] == "completed" and setup.store.stats()["chunks"] == setup.store.stats()["vectors"] > 0


def test_missing_embedding_model_is_reported_clearly(setup):
    job = queue(setup, "a.md", topic_markdown("music", 2, 3))
    setup.fake.embed_model_missing = True
    setup.start()
    failed = wait_for(lambda: (j := setup.store.get_job(job["id"]))["status"] == "failed" and j, what="job failure")
    assert failed["error_code"] == "embedding_model_missing" and "ollama pull" in failed["error"]


def test_jobs_run_one_at_a_time_in_queue_order(setup):
    jobs = [queue(setup, f"{n}.md", topic_markdown(t, 2, 2)) for n, t in enumerate(("music", "finance", "cooking"))]
    seen = []
    setup.fake.on_embed = lambda texts: seen.append(setup.store.running_jobs() and [j["id"] for j in setup.store.running_jobs()])
    setup.start()
    for j in jobs:
        wait_for(lambda j=j: setup.store.get_job(j["id"])["status"] == "completed", what=f"job {j['id']}")
    assert all(s is not None and len(s) <= 1 for s in seen)  # never two jobs running at once
    finished = [setup.store.get_job(j["id"])["finished_at"] for j in jobs]
    assert finished == sorted(finished)


def test_cancel_of_a_running_job_stops_the_worker_at_a_checkpoint(setup):
    job = queue(setup, "big.md", topic_markdown("astronomy", 8, 4))
    setup.fake.embed_delay_s = 0.3
    setup.start()
    wait_for(lambda: len(setup.fake.embed_calls) >= 2, what="two batches")
    setup.store.request_cancel(job["id"])
    done = wait_for(lambda: (j := setup.store.get_job(job["id"]))["status"] in ("cancelled", "completed") and j, what="cancellation")
    assert done["status"] == "cancelled"
    assert setup.store.stats()["chunks"] == 0 == setup.store.stats()["vectors"]
    doc = setup.store.find_document(done["collection_id"], "docs", "big.md")
    assert doc["status"] == "cancelled"

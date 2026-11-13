"""Atomicity of the collection change log under a paused source write."""

import json
import threading
from pathlib import Path

from app import repository
from app.schemas import OCRPageInput

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo.json"
CORRECTED = ROOT / "samples" / "page-002-corrected.json"


def test_change_log_is_invisible_until_the_source_commit(tmp_path, monkeypatch):
    db_path = tmp_path / "archive.sqlite3"
    repository.initialize_database(db_path)
    repository.seed_demo_if_empty(db_path, DEMO)

    head_before = repository.health_counts(db_path)["collection_commits"]

    corrected = OCRPageInput.model_validate(
        json.loads(CORRECTED.read_text(encoding="utf-8"))
    )

    original_record = repository._record_collection_commit
    started = threading.Event()
    release = threading.Event()
    errors = []

    def paused_record(conn, *args, **kwargs):
        started.set()
        if not release.wait(timeout=10):
            raise AssertionError("writer was not released")
        return original_record(conn, *args, **kwargs)

    monkeypatch.setattr(repository, "_record_collection_commit", paused_record)

    def writer():
        try:
            repository.upsert_page_ocr(db_path, "page-002", corrected)
        except BaseException as exc:  # pragma: no branch - surfaced below
            errors.append(exc)

    thread = threading.Thread(target=writer, daemon=True)
    thread.start()
    assert started.wait(timeout=10)

    # From another connection the uncommitted change log, OCR and index are
    # all invisible: readers keep seeing the pre-correction snapshot.
    counts = repository.health_counts(db_path)
    assert counts["collection_commits"] == head_before
    assert (
        repository.search_phrase(
            db_path, "the lighthouse keeper climbed"
        )["count"]
        == 1
    )

    release.set()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []

    counts = repository.health_counts(db_path)
    assert counts["collection_commits"] == head_before + 1
    assert (
        repository.search_phrase(
            db_path, "the lighthouse keeper walked"
        )["count"]
        == 1
    )


def test_failed_write_leaves_no_change_log(tmp_path):
    db_path = tmp_path / "archive.sqlite3"
    repository.initialize_database(db_path)
    repository.seed_demo_if_empty(db_path, DEMO)

    head_before = repository.health_counts(db_path)["collection_commits"]
    payload = OCRPageInput.model_validate(
        json.loads(
            (ROOT / "samples" / "page-002-missing-block.json").read_text(
                encoding="utf-8"
            )
        )
    )

    try:
        repository.upsert_page_ocr(db_path, "page-002", payload)
    except repository.DomainError:
        pass
    else:  # pragma: no cover
        raise AssertionError("expected the missing-block rejection")

    assert (
        repository.health_counts(db_path)["collection_commits"] == head_before
    )

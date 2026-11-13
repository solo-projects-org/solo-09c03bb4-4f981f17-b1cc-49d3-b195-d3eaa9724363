"""Collection API: first full load, then corrections only.

Covers batching, the full-to-incremental handoff, immutability across
mid-run corrections and restarts, commit grouping, late OCR, and the
atomic change log.
"""

import json
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.main import create_app
from app import repository

ROOT = Path(__file__).resolve().parents[1]
DEMO = ROOT / "samples" / "demo.json"
CORRECTED = ROOT / "samples" / "page-002-corrected.json"


@pytest.fixture()
def client(tmp_path):
    db_path = tmp_path / "archive.sqlite3"
    app = create_app(
        database_path=db_path,
        sample_path=DEMO,
        seed_demo=True,
    )
    with TestClient(app) as test_client:
        yield test_client, db_path


def load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def open_batch(client: TestClient, **params) -> dict:
    response = client.post("/api/collection/batches", params=params)
    assert response.status_code == 201, response.text
    return response.json()


def page(client: TestClient, batch_id: str, page_no: int) -> dict:
    response = client.get(
        f"/api/collection/batches/{batch_id}/pages/{page_no}"
    )
    assert response.status_code == 200, response.text
    return response.json()


def all_pages(client: TestClient, batch: dict) -> list[dict]:
    return [
        page(client, batch["batch_id"], n)
        for n in range(1, batch["total_pages"] + 1)
    ]


def articles_in(batch_pages: list[dict]) -> dict[str, dict]:
    found = {}
    for pg in batch_pages:
        for group in pg["groups"]:
            for article in group["articles"]:
                found[article["article_id"]] = article
    return found


def word_texts(article: dict) -> list[str]:
    return [word["text"] for word in article["words"]]


def test_full_batch_contains_every_ocr_complete_article(client):
    c, _ = client
    batch = open_batch(c, mode="full", page_size=2)
    assert batch["mode"] == "full"
    assert batch["total_groups"] == 3
    assert batch["total_pages"] == 2
    assert batch["anchor_commit_seq"] == 3
    assert batch["after_commit_seq"] is None

    pages = all_pages(c, batch)
    # page_size counts groups; a group is never split across pages.
    assert [pg["group_count"] for pg in pages] == [2, 1]
    assert pages[0]["next_page"] == 2
    assert pages[0]["continuation"] is None
    assert pages[-1]["is_final_page"] is True

    articles = articles_in(pages)
    assert sorted(articles) == ["A-100", "A-200", "A-300"]
    # Full-load groups are virtual commit 0, one article per group.
    assert {group["commit_seq"] for pg in pages for group in pg["groups"]} == {0}

    a100 = articles["A-100"]
    assert a100["title"] == "The Lighthouse Keeper"
    texts = word_texts(a100)
    assert texts[:3] == ["HARBOR", "GAZETTE", "ARCHIVE"]
    assert texts[-1] == "dawn"
    # Words carry page, block, ordinal and box in catalog/OCR order.
    first = a100["words"][0]
    assert first["page_id"] == "page-001"
    assert first["block_no"] == 1
    assert first["bbox"] == {"x0": 100.0, "y0": 120.0, "x1": 215.0, "y1": 155.0}

    # The continuation token is only handed back on the final page.
    assert pages[-1]["continuation"] == batch["continuation"]
    assert batch["continuation"].startswith("cur_")


def test_full_batch_immutable_when_correction_lands_mid_collection(client):
    c, _ = client
    batch = open_batch(c, mode="full", page_size=1)
    first_page = page(c, batch["batch_id"], 1)

    # A correction arrives while the collector still has pages to read.
    correction = c.put(
        "/api/pages/page-002/ocr", json=load_json(CORRECTED)
    )
    assert correction.status_code == 200

    # Every page of the old batch still returns the snapshot taken at request.
    for n in range(1, batch["total_pages"] + 1):
        again = page(c, batch["batch_id"], n)
        assert again["groups"] == page(c, batch["batch_id"], n)["groups"]
    assert page(c, batch["batch_id"], 1)["groups"] == first_page["groups"]

    articles = articles_in(all_pages(c, batch))
    texts = word_texts(articles["A-100"])
    assert "climbed" in texts
    assert "walked" not in texts

    # The search API, in contrast, already serves the corrected content.
    search = c.get(
        "/api/search/phrase", params={"q": "the lighthouse keeper walked"}
    ).json()
    assert search["count"] == 1


def test_incremental_batch_returns_only_commits_after_cursor(client):
    c, _ = client
    full = open_batch(c, mode="full", page_size=5)
    cursor = page(c, full["batch_id"], full["total_pages"])["continuation"]

    correction = c.put(
        "/api/pages/page-002/ocr", json=load_json(CORRECTED)
    ).json()
    assert correction["affected_articles"] == ["A-100", "A-200"]

    incr = open_batch(
        c, mode="incremental", page_size=10, continuation=cursor
    )
    assert incr["mode"] == "incremental"
    assert incr["after_commit_seq"] == full["anchor_commit_seq"]
    assert incr["total_groups"] == 1
    payload = page(c, incr["batch_id"], 1)
    group = payload["groups"][0]

    # One OCR commit touching two articles is one group, never split.
    assert [a["article_id"] for a in group["articles"]] == ["A-100", "A-200"]
    by_id = {a["article_id"]: a for a in group["articles"]}
    assert "walked" in word_texts(by_id["A-100"])
    assert "climbed" not in word_texts(by_id["A-100"])
    assert "supplement" in word_texts(by_id["A-200"])
    assert payload["continuation"] == incr["continuation"]


def test_full_to_incremental_handoff_loses_no_correction(client):
    c, _ = client
    # Correction committed before the full batch: included in the full load.
    first = open_batch(c, mode="full", page_size=5)
    c.put("/api/pages/page-002/ocr", json=load_json(CORRECTED))
    cursor = page(c, first["batch_id"], first["total_pages"])["continuation"]

    # Two further corrections after the handoff position.
    c.put("/api/catalog/articles/A-300", json={
        "title": "Weather Note Revised",
        "blocks": [{"page_id": "page-001", "block_no": 3}],
    })
    c.put("/api/pages/page-002/ocr", json=load_json(CORRECTED))

    incr = open_batch(
        c, mode="incremental", page_size=1, continuation=cursor
    )
    pages = all_pages(c, incr)
    commit_seqs = [
        group["commit_seq"] for pg in pages for group in pg["groups"]
    ]
    # Strict commit order, one group per commit.
    assert commit_seqs == sorted(commit_seqs)
    assert len(commit_seqs) == 3

    touched = {
        article["article_id"]
        for pg in pages
        for group in pg["groups"]
        for article in group["articles"]
    }
    # The page correction rebuilds A-100 and A-200; the catalog revision
    # re-emits A-300 with its new title.
    assert touched == {"A-100", "A-200", "A-300"}
    weather = [
        article
        for pg in pages
        for group in pg["groups"]
        for article in group["articles"]
        if article["article_id"] == "A-300"
    ][0]
    assert weather["title"] == "Weather Note Revised"

    # The next incremental round from the new cursor sees nothing left.
    new_cursor = pages[-1]["continuation"]
    empty = open_batch(
        c, mode="incremental", page_size=10, continuation=new_cursor
    )
    assert empty["total_groups"] == 0
    empty_page = page(c, empty["batch_id"], 1)
    assert empty_page["groups"] == []
    assert empty_page["is_final_page"] is True
    assert empty_page["continuation"] == new_cursor
    assert c.get(
        f"/api/collection/batches/{empty['batch_id']}/pages/2"
    ).status_code == 404


def test_group_content_is_fixed_at_its_commit_not_later_versions(client):
    c, _ = client
    full = open_batch(c, mode="full", page_size=5)
    cursor = page(c, full["batch_id"], full["total_pages"])["continuation"]

    # First correction: walked. Open the incremental batch immediately.
    c.put("/api/pages/page-002/ocr", json=load_json(CORRECTED))
    incr = open_batch(
        c, mode="incremental", page_size=5, continuation=cursor
    )
    snapshot_before = page(c, incr["batch_id"], 1)["groups"]

    # A second, different version of the same page arrives later.
    second = load_json(CORRECTED)
    second["blocks"][0]["words"][0]["text"] = "marched"
    c.put("/api/pages/page-002/ocr", json=second)

    # The materialized batch still reports the first correction's content.
    snapshot_after = page(c, incr["batch_id"], 1)["groups"]
    assert snapshot_after == snapshot_before
    a100 = [
        a for group in snapshot_after for a in group["articles"]
        if a["article_id"] == "A-100"
    ][0]
    texts = word_texts(a100)
    assert "walked" in texts
    assert "marched" not in texts

    # And the newer version is delivered once, in the next incremental round.
    next_cursor = page(
        c, incr["batch_id"], incr["total_pages"]
    )["continuation"]
    follow_up = open_batch(
        c, mode="incremental", page_size=5, continuation=next_cursor
    )
    group = page(c, follow_up["batch_id"], 1)["groups"][0]
    a100_v2 = [a for a in group["articles"] if a["article_id"] == "A-100"][0]
    assert "marched" in word_texts(a100_v2)
    assert "walked" not in word_texts(a100_v2)


def test_pending_ocr_article_is_collected_only_when_completed(client):
    c, _ = client
    # Catalog first, OCR missing: excluded from the full load.
    catalog = c.put(
        "/api/catalog/articles/A-500",
        json={
            "title": "Late OCR",
            "blocks": [{"page_id": "page-005", "block_no": 1}],
        },
    )
    assert catalog.json()["status"] == "pending_ocr"

    full = open_batch(c, mode="full", page_size=5)
    assert "A-500" not in articles_in(all_pages(c, full))
    cursor = page(c, full["batch_id"], full["total_pages"])["continuation"]

    # The OCR completion makes it searchable and collectable in one commit.
    ocr = c.put(
        "/api/pages/page-005/ocr",
        json={
            "width": 800,
            "height": 1000,
            "blocks": [{
                "block_no": 1,
                "words": [
                    {"text": "Catalog", "bbox": [10, 20, 90, 50]},
                    {"text": "first", "bbox": [100, 20, 155, 50]},
                ],
            }],
        },
    )
    assert ocr.json()["affected_articles"] == ["A-500"]

    incr = open_batch(
        c, mode="incremental", page_size=5, continuation=cursor
    )
    group = page(c, incr["batch_id"], 1)["groups"][0]
    assert [a["article_id"] for a in group["articles"]] == ["A-500"]
    assert word_texts(group["articles"][0]) == ["Catalog", "first"]


def test_batch_pages_are_replayable_after_restart(client):
    c, db_path = client
    batch = open_batch(c, mode="full", page_size=1)
    first = page(c, batch["batch_id"], 1)
    batch_id = batch["batch_id"]

    # Simulate a process restart: a brand-new app over the same SQLite volume.
    restarted = create_app(
        database_path=db_path, sample_path=DEMO, seed_demo=True
    )
    with TestClient(restarted) as c2:
        again = page(c2, batch_id, 1)
        assert again["groups"] == first["groups"]
        info = c2.get(f"/api/collection/batches/{batch_id}").json()
        assert info["batch_id"] == batch_id
        assert info["continuation"] == batch["continuation"]
        # Repeated reads of the same page stay identical.
        assert page(c2, batch_id, 2)["groups"] == page(c2, batch_id, 2)["groups"]


def test_repeated_page_reads_are_identical(client):
    c, _ = client
    batch = open_batch(c, mode="full", page_size=2)
    first = page(c, batch["batch_id"], 1)
    for _ in range(3):
        assert page(c, batch["batch_id"], 1)["groups"] == first["groups"]


def test_batch_validation_errors(client):
    c, _ = client
    full = open_batch(c, mode="full")
    cursor = full["continuation"]

    # Incremental requires a token.
    r = c.post("/api/collection/batches", params={"mode": "incremental"})
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "continuation_required"

    # Full load rejects a token.
    r = c.post(
        "/api/collection/batches",
        params={"mode": "full", "continuation": cursor},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "continuation_not_allowed"

    # Garbage and foreign tokens are rejected.
    for bad in ("cur_bogus", "cur_Mg!!", "something-else"):
        r = c.post(
            "/api/collection/batches",
            params={"mode": "incremental", "continuation": bad},
        )
        assert r.status_code == 422
        assert r.json()["detail"]["code"] == "invalid_continuation"

    # A structurally valid token beyond the archive's history is rejected.
    future = repository.encode_cursor(999_999)
    r = c.post(
        "/api/collection/batches",
        params={"mode": "incremental", "continuation": future},
    )
    assert r.status_code == 422
    assert r.json()["detail"]["code"] == "unknown_continuation"

    # Unknown batch and out-of-range page.
    assert c.get(
        "/api/collection/batches/0123456789abcdef/pages/1"
    ).status_code == 404
    assert c.get(
        f"/api/collection/batches/{full['batch_id']}/pages/99"
    ).status_code == 404


def test_change_log_commits_or_rolls_back_with_source_and_index(client):
    c, db_path = client
    # A rejected OCR retransmission must not leave a collection commit either.
    before = c.get("/health").json()["collection_commits"]
    r = c.put(
        "/api/pages/page-002/ocr",
        json=load_json(ROOT / "samples" / "page-002-missing-block.json"),
    )
    assert r.status_code == 409
    assert c.get("/health").json()["collection_commits"] == before

    # The log is stored in the same SQLite database as source and index.
    with repository._connection(db_path) as conn:
        log_tables = {
            row[0]
            for row in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
    assert {
        "collection_commits",
        "collection_commit_articles",
        "collection_batches",
        "collection_batch_groups",
    } <= log_tables


def test_existing_data_volume_can_start_a_first_collection(client):
    c, _ = client
    # The demo corpus is seeded before any collection batch exists; the first
    # full load over the existing volume works without reseeding.
    listing = c.get("/api/collection/batches").json()
    assert listing["count"] == 0
    batch = open_batch(c, mode="full")
    assert batch["total_groups"] == 3
    assert c.get("/api/collection/batches").json()["count"] == 1


def test_collection_runs_while_search_and_ingestion_keep_working(client):
    c, _ = client
    full = open_batch(c, mode="full", page_size=1)
    # Read part of the batch, then keep using the ordinary APIs concurrently.
    page(c, full["batch_id"], 1)
    search_before = c.get(
        "/api/search/phrase",
        params={"q": "the lighthouse keeper climbed"},
    ).json()["count"]
    assert search_before == 1

    c.put("/api/pages/page-002/ocr", json=load_json(CORRECTED))
    assert c.get(
        "/api/search/phrase",
        params={"q": "the lighthouse keeper walked"},
    ).json()["count"] == 1
    assert c.get("/api/articles/A-100").status_code == 200
    assert c.get("/api/pages/page-002").status_code == 200

    # The unfinished batch still pages to the end and yields the cursor.
    last = page(c, full["batch_id"], full["total_pages"])
    assert last["is_final_page"] is True
    assert last["continuation"] == full["continuation"]

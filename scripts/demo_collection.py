#!/usr/bin/env python3
"""End-to-end collection demo against a running archive service.

Scenario (mirrors the Swagger walkthrough in README.md):

1. Open the first full-load batch and page through it with ``page_size=1``.
2. Halfway through, a page OCR correction is committed via the normal
   ingestion API while collection is in progress.
3. The old batch still returns the frozen first-load content; finish paging
   it and take the continuation token from its final page.
4. Open an incremental batch from that token: the correction is delivered as
   one commit group (the two affected articles stay together), with content
   fixed at the correction commit.
5. A second incremental round confirms there is nothing left to collect.

Usage::

    python scripts/demo_collection.py                       # localhost:8000
    python scripts/demo_collection.py http://localhost:8000

The script only calls HTTP endpoints, so it works against the Docker
Compose service or a local ``uvicorn`` process.
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

BASE_URL = sys.argv[1].rstrip("/") if len(sys.argv) > 1 else "http://localhost:8000"
ROOT = Path(__file__).resolve().parents[1]
CORRECTED = ROOT / "samples" / "page-002-corrected.json"


def request(method: str, path: str, params: dict[str, Any] | None = None,
            body: Any = None) -> Any:
    url = BASE_URL + path
    if params:
        from urllib.parse import urlencode

        url = f"{url}?{urlencode(params)}"
    data = None
    headers = {}
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8"))
            print(f"{method} {path} -> {response.status}")
            return payload
    except urllib.error.HTTPError as exc:  # pragma: no cover - demo helper
        detail = exc.read().decode("utf-8")
        raise SystemExit(f"{method} {path} failed: {exc.code} {detail}") from exc


def main() -> None:
    print(f"# Collection demo against {BASE_URL}\n")

    print("## 1. First full load, one article group per page")
    full = request("POST", "/api/collection/batches",
                   {"mode": "full", "page_size": 1})
    batch_id = full["batch_id"]
    print(f"   batch={batch_id} groups={full['total_groups']} "
          f"pages={full['total_pages']} anchor_commit={full['anchor_commit_seq']}")

    first_pages = []
    for page_no in range(1, full["total_pages"] + 1):
        pg = request("GET", f"/api/collection/batches/{batch_id}/pages/{page_no}")
        for group in pg["groups"]:
            for article in group["articles"]:
                first_pages.append(article["article_id"])
                texts = [w["text"] for w in article["words"]]
                print(f"   page {page_no}: {article['article_id']} "
                      f"({len(article['words'])} words, first={texts[0]!r})")
        if page_no == 1:
            print("\n## 2. Correction committed while collection is in progress")
            correction = request(
                "PUT", "/api/pages/page-002/ocr",
                body=json.loads(CORRECTED.read_text(encoding="utf-8")),
            )
            print(f"   affected_articles={correction['affected_articles']} "
                  f"revision={correction['revision']}")

    print("\n## 3. Finish the frozen full load and take the continuation")
    last = request("GET", f"/api/collection/batches/{batch_id}/pages/"
                          f"{full['total_pages']}")
    cursor = last["continuation"]
    print(f"   collected={first_pages} continuation={cursor}")

    print("\n## 4. Incremental batch: only the correction, grouped by commit")
    incr = request("POST", "/api/collection/batches",
                   {"mode": "incremental", "page_size": 50,
                    "continuation": cursor})
    incr_page = request("GET", f"/api/collection/batches/{incr['batch_id']}/pages/1")
    for group in incr_page["groups"]:
        ids = [a["article_id"] for a in group["articles"]]
        print(f"   commit {group['commit_seq']} group={ids} (not split)")
        for article in group["articles"]:
            texts = [w["text"] for w in article["words"]]
            marker = "walked" if "walked" in texts else "supplement"
            print(f"     {article['article_id']}: contains {marker!r}, "
                  f"words={len(article['words'])}")
    next_cursor = incr_page["continuation"]

    print("\n## 5. Following incremental round has nothing new")
    empty = request("POST", "/api/collection/batches",
                    {"mode": "incremental", "page_size": 50,
                     "continuation": next_cursor})
    empty_page = request("GET",
                         f"/api/collection/batches/{empty['batch_id']}/pages/1")
    print(f"   groups={empty_page['group_count']} "
          f"is_final_page={empty_page['is_final_page']} "
          f"continuation={empty_page['continuation']}")
    print("\nDemo finished: full load + one correction collected without gaps.")


if __name__ == "__main__":
    main()

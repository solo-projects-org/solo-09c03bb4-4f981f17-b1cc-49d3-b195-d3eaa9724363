from __future__ import annotations

import base64
import binascii
import json
import os
import sqlite3
import unicodedata
import uuid
from collections import defaultdict
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

from .schemas import CatalogArticleInput, OCRPageInput, OCRWordInput


SCHEMA = """
CREATE TABLE IF NOT EXISTS pages (
    page_id     TEXT PRIMARY KEY,
    image_url   TEXT,
    width       REAL,
    height      REAL,
    revision    INTEGER NOT NULL,
    updated_at  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_blocks (
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    ordinal     INTEGER NOT NULL,
    PRIMARY KEY (page_id, block_no),
    UNIQUE (page_id, ordinal),
    FOREIGN KEY (page_id) REFERENCES pages(page_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS page_words (
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    word_no     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    token       TEXT NOT NULL,
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    x1          REAL NOT NULL,
    y1          REAL NOT NULL,
    PRIMARY KEY (page_id, block_no, word_no),
    FOREIGN KEY (page_id, block_no)
        REFERENCES page_blocks(page_id, block_no) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_page_words_token ON page_words(token);

CREATE TABLE IF NOT EXISTS articles (
    article_id     TEXT PRIMARY KEY,
    title          TEXT,
    revision       INTEGER NOT NULL,
    updated_at     TEXT NOT NULL,
    indexed_words  INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS catalog_blocks (
    article_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    PRIMARY KEY (article_id, seq),
    UNIQUE (article_id, page_id, block_no),
    FOREIGN KEY (article_id) REFERENCES articles(article_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_catalog_blocks_page
    ON catalog_blocks(page_id, block_no);

CREATE TABLE IF NOT EXISTS article_index (
    article_id  TEXT NOT NULL,
    seq         INTEGER NOT NULL,
    page_id     TEXT NOT NULL,
    block_no    INTEGER NOT NULL,
    word_no     INTEGER NOT NULL,
    text        TEXT NOT NULL,
    token       TEXT NOT NULL,
    x0          REAL NOT NULL,
    y0          REAL NOT NULL,
    x1          REAL NOT NULL,
    y1          REAL NOT NULL,
    PRIMARY KEY (article_id, seq),
    FOREIGN KEY (article_id) REFERENCES articles(article_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_article_index_token ON article_index(token);
CREATE INDEX IF NOT EXISTS idx_article_index_article_seq
    ON article_index(article_id, seq);

-- ---------------------------------------------------------------------------
-- Collection log: every catalog/OCR commit that makes an OCR-complete article
-- searchable (or changes one) is recorded here, inside the same SQLite
-- transaction as the source data and search index changes.  The log plus the
-- immutable batch materialization below make every collection round repeatable.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS collection_commits (
    commit_seq   INTEGER PRIMARY KEY AUTOINCREMENT,
    reason       TEXT NOT NULL,
    ref_kind     TEXT NOT NULL,
    ref_id       TEXT NOT NULL,
    created_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS collection_commit_articles (
    commit_seq    INTEGER NOT NULL,
    article_id    TEXT NOT NULL,
    article_rev   INTEGER NOT NULL,
    title         TEXT,
    words_json    TEXT NOT NULL,
    PRIMARY KEY (commit_seq, article_id),
    FOREIGN KEY (commit_seq)
        REFERENCES collection_commits(commit_seq) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_commit_articles_seq
    ON collection_commit_articles(commit_seq);

-- A batch fixes the exact scope and content visible when it was requested.
-- All its groups are materialized up front, so later corrections never alter
-- any page, even after a restart.
CREATE TABLE IF NOT EXISTS collection_batches (
    batch_id          TEXT PRIMARY KEY,
    mode              TEXT NOT NULL CHECK (mode IN ('full', 'incremental')),
    page_size         INTEGER NOT NULL,
    anchor_commit_seq INTEGER NOT NULL,
    after_commit_seq  INTEGER,
    created_at        TEXT NOT NULL,
    continuation      TEXT
);

CREATE TABLE IF NOT EXISTS collection_batch_groups (
    batch_id     TEXT NOT NULL,
    group_seq    INTEGER NOT NULL,
    page_no      INTEGER NOT NULL,
    commit_seq   INTEGER NOT NULL,
    articles_json TEXT NOT NULL,
    PRIMARY KEY (batch_id, group_seq),
    FOREIGN KEY (batch_id)
        REFERENCES collection_batches(batch_id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_batch_groups_page
    ON collection_batch_groups(batch_id, page_no);
"""

MAX_PHRASE_TOKENS = 40
COLLECTION_MIN_PAGE_SIZE = 1
COLLECTION_MAX_PAGE_SIZE = 200
COLLECTION_DEFAULT_PAGE_SIZE = 50
COLLECTION_LIST_LIMIT = 100
CURSOR_PREFIX = "cur_"


class DomainError(Exception):
    """An expected error that should be returned as a structured HTTP error."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        **details: Any,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.details = details

    def payload(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.details}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_token(value: str) -> str:
    """Normalize case and Unicode width while keeping OCR token boundaries."""
    return unicodedata.normalize("NFKC", value).strip().casefold()


def _connect(db_path: str | os.PathLike[str]) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), timeout=30, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextmanager
def _connection(db_path: str | os.PathLike[str]) -> Iterator[sqlite3.Connection]:
    conn = _connect(db_path)
    try:
        yield conn
    finally:
        conn.close()


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # BEGIN IMMEDIATE serializes writers.  All source changes and every
    # affected article's index rows commit together, never partially.
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


@contextmanager
def _read_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    # A deferred read transaction gives a single SQLite snapshot for the whole
    # search (candidate positions, boxes, and article titles).
    conn.execute("BEGIN")
    try:
        yield
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    else:
        conn.execute("COMMIT")


def initialize_database(db_path: str | os.PathLike[str]) -> None:
    path = Path(db_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _connection(path) as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)


def _get_page_refs(
    conn: sqlite3.Connection, page_id: str
) -> tuple[set[int], list[str]]:
    rows = conn.execute(
        """
        SELECT DISTINCT article_id, block_no
        FROM catalog_blocks
        WHERE page_id = ?
        ORDER BY article_id, block_no
        """,
        (page_id,),
    ).fetchall()
    required = {int(row["block_no"]) for row in rows}
    affected = sorted({str(row["article_id"]) for row in rows})
    return required, affected


def _replace_page_source(
    conn: sqlite3.Connection,
    page_id: str,
    payload: OCRPageInput,
    blocks: list[tuple[int, list[OCRWordInput]]],
    updated_at: str,
) -> tuple[int, int]:
    old = conn.execute(
        "SELECT revision FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    revision = (int(old["revision"]) + 1) if old else 1

    if old:
        conn.execute(
            """
            UPDATE pages
            SET image_url = ?, width = ?, height = ?,
                revision = ?, updated_at = ?
            WHERE page_id = ?
            """,
            (
                payload.image_url,
                payload.width,
                payload.height,
                revision,
                updated_at,
                page_id,
            ),
        )
    else:
        conn.execute(
            """
            INSERT INTO pages
                (page_id, image_url, width, height, revision, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                page_id,
                payload.image_url,
                payload.width,
                payload.height,
                revision,
                updated_at,
            ),
        )

    # Children first; ordering is explicit because page block numbers need not
    # be contiguous or ascending in the source export.
    conn.execute("DELETE FROM page_words WHERE page_id = ?", (page_id,))
    conn.execute("DELETE FROM page_blocks WHERE page_id = ?", (page_id,))

    word_count = 0
    for ordinal, (block_no, words) in enumerate(blocks):
        conn.execute(
            "INSERT INTO page_blocks (page_id, block_no, ordinal) VALUES (?, ?, ?)",
            (page_id, block_no, ordinal),
        )
        rows = []
        for word_no, word in enumerate(words):
            token = normalize_token(word.text)
            rows.append(
                (
                    page_id,
                    block_no,
                    word_no,
                    word.text,
                    token,
                    word.bbox.x0,
                    word.bbox.y0,
                    word.bbox.x1,
                    word.bbox.y1,
                )
            )
        if rows:
            conn.executemany(
                """
                INSERT INTO page_words
                    (page_id, block_no, word_no, text, token, x0, y0, x1, y1)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows,
            )
        word_count += len(words)

    return revision, word_count


def _rebuild_article(conn: sqlite3.Connection, article_id: str) -> int:
    """Rebuild one article's flattened OCR sequence from current page OCR.

    An article is indexed only when every catalog-referenced block is present
    in OCR.  This makes catalog-before-OCR ingestion possible without exposing
    phrases that would silently jump over a missing block.
    """
    conn.execute("DELETE FROM article_index WHERE article_id = ?", (article_id,))

    incomplete = conn.execute(
        """
        SELECT 1
        FROM catalog_blocks AS cb
        LEFT JOIN page_blocks AS pb
          ON pb.page_id = cb.page_id AND pb.block_no = cb.block_no
        WHERE cb.article_id = ? AND pb.page_id IS NULL
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    if incomplete:
        conn.execute(
            "UPDATE articles SET indexed_words = 0 WHERE article_id = ?",
            (article_id,),
        )
        return 0

    rows = conn.execute(
        """
        SELECT pw.page_id, pw.block_no, pw.word_no, pw.text, pw.token,
               pw.x0, pw.y0, pw.x1, pw.y1
        FROM catalog_blocks AS cb
        JOIN page_words AS pw
          ON pw.page_id = cb.page_id
         AND pw.block_no = cb.block_no
        WHERE cb.article_id = ?
        ORDER BY cb.seq ASC, pw.word_no ASC
        """,
        (article_id,),
    ).fetchall()

    params = [
        (
            article_id,
            seq,
            row["page_id"],
            row["block_no"],
            row["word_no"],
            row["text"],
            row["token"],
            row["x0"],
            row["y0"],
            row["x1"],
            row["y1"],
        )
        for seq, row in enumerate(rows)
    ]
    if params:
        conn.executemany(
            """
            INSERT INTO article_index
                (article_id, seq, page_id, block_no, word_no,
                 text, token, x0, y0, x1, y1)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            params,
        )
    conn.execute(
        "UPDATE articles SET indexed_words = ? WHERE article_id = ?",
        (len(rows), article_id),
    )
    return len(rows)


def upsert_page_ocr(
    db_path: str | os.PathLike[str],
    page_id: str,
    payload: OCRPageInput,
) -> dict[str, Any]:
    page_id = page_id.strip()
    if not page_id:
        raise DomainError(422, "invalid_page_id", "page_id must not be blank")

    blocks = payload.grouped_blocks()
    provided = {block_no for block_no, _ in blocks}
    updated_at = utc_now()

    with _connection(db_path) as conn:
        with _write_transaction(conn):
            required, affected = _get_page_refs(conn, page_id)
            missing = sorted(required - provided)
            if missing:
                raise DomainError(
                    409,
                    "missing_catalog_blocks",
                    (
                        "OCR retransmission rejected: the page omits blocks that "
                        "catalog articles reference. No OCR or index change was "
                        "committed; the previous results remain queryable."
                    ),
                    page_id=page_id,
                    missing_blocks=missing,
                    affected_articles=affected,
                    unchanged=True,
                )

            revision, word_count = _replace_page_source(
                conn, page_id, payload, blocks, updated_at
            )

            for article_id in affected:
                _rebuild_article(conn, article_id)

            # The collectable change log commits in the same transaction as the
            # OCR source and the rebuilt search indexes.
            _record_collection_commit(
                conn,
                reason="page_ocr_retransmission",
                ref_kind="page",
                ref_id=page_id,
                affected_articles=affected,
                created_at=updated_at,
            )

            return {
                "page_id": page_id,
                "revision": revision,
                "updated_at": updated_at,
                "blocks_count": len(blocks),
                "words_count": word_count,
                "affected_articles": affected,
                "message": (
                    "Page OCR and all affected article indexes committed "
                    "atomically."
                ),
            }


def upsert_catalog_article(
    db_path: str | os.PathLike[str],
    article_id: str,
    payload: CatalogArticleInput,
) -> dict[str, Any]:
    article_id = article_id.strip()
    if not article_id:
        raise DomainError(422, "invalid_article_id", "article_id must not be blank")

    updated_at = utc_now()
    with _connection(db_path) as conn:
        with _write_transaction(conn):
            old = conn.execute(
                "SELECT revision FROM articles WHERE article_id = ?",
                (article_id,),
            ).fetchone()

            missing = []
            for ref in payload.blocks:
                exists = conn.execute(
                    """
                    SELECT 1 FROM page_blocks
                    WHERE page_id = ? AND block_no = ?
                    """,
                    (ref.page_id, ref.block_no),
                ).fetchone()
                if not exists:
                    missing.append(f"{ref.page_id}/{ref.block_no}")

            # A brand-new article may be cataloged before its OCR pages arrive;
            # it stays unindexed until complete.  Replacing an existing article
            # with missing OCR blocks is rejected so its old results are not
            # silently discarded.
            if missing and old:
                raise DomainError(
                    409,
                    "missing_ocr_blocks",
                    (
                        "Catalog replacement rejected because referenced OCR "
                        "blocks are not present. The previous catalog and index "
                        "remain unchanged."
                    ),
                    article_id=article_id,
                    missing_blocks=missing,
                    unchanged=True,
                )

            revision = (int(old["revision"]) + 1) if old else 1

            if old:
                conn.execute(
                    """
                    UPDATE articles
                    SET title = ?, revision = ?, updated_at = ?
                    WHERE article_id = ?
                    """,
                    (payload.title, revision, updated_at, article_id),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO articles
                        (article_id, title, revision, updated_at, indexed_words)
                    VALUES (?, ?, ?, ?, 0)
                    """,
                    (article_id, payload.title, revision, updated_at),
                )

            conn.execute(
                "DELETE FROM catalog_blocks WHERE article_id = ?", (article_id,)
            )
            conn.executemany(
                """
                INSERT INTO catalog_blocks
                    (article_id, seq, page_id, block_no)
                VALUES (?, ?, ?, ?)
                """,
                [
                    (article_id, seq, ref.page_id, ref.block_no)
                    for seq, ref in enumerate(payload.blocks)
                ],
            )

            _rebuild_article(conn, article_id)
            _record_collection_commit(
                conn,
                reason="catalog_article_upsert",
                ref_kind="article",
                ref_id=article_id,
                affected_articles=[article_id],
                created_at=updated_at,
            )
            return _article_info_from_conn(conn, article_id)


def _page_info_from_conn(conn: sqlite3.Connection, page_id: str) -> dict[str, Any]:
    page = conn.execute(
        "SELECT * FROM pages WHERE page_id = ?", (page_id,)
    ).fetchone()
    if not page:
        raise DomainError(404, "page_not_found", f"page {page_id!r} was not found")

    blocks = conn.execute(
        """
        SELECT pb.block_no, pb.ordinal, COUNT(pw.word_no) AS word_count
        FROM page_blocks AS pb
        LEFT JOIN page_words AS pw
          ON pw.page_id = pb.page_id AND pw.block_no = pb.block_no
        WHERE pb.page_id = ?
        GROUP BY pb.page_id, pb.block_no, pb.ordinal
        ORDER BY pb.ordinal
        """,
        (page_id,),
    ).fetchall()

    return {
        "page_id": page["page_id"],
        "image_url": page["image_url"],
        "width": page["width"],
        "height": page["height"],
        "revision": int(page["revision"]),
        "updated_at": page["updated_at"],
        "blocks": [
            {
                "block_no": int(row["block_no"]),
                "ordinal": int(row["ordinal"]),
                "word_count": int(row["word_count"]),
            }
            for row in blocks
        ],
    }


def get_page_info(
    db_path: str | os.PathLike[str], page_id: str
) -> dict[str, Any]:
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            return _page_info_from_conn(conn, page_id)


def _article_info_from_conn(
    conn: sqlite3.Connection, article_id: str
) -> dict[str, Any]:
    article = conn.execute(
        "SELECT * FROM articles WHERE article_id = ?", (article_id,)
    ).fetchone()
    if not article:
        raise DomainError(
            404, "article_not_found", f"article {article_id!r} was not found"
        )

    blocks = conn.execute(
        """
        SELECT page_id, block_no
        FROM catalog_blocks
        WHERE article_id = ?
        ORDER BY seq
        """,
        (article_id,),
    ).fetchall()
    incomplete = conn.execute(
        """
        SELECT 1
        FROM catalog_blocks AS cb
        LEFT JOIN page_blocks AS pb
          ON pb.page_id = cb.page_id AND pb.block_no = cb.block_no
        WHERE cb.article_id = ? AND pb.page_id IS NULL
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    return {
        "article_id": article["article_id"],
        "title": article["title"],
        "revision": int(article["revision"]),
        "updated_at": article["updated_at"],
        "status": "pending_ocr" if incomplete else "indexed",
        "indexed_words": int(article["indexed_words"]),
        "blocks": [
            {"page_id": row["page_id"], "block_no": int(row["block_no"])}
            for row in blocks
        ],
    }


def get_article_info(
    db_path: str | os.PathLike[str], article_id: str
) -> dict[str, Any]:
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            return _article_info_from_conn(conn, article_id)


def _candidate_starts(
    conn: sqlite3.Connection,
    tokens: list[str],
    article_id: str | None,
    limit: int,
) -> tuple[list[sqlite3.Row], bool]:
    """Find consecutive token runs with a bounded number of SQL self joins."""
    token_count = len(tokens)
    params: list[Any] = []

    if token_count == 1:
        sql = "SELECT article_id, seq FROM article_index WHERE token = ?"
        params.append(tokens[0])
    else:
        parts = ["SELECT i0.article_id, i0.seq FROM article_index AS i0"]
        for offset in range(1, token_count):
            parts.append(
                "JOIN article_index AS i{off} "
                "ON i{off}.article_id = i0.article_id "
                "AND i{off}.seq = i0.seq + {off} "
                "AND i{off}.token = ?".format(off=offset)
            )
            params.append(tokens[offset])
        parts.append("WHERE i0.token = ?")
        params.append(tokens[0])
        sql = "\n".join(parts)

    if article_id:
        sql += " AND article_id = ?" if token_count == 1 else " AND i0.article_id = ?"
        params.append(article_id)

    sql += (
        " ORDER BY article_id, seq" if token_count == 1 else " ORDER BY i0.article_id, i0.seq"
    )
    sql += " LIMIT ?"
    params.append(limit + 1)

    rows = conn.execute(sql, params).fetchall()
    return rows[:limit], len(rows) > limit


def search_phrase(
    db_path: str | os.PathLike[str],
    phrase: str,
    article_id: str | None = None,
    limit: int = 50,
) -> dict[str, Any]:
    tokens = [normalize_token(token) for token in phrase.split()]
    if not tokens:
        raise DomainError(422, "empty_phrase", "phrase must contain at least one token")
    if len(tokens) > MAX_PHRASE_TOKENS:
        raise DomainError(
            422,
            "phrase_too_long",
            f"phrase may contain at most {MAX_PHRASE_TOKENS} tokens",
        )
    if limit < 1 or limit > 200:
        raise DomainError(422, "invalid_limit", "limit must be between 1 and 200")

    article_id = article_id.strip() if article_id else None
    if article_id == "":
        article_id = None

    with _connection(db_path) as conn:
        with _read_transaction(conn):
            candidates, truncated = _candidate_starts(
                conn, tokens, article_id, limit
            )
            if not candidates:
                return {
                    "query": phrase,
                    "normalized_tokens": tokens,
                    "limit": limit,
                    "count": 0,
                    "truncated": False,
                    "hits": [],
                }

            article_ids = sorted({str(row["article_id"]) for row in candidates})
            title_rows = conn.execute(
                """
                SELECT article_id, title
                FROM articles
                WHERE article_id IN ({})
                """.format(", ".join("?" for _ in article_ids)),
                article_ids,
            ).fetchall()
            titles = {
                str(row["article_id"]): row["title"] for row in title_rows
            }

            needed: dict[str, set[int]] = defaultdict(set)
            for row in candidates:
                start = int(row["seq"])
                aid = str(row["article_id"])
                needed[aid].update(range(start, start + len(tokens)))

            word_rows = []
            for aid, seqs in needed.items():
                ordered = sorted(seqs)
                # Stay comfortably below SQLite's host-parameter limit.
                for offset in range(0, len(ordered), 500):
                    chunk = ordered[offset : offset + 500]
                    word_rows.extend(
                        conn.execute(
                            """
                            SELECT article_id, seq, page_id, block_no, word_no,
                                   text, token, x0, y0, x1, y1
                            FROM article_index
                            WHERE article_id = ?
                              AND seq IN ({})
                            ORDER BY seq
                            """.format(", ".join("?" for _ in chunk)),
                            [aid, *chunk],
                        ).fetchall()
                    )

            words_by_seq = {
                (str(row["article_id"]), int(row["seq"])): row
                for row in word_rows
            }

            hits = []
            for candidate in candidates:
                aid = str(candidate["article_id"])
                start = int(candidate["seq"])
                sequence = [
                    words_by_seq[(aid, seq)]
                    for seq in range(start, start + len(tokens))
                ]
                pages: list[str] = []
                for row in sequence:
                    page_id = str(row["page_id"])
                    if page_id not in pages:
                        pages.append(page_id)

                hits.append(
                    {
                        "article_id": aid,
                        "title": titles.get(aid),
                        "start_seq": start,
                        "pages": pages,
                        "words": [
                            {
                                "seq": int(row["seq"]),
                                "page_id": row["page_id"],
                                "block_no": int(row["block_no"]),
                                "word_no": int(row["word_no"]),
                                "text": row["text"],
                                "bbox": {
                                    "x0": row["x0"],
                                    "y0": row["y0"],
                                    "x1": row["x1"],
                                    "y1": row["y1"],
                                },
                            }
                            for row in sequence
                        ],
                    }
                )

            return {
                "query": phrase,
                "normalized_tokens": tokens,
                "limit": limit,
                "count": len(hits),
                "truncated": truncated,
                "hits": hits,
            }


def _is_article_indexed(conn: sqlite3.Connection, article_id: str) -> bool:
    """An article is collectable only when all its catalog blocks have OCR."""
    row = conn.execute(
        """
        SELECT 1
        FROM catalog_blocks AS cb
        LEFT JOIN page_blocks AS pb
          ON pb.page_id = cb.page_id AND pb.block_no = cb.block_no
        WHERE cb.article_id = ? AND pb.page_id IS NULL
        LIMIT 1
        """,
        (article_id,),
    ).fetchone()
    return row is None


def _article_snapshot(
    conn: sqlite3.Connection, article_id: str
) -> dict[str, Any]:
    """Serialize one OCR-complete article exactly as a commit made it.

    The flattened word order follows catalog block order and OCR word order,
    matching the search index.  Everything needed by the downstream full-text
    warehouse (id, title, ordered words and boxes) is captured.
    """
    article = conn.execute(
        "SELECT article_id, title, revision FROM articles WHERE article_id = ?",
        (article_id,),
    ).fetchone()
    if article is None:
        raise DomainError(
            404, "article_not_found", f"article {article_id!r} was not found"
        )

    word_rows = conn.execute(
        """
        SELECT ai.seq, ai.page_id, ai.block_no, ai.word_no,
               ai.text, ai.x0, ai.y0, ai.x1, ai.y1
        FROM article_index AS ai
        WHERE ai.article_id = ?
        ORDER BY ai.seq
        """,
        (article_id,),
    ).fetchall()

    return {
        "article_id": article["article_id"],
        "title": article["title"],
        "revision": int(article["revision"]),
        "words": [
            {
                "seq": int(row["seq"]),
                "page_id": row["page_id"],
                "block_no": int(row["block_no"]),
                "word_no": int(row["word_no"]),
                "text": row["text"],
                "bbox": {
                    "x0": row["x0"],
                    "y0": row["y0"],
                    "x1": row["x1"],
                    "y1": row["y1"],
                },
            }
            for row in word_rows
        ],
    }


def _record_collection_commit(
    conn: sqlite3.Connection,
    reason: str,
    ref_kind: str,
    ref_id: str,
    affected_articles: list[str],
    created_at: str,
) -> int | None:
    """Append the collectable changes of one source commit to the change log.

    Runs inside the caller's write transaction, so the source data, the search
    index and the collectable change become visible together or not at all.
    Only OCR-complete articles are recorded; an article whose OCR is completed
    later appears in a later commit.  A commit with no collectable article
    produces no log row.
    """
    snapshots = []
    for article_id in sorted(set(affected_articles)):
        if not _is_article_indexed(conn, article_id):
            continue
        snapshots.append(_article_snapshot(conn, article_id))
    if not snapshots:
        return None

    cursor = conn.execute(
        """
        INSERT INTO collection_commits (reason, ref_kind, ref_id, created_at)
        VALUES (?, ?, ?, ?)
        """,
        (reason, ref_kind, ref_id, created_at),
    )
    commit_seq = int(cursor.lastrowid)
    conn.executemany(
        """
        INSERT INTO collection_commit_articles
            (commit_seq, article_id, article_rev, title, words_json)
        VALUES (?, ?, ?, ?, ?)
        """,
        [
            (
                commit_seq,
                snapshot["article_id"],
                snapshot["revision"],
                snapshot["title"],
                json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")),
            )
            for snapshot in snapshots
        ],
    )
    return commit_seq


def encode_cursor(commit_seq: int) -> str:
    """Build the opaque continuation token for an anchored commit position."""
    raw = str(int(commit_seq)).encode("ascii")
    token = base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")
    return f"{CURSOR_PREFIX}{token}"


def decode_cursor(cursor: str) -> int:
    """Parse a continuation token, rejecting anything this service did not issue."""
    if not isinstance(cursor, str) or not cursor.startswith(CURSOR_PREFIX):
        raise DomainError(
            422,
            "invalid_continuation",
            "continuation is not a token issued by this service",
        )
    token = cursor[len(CURSOR_PREFIX) :]
    padded = token + "=" * (-len(token) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        value = int(raw.decode("ascii"))
    except (binascii.Error, UnicodeDecodeError, ValueError):
        raise DomainError(
            422,
            "invalid_continuation",
            "continuation is not a token issued by this service",
        )
    if value < 0:
        raise DomainError(
            422,
            "invalid_continuation",
            "continuation is not a token issued by this service",
        )
    return value


def _full_batch_groups(conn: sqlite3.Connection) -> list[tuple[int, str]]:
    """Every OCR-complete article as virtual commit 0, one article per group."""
    rows = conn.execute(
        """
        SELECT a.article_id
        FROM articles AS a
        WHERE a.indexed_words > 0
          AND NOT EXISTS (
                SELECT 1
                FROM catalog_blocks AS cb
                LEFT JOIN page_blocks AS pb
                  ON pb.page_id = cb.page_id AND pb.block_no = cb.block_no
                WHERE cb.article_id = a.article_id AND pb.page_id IS NULL
            )
        ORDER BY a.article_id
        """
    ).fetchall()
    groups = []
    for row in rows:
        snapshot = _article_snapshot(conn, str(row["article_id"]))
        payload = {
            "commit_seq": 0,
            "articles": [snapshot],
        }
        groups.append((0, json.dumps(payload, ensure_ascii=False, separators=(",", ":"))))
    return groups


def _incremental_batch_groups(
    conn: sqlite3.Connection, after_commit_seq: int
) -> list[tuple[int, str]]:
    """Change-log commits after the cursor position, in commit order.

    One commit is one group and is never split: every OCR/catalog change that
    the same source commit involved stays together and carries the content as
    of that commit, never a later version.
    """
    commit_rows = conn.execute(
        """
        SELECT commit_seq
        FROM collection_commits
        WHERE commit_seq > ?
        ORDER BY commit_seq
        """,
        (after_commit_seq,),
    ).fetchall()

    groups = []
    for commit_row in commit_rows:
        commit_seq = int(commit_row["commit_seq"])
        article_rows = conn.execute(
            """
            SELECT words_json
            FROM collection_commit_articles
            WHERE commit_seq = ?
            ORDER BY article_id
            """,
            (commit_seq,),
        ).fetchall()
        articles = [
            json.loads(str(row["words_json"])) for row in article_rows
        ]
        payload = {"commit_seq": commit_seq, "articles": articles}
        groups.append(
            (commit_seq, json.dumps(payload, ensure_ascii=False, separators=(",", ":")))
        )
    return groups


def create_collection_batch(
    db_path: str | os.PathLike[str],
    mode: str,
    page_size: int = COLLECTION_DEFAULT_PAGE_SIZE,
    continuation: str | None = None,
) -> dict[str, Any]:
    """Open an immutable, replayable collection round.

    * ``full``  - first load: every currently OCR-complete article, anchored at
      the latest committed change.
    * ``incremental`` - only commits after the continuation position.

    The whole scope is materialized in one write transaction, so paging,
    retries and restarts always return the same content even while OCR and
    cataloging keep committing corrections.
    """
    if mode not in {"full", "incremental"}:
        raise DomainError(422, "invalid_mode", "mode must be 'full' or 'incremental'")
    if not COLLECTION_MIN_PAGE_SIZE <= page_size <= COLLECTION_MAX_PAGE_SIZE:
        raise DomainError(
            422,
            "invalid_page_size",
            (
                f"page_size must be between {COLLECTION_MIN_PAGE_SIZE} and "
                f"{COLLECTION_MAX_PAGE_SIZE} groups"
            ),
        )

    after_commit_seq = None
    if mode == "incremental":
        if not continuation:
            raise DomainError(
                422,
                "continuation_required",
                "incremental batches require the continuation token of a prior batch",
            )
        after_commit_seq = decode_cursor(continuation)
    elif continuation:
        raise DomainError(
            422,
            "continuation_not_allowed",
            "full (first-load) batches do not accept a continuation token",
        )

    created_at = utc_now()
    batch_id = uuid.uuid4().hex
    with _connection(db_path) as conn:
        with _write_transaction(conn):
            head_row = conn.execute(
                "SELECT COALESCE(MAX(commit_seq), 0) AS head FROM collection_commits"
            ).fetchone()
            head_seq = int(head_row["head"])

            if mode == "full":
                anchor = head_seq
                groups = _full_batch_groups(conn)
            else:
                assert after_commit_seq is not None
                if after_commit_seq > head_seq:
                    raise DomainError(
                        422,
                        "unknown_continuation",
                        "continuation points beyond any commit known to this archive",
                    )
                anchor = head_seq
                groups = _incremental_batch_groups(conn, after_commit_seq)

            new_cursor = encode_cursor(anchor)
            conn.execute(
                """
                INSERT INTO collection_batches
                    (batch_id, mode, page_size, anchor_commit_seq,
                     after_commit_seq, created_at, continuation)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    batch_id,
                    mode,
                    page_size,
                    anchor,
                    after_commit_seq,
                    created_at,
                    new_cursor,
                ),
            )
            conn.executemany(
                """
                INSERT INTO collection_batch_groups
                    (batch_id, group_seq, page_no, commit_seq, articles_json)
                VALUES (?, ?, ?, ?, ?)
                """,
                [
                    (batch_id, seq, seq // page_size + 1, commit_seq, payload)
                    for seq, (commit_seq, payload) in enumerate(groups)
                ],
            )

    return get_collection_batch(db_path, batch_id)


def _batch_row_or_404(
    conn: sqlite3.Connection, batch_id: str
) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM collection_batches WHERE batch_id = ?",
        (batch_id,),
    ).fetchone()
    if row is None:
        raise DomainError(
            404, "batch_not_found", f"collection batch {batch_id!r} was not found"
        )
    return row


def get_collection_batch(
    db_path: str | os.PathLike[str], batch_id: str
) -> dict[str, Any]:
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            batch = _batch_row_or_404(conn, batch_id)
            total = conn.execute(
                "SELECT COUNT(*) AS n FROM collection_batch_groups WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()["n"]
            page_size = int(batch["page_size"])
            total_pages = (int(total) + page_size - 1) // page_size
            return {
                "batch_id": batch["batch_id"],
                "mode": batch["mode"],
                "page_size": page_size,
                "anchor_commit_seq": int(batch["anchor_commit_seq"]),
                "after_commit_seq": (
                    int(batch["after_commit_seq"])
                    if batch["after_commit_seq"] is not None
                    else None
                ),
                "total_groups": int(total),
                "total_pages": total_pages,
                "created_at": batch["created_at"],
                "continuation": batch["continuation"],
            }


def list_collection_batches(
    db_path: str | os.PathLike[str], limit: int = 50
) -> dict[str, Any]:
    limit = min(max(int(limit), 1), COLLECTION_LIST_LIMIT)
    with _connection(db_path) as conn:
        with _read_transaction(conn):
            rows = conn.execute(
                """
                SELECT b.*, COUNT(g.group_seq) AS total_groups
                FROM collection_batches AS b
                LEFT JOIN collection_batch_groups AS g
                  ON g.batch_id = b.batch_id
                GROUP BY b.batch_id
                ORDER BY b.rowid DESC
                LIMIT ?
                """,
                (limit,),
            ).fetchall()
            batches = []
            for row in rows:
                page_size = int(row["page_size"])
                total_groups = int(row["total_groups"])
                batches.append(
                    {
                        "batch_id": row["batch_id"],
                        "mode": row["mode"],
                        "page_size": page_size,
                        "anchor_commit_seq": int(row["anchor_commit_seq"]),
                        "after_commit_seq": (
                            int(row["after_commit_seq"])
                            if row["after_commit_seq"] is not None
                            else None
                        ),
                        "total_groups": total_groups,
                        "total_pages": (total_groups + page_size - 1) // page_size,
                        "created_at": row["created_at"],
                        "continuation": row["continuation"],
                    }
                )
            return {"count": len(batches), "limit": limit, "batches": batches}


def get_collection_page(
    db_path: str | os.PathLike[str], batch_id: str, page: int
) -> dict[str, Any]:
    """Return one immutable page; groups are never split across pages."""
    page = int(page)
    if page < 1:
        raise DomainError(422, "invalid_page", "page must be >= 1")

    with _connection(db_path) as conn:
        with _read_transaction(conn):
            batch = _batch_row_or_404(conn, batch_id)
            page_size = int(batch["page_size"])
            total_row = conn.execute(
                "SELECT COUNT(*) AS n FROM collection_batch_groups WHERE batch_id = ?",
                (batch_id,),
            ).fetchone()
            total_groups = int(total_row["n"])
            total_pages = (total_groups + page_size - 1) // page_size

            # An empty batch (e.g. an incremental round with no new commit)
            # serves page 1 as an empty final page carrying the cursor; any
            # other page does not exist.
            if page > max(total_pages, 1):
                raise DomainError(
                    404,
                    "page_not_found",
                    (
                        f"page {page} does not exist in batch {batch_id!r}; "
                        f"the batch has {total_pages} page(s)"
                    ),
                )

            group_rows = conn.execute(
                """
                SELECT commit_seq, articles_json
                FROM collection_batch_groups
                WHERE batch_id = ? AND page_no = ?
                ORDER BY group_seq
                """,
                (batch_id, page),
            ).fetchall()
            groups = [json.loads(str(row["articles_json"])) for row in group_rows]

            # An empty batch (e.g. an incremental round with no new commit)
            # still serves page 1 as an empty final page carrying the cursor.
            is_final = (total_pages == 0 and page == 1) or page == total_pages
            return {
                "batch_id": batch_id,
                "mode": batch["mode"],
                "page": page,
                "page_size": page_size,
                "total_groups": total_groups,
                "total_pages": total_pages,
                "group_count": len(groups),
                "groups": groups,
                "next_page": None if is_final else page + 1,
                "is_final_page": is_final,
                # The position token is handed back only at the end of the run;
                # intermediate pages keep paging with (batch_id, page).
                "continuation": batch["continuation"] if is_final else None,
                "anchor_commit_seq": int(batch["anchor_commit_seq"]),
            }


def health_counts(db_path: str | os.PathLike[str]) -> dict[str, Any]:
    with _connection(db_path) as conn:
        pages = conn.execute("SELECT COUNT(*) FROM pages").fetchone()[0]
        articles = conn.execute("SELECT COUNT(*) FROM articles").fetchone()[0]
        ocr_words = conn.execute("SELECT COUNT(*) FROM page_words").fetchone()[0]
        indexed_words = conn.execute("SELECT COUNT(*) FROM article_index").fetchone()[0]
        collection_commits = conn.execute(
            "SELECT COUNT(*) FROM collection_commits"
        ).fetchone()[0]
        collection_batches = conn.execute(
            "SELECT COUNT(*) FROM collection_batches"
        ).fetchone()[0]
    return {
        "status": "ok",
        "database": str(db_path),
        "pages": int(pages),
        "articles": int(articles),
        "ocr_words": int(ocr_words),
        "indexed_words": int(indexed_words),
        "collection_commits": int(collection_commits),
        "collection_batches": int(collection_batches),
    }


def seed_demo_if_empty(
    db_path: str | os.PathLike[str], sample_path: str | os.PathLike[str]
) -> bool:
    """Load the bundled local sample once into an empty database."""
    counts = health_counts(db_path)
    if counts["pages"] or counts["articles"]:
        return False

    with open(sample_path, "r", encoding="utf-8") as handle:
        sample = json.load(handle)

    for page_data in sample.get("pages", []):
        page_id = page_data["page_id"]
        page_payload = OCRPageInput.model_validate(page_data)
        upsert_page_ocr(db_path, page_id, page_payload)

    for article_data in sample.get("articles", []):
        article_id = article_data["article_id"]
        article_payload = CatalogArticleInput.model_validate(article_data)
        upsert_catalog_article(db_path, article_id, article_payload)

    return True

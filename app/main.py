from __future__ import annotations

import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Path as ApiPath, Query
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__
from .repository import (
    COLLECTION_DEFAULT_PAGE_SIZE,
    COLLECTION_MAX_PAGE_SIZE,
    COLLECTION_MIN_PAGE_SIZE,
    DomainError,
    create_collection_batch,
    get_article_info,
    get_collection_batch,
    get_collection_page,
    get_page_info,
    health_counts,
    initialize_database,
    list_collection_batches,
    search_phrase,
    seed_demo_if_empty,
    upsert_catalog_article,
    upsert_page_ocr,
)
from .schemas import (
    ArticleInfo,
    BatchInfo,
    BatchListResponse,
    BatchPageResponse,
    CatalogArticleInput,
    HealthResponse,
    OCRPageInput,
    OCRUpdateResponse,
    PageInfo,
    PhraseSearchResponse,
)

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLE_PATH = PROJECT_ROOT / "samples" / "demo.json"
SCANS_DIR = PROJECT_ROOT / "static" / "scans"

DESCRIPTION = """
按 OCR 分词检索报刊文章中的连续英文词组，并返回命中处每个词的版面与字框。
Phrase search over OCR tokens, with per-word page and image-box locations.

### 数据语义

* 编目先给出文章的有序块引用 `page_id + block_no`；OCR 按版面提供块内的词和字框。
* 查询词组必须是 **连续 OCR 词元**，忽略大小写；可跨块、可跨版面，但不会跨文章。
* 每处命中返回 `article_id`，以及词组中每个词的 `page_id`、`block_no` 和 `bbox`。
* `bbox` 输出为图像像素坐标 `x0, y0, x1, y1`；输入也接受 `[x0,y0,x1,y1]` 或 `{x,y,width,height}`。

### 本地样例（启动后自动装入空数据库）

1. `GET /api/search/phrase?q=the lighthouse keeper climbed`
   返回 A-100；`words` 先落在 `page-001`，再跨到 `page-002`。
2. `PUT /api/pages/page-002/ocr`
   Swagger 已预填 `samples/page-002-corrected.json` 的更正内容；执行后：
   * 旧查询 `the lighthouse keeper climbed` 无结果
   * 新查询 `the lighthouse keeper walked` 命中，仍跨两个版面
   * `only in the archive edition` 变为无结果，`only in the archive supplement` 命中
3. 只提交 `page-002` 的块 1（缺少编目引用的块 2）时，整个更正返回 409，
   数据库与更正前索引都不变，旧查询仍可执行。

单版 OCR 重传会在一个 SQLite `BEGIN IMMEDIATE` 事务中替换该版 OCR，并只重建
引用该版面的文章索引。搜索使用同一读快照，因此并发查询只会看到更正前或更正后的
完整结果，不会看到半重建状态。

### 全文库采集（首次全量，之后只采更正）

1. `POST /api/collection/batches?mode=full&page_size=1` 开全量批号，逐页
   `GET /api/collection/batches/{batch_id}/pages/{page}` 取回 OCR 齐全文章的
   编号、标题和带字框的有序词；批号内容在创建时固化，中途更正不改变任何一页。
2. 采到一半时照常 `PUT /api/pages/page-002/ocr` 提交更正，OCR、编目、检索不受影响。
3. 取完全量末页拿到不透明 `continuation`，再
   `POST /api/collection/batches?mode=incremental&continuation=...` 开增量批号：
   只返回该位置之后的提交，一次提交涉及的文章同组、不拆页，内容以那次提交为准。
   凭 `batch_id` 重取或重启后续采结果不变。
"""


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


def create_app(
    database_path: str | os.PathLike[str] | None = None,
    sample_path: str | os.PathLike[str] | None = None,
    seed_demo: bool | None = None,
) -> FastAPI:
    db_path = str(
        database_path
        or os.getenv("DATABASE_PATH", "data/archive.sqlite3")
    )
    demo_file = Path(
        sample_path
        or os.getenv("DEMO_DATA_PATH", str(DEFAULT_SAMPLE_PATH))
    )
    should_seed = _env_bool("SEED_DEMO", True) if seed_demo is None else seed_demo

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        initialize_database(db_path)
        if should_seed:
            seed_demo_if_empty(db_path, demo_file)
        yield

    app = FastAPI(
        title="Newspaper Phrase Locator",
        version=__version__,
        description=DESCRIPTION,
        lifespan=lifespan,
        openapi_tags=[
            {"name": "pages", "description": "OCR page ingestion and inspection"},
            {"name": "catalog", "description": "Article block order from cataloging"},
            {"name": "search", "description": "Case-insensitive OCR phrase search"},
            {
                "name": "collection",
                "description": (
                    "Immutable full-load and incremental collection rounds for the "
                    "unified full-text warehouse"
                ),
            },
            {"name": "ops", "description": "Health and documentation"},
        ],
    )
    if SCANS_DIR.is_dir():
        app.mount("/scans", StaticFiles(directory=SCANS_DIR), name="scans")

    app.state.database_path = db_path
    app.state.sample_path = str(demo_file)

    @app.exception_handler(DomainError)
    async def domain_error_handler(_request, exc: DomainError) -> JSONResponse:
        return JSONResponse(status_code=exc.status_code, content={"detail": exc.payload()})

    @app.get("/", include_in_schema=False)
    def root() -> RedirectResponse:
        return RedirectResponse(url="/docs")

    @app.get(
        "/health",
        response_model=HealthResponse,
        tags=["ops"],
        summary="Health and row counts",
    )
    def health() -> dict:
        return health_counts(db_path)

    @app.put(
        "/api/pages/{page_id}/ocr",
        response_model=OCRUpdateResponse,
        tags=["pages"],
        summary="Replace one page's OCR and rebuild only affected article indexes",
    )
    def put_page_ocr(
        payload: OCRPageInput,
        page_id: str = ApiPath(
            ...,
            min_length=1,
            max_length=128,
            examples=["page-002"],
            description="Layout page identifier shared by catalog and OCR",
        ),
    ) -> dict:
        return upsert_page_ocr(db_path, page_id, payload)

    @app.get(
        "/api/pages/{page_id}",
        response_model=PageInfo,
        tags=["pages"],
        summary="Inspect a page's OCR revision and blocks",
    )
    def get_page(page_id: str = ApiPath(..., min_length=1, examples=["page-002"])) -> dict:
        return get_page_info(db_path, page_id)

    @app.put(
        "/api/catalog/articles/{article_id}",
        response_model=ArticleInfo,
        tags=["catalog"],
        summary="Create or replace an article's ordered block references",
    )
    def put_article(
        payload: CatalogArticleInput,
        article_id: str = ApiPath(
            ...,
            min_length=1,
            max_length=128,
            examples=["A-100"],
        ),
    ) -> dict:
        return upsert_catalog_article(db_path, article_id, payload)

    @app.get(
        "/api/articles/{article_id}",
        response_model=ArticleInfo,
        tags=["catalog"],
        summary="Inspect catalog blocks and indexed word count",
    )
    def get_article(
        article_id: str = ApiPath(..., min_length=1, examples=["A-100"])
    ) -> dict:
        return get_article_info(db_path, article_id)

    @app.get(
        "/api/search/phrase",
        response_model=PhraseSearchResponse,
        tags=["search"],
        summary="Find case-insensitive consecutive OCR token phrases",
    )
    @app.get(
        "/api/search",
        response_model=PhraseSearchResponse,
        tags=["search"],
        include_in_schema=False,
    )
    def search(
        q: str = Query(
            ...,
            min_length=1,
            max_length=1024,
            examples=["the lighthouse keeper climbed"],
            description=(
                "Whitespace-separated OCR tokens. Matching is consecutive, "
                "case-insensitive, and may cross page/block boundaries within "
                "one article."
            ),
        ),
        article_id: str | None = Query(
            default=None,
            max_length=128,
            description="Optional article filter for citation lookup",
        ),
        limit: int = Query(
            default=50,
            ge=1,
            le=200,
            description="Maximum number of phrase occurrences returned",
        ),
    ) -> dict:
        return search_phrase(db_path, q, article_id=article_id, limit=limit)

    # ------------------------------------------------------------------
    # Collection API: first full load, then corrections only
    # ------------------------------------------------------------------

    @app.post(
        "/api/collection/batches",
        response_model=BatchInfo,
        tags=["collection"],
        status_code=201,
        summary="Open an immutable full-load or incremental collection batch",
    )
    def post_collection_batch(
        mode: str = Query(
            ...,
            pattern="^(full|incremental)$",
            description=(
                "full = first load of every OCR-complete article; "
                "incremental = only corrections committed after continuation"
            ),
        ),
        page_size: int = Query(
            default=COLLECTION_DEFAULT_PAGE_SIZE,
            ge=COLLECTION_MIN_PAGE_SIZE,
            le=COLLECTION_MAX_PAGE_SIZE,
            description=(
                "Maximum number of groups per page. A group is the set of "
                "articles one commit touched; it is never split across pages."
            ),
        ),
        continuation: str | None = Query(
            default=None,
            description=(
                "Opaque token from the final page of a previous batch. "
                "Required for mode=incremental; forbidden for mode=full."
            ),
        ),
    ) -> dict:
        return create_collection_batch(
            db_path, mode, page_size=page_size, continuation=continuation
        )

    @app.get(
        "/api/collection/batches",
        response_model=BatchListResponse,
        tags=["collection"],
        summary="List collection batches (newest first), for restart recovery",
    )
    def get_collection_batches(
        limit: int = Query(default=50, ge=1, le=100),
    ) -> dict:
        return list_collection_batches(db_path, limit=limit)

    @app.get(
        "/api/collection/batches/{batch_id}",
        response_model=BatchInfo,
        tags=["collection"],
        summary="Inspect one batch and its continuation token",
    )
    def get_collection_batch_info(batch_id: str = ApiPath(..., min_length=8)) -> dict:
        return get_collection_batch(db_path, batch_id)

    @app.get(
        "/api/collection/batches/{batch_id}/pages/{page}",
        response_model=BatchPageResponse,
        tags=["collection"],
        summary="Fetch one immutable page of a batch, by batch id and page number",
    )
    def get_collection_batch_page(
        batch_id: str = ApiPath(..., min_length=8),
        page: int = ApiPath(..., ge=1),
    ) -> dict:
        return get_collection_page(db_path, batch_id, page)

    return app


app = create_app()

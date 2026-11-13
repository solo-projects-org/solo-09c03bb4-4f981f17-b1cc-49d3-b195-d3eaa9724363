# Newspaper Phrase Locator

报刊档案引文核对服务：把编目的文章块顺序和 OCR 词/字框建成 SQLite 索引，按连续英文词组定位到原图上的每一处位置。一句话即使跨块、跨版面，也能返回它在各版上的字框；命中不会跨文章。

## 启动

```bash
docker compose up --build
```

打开 **http://localhost:8000/docs**（Swagger UI）。服务启动时若数据库为空，会自动装入 `/app/samples/demo.json` 本地样例。

- SQLite 文件：`/data/archive.sqlite3`，保存在 Compose volume `archive-data`
- 健康检查：`GET /health`
- 重建演示数据：`docker compose down -v && docker compose up --build`

也可本地运行：

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements-dev.txt
uvicorn app.main:app --reload
pytest
```

## Swagger 演示

### 1. 跨版检索

在 Swagger 执行：

```text
GET /api/search/phrase?q=the lighthouse keeper climbed
```

返回 A-100 的一处命中。`words` 中 `keeper` 在 `page-001` 的 block 2，`climbed` 在 `page-002` 的 block 1；每个词都带 `page_id`、`block_no`、`bbox`。样例版面图可从 `image_url` 打开（例如 <http://localhost:8000/scans/page-001.svg>）。再试：

```text
GET /api/search/phrase?q=BULLETIN the lighthouse keeper climbed
```

这个短语同时跨 `page-001` 的两个块和版面边界。把 `article_id=A-100` 传入可只看某篇文章。

文章边界测试：

```text
GET /api/search/phrase?q=dawn%20only
```

A-100 以 `dawn` 结束，A-200 以 `only` 开始，虽然二者同在 `page-002` 的 OCR 中，但不会命中。

### 2. 单版更正

在 Swagger 的 `PUT /api/pages/page-002/ocr` 中，请求体已预填
`samples/page-002-corrected.json` 的内容（也可直接复制该文件），执行：

- `page-002` OCR 被替换
- 只重建引用该版面的 A-100、A-200 索引
- 响应中的 `affected_articles` 为 `["A-100", "A-200"]`

然后：

| 查询 | 更正前 | 更正后 |
| --- | --- | --- |
| `the lighthouse keeper climbed` | 1 处，跨页 | 0 |
| `the lighthouse keeper walked` | 0 | 1 处，跨页 |
| `only in the archive edition` | 1 处 | 0 |
| `only in the archive supplement` | 0 | 1 处 |

### 3. 缺块时整版拒绝

把 `samples/page-002-missing-block.json` 作为
`PUT /api/pages/page-002/ocr` 的请求体。它只有 block 1，缺少编目引用的 block 2。响应为 **409**：

```json
{
  "detail": {
    "code": "missing_catalog_blocks",
    "message": "OCR retransmission rejected: ...",
    "page_id": "page-002",
    "missing_blocks": [2],
    "affected_articles": ["A-100", "A-200"],
    "unchanged": true
  }
}
```

整个版面的 OCR 替换和索引重建都不会提交；更正前的查询结果照常可查。

### 4. 全文库采集：首次全量，之后只采更正

面向统一全文库的采集接口。首次开**全量批号**取回每篇 OCR 齐全文章的编号、标题和带字框的有序词；翻到最后一页拿到不透明的接续位置 `continuation`，之后凭它开**增量批号**，只取回该位置之后提交的更正。

在 Swagger 按下面顺序操作（也可以直接跑 `python scripts/demo_collection.py`，脚本只调用 HTTP 接口）：

1. 开全量批号，`page_size=1` 便于演示中途更正：

   ```text
   POST /api/collection/batches?mode=full&page_size=1
   ```

   响应给出 `batch_id`、`total_pages` 和批号级 `continuation`。先取第 1 页：

   ```text
   GET /api/collection/batches/{batch_id}/pages/1
   ```

   每组是一篇文章，`words` 按编目块顺序和 OCR 词序排列，每个词带 `page_id`、`block_no`、`word_no`、`seq` 和 `bbox`。中间页不返回 `continuation`。

2. **采到一半发生更正**：在另一个 Swagger 调用里执行

   ```text
   PUT /api/pages/page-002/ocr
   ```

   请求体沿用 `samples/page-002-corrected.json`。OCR、编目和检索接口全程照常可用。

3. 继续把全量批号剩余页取完（`pages/2`、`pages/3`）。批号在创建时就把范围和内容固化了，所以中途的更正**不会**改变任何一页：A-100 仍是含 `climbed` 的旧版本。最后一页 `is_final_page=true`，并返回 `continuation`（如 `cur_Mw`）。

4. 凭接续位置开增量批号：

   ```text
   POST /api/collection/batches?mode=incremental&page_size=50&continuation=cur_Mw
   GET  /api/collection/batches/{新 batch_id}/pages/1
   ```

   增量按提交顺序返回。那次 OCR 更正同时涉及 A-100、A-200，它们在**同一个组**里、不拆页；组内每篇内容都以那次提交为准（A-100 含 `walked`，A-200 含 `supplement`），不会被之后的版本代替。末页返回新的 `continuation`。

5. 立刻再开一轮增量会得到空批号：`total_groups=0`，第 1 页是空的最终页，`continuation` 不变。等以后有新的更正提交，再凭它取回即可。

重启后凭持久化在 SQLite 里的 `batch_id` 可以继续翻页或重取任意一页，结果与重启前完全一致；`GET /api/collection/batches` 列出全部批号，用于找回续采位置。OCR 未齐的文章（`pending_ocr`）不进全量；待 OCR 后补齐时，它作为一次提交进入下一轮增量，不会漏。

## API

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| `PUT` | `/api/pages/{page_id}/ocr` | 替换单版 OCR，原子重建涉及该版的文章索引 |
| `GET` | `/api/pages/{page_id}` | 查看版面 revision、块与词数 |
| `PUT` | `/api/catalog/articles/{article_id}` | 新建/替换文章的块引用顺序 |
| `GET` | `/api/articles/{article_id}` | 查看文章块引用、索引状态与词数 |
| `GET` | `/api/search/phrase?q=...` | 忽略大小写、连续词组的跨块/跨版检索 |
| `GET` | `/api/search?q=...` | 同上，未写入 Swagger 的兼容别名 |
| `POST` | `/api/collection/batches?mode=...` | 开一个不可变的全量/增量采集批号 |
| `GET` | `/api/collection/batches` | 列出已有批号（重启后找回续采位置） |
| `GET` | `/api/collection/batches/{batch_id}` | 查看批号的锚点、页数与接续位置 |
| `GET` | `/api/collection/batches/{batch_id}/pages/{page}` | 按批号取一页，重取/重启结果不变 |
| `GET` | `/health` | 健康检查与行数统计 |

### OCR 输入

```json
{
  "image_url": "/scans/page-002.svg",
  "width": 1600,
  "height": 2200,
  "blocks": [
    {
      "block_no": 1,
      "words": [
        {"text": "walked", "bbox": [100, 140, 220, 175]},
        {"text": "the", "bbox": [230, 140, 295, 175]}
      ]
    }
  ]
}
```

也接受不嵌套块的 OCR 导出：

```json
{
  "words": [
    {"block_no": 1, "text": "walked", "bbox": [100, 140, 220, 175]},
    {"block_no": 1, "text": "the", "bbox": [230, 140, 295, 175]}
  ]
}
```

`bbox` 输入支持：

- `[x0, y0, x1, y1]`
- `{"x0": ..., "y0": ..., "x1": ..., "y1": ...}`
- `{"x": ..., "y": ..., "width": ..., "height": ...}`
- `{"left": ..., "top": ..., "right": ..., "bottom": ...}`

输出统一为像素坐标对象 `{"x0": ..., "y0": ..., "x1": ..., "y1": ...}`。

### 编目输入

`blocks` 的顺序就是文章正文的连续顺序；`page_id + block_no` 指向 OCR 块，不要求物理块号连续：

```json
{
  "title": "The Lighthouse Keeper",
  "blocks": [
    {"page_id": "page-001", "block_no": 1},
    {"page_id": "page-001", "block_no": 2},
    {"page_id": "page-002", "block_no": 1}
  ]
}
```

- OCR 已到位的文章立即建索引。
- 新文章可以先编目、后到 OCR；此时 `status` 为 `pending_ocr`，`indexed_words` 为 0，不会以残缺块序列参与检索。
- 替换已有文章时若引用的 OCR 块缺失，返回 409，旧编目和旧索引保持不变。

### 查询语义

1. 以空白切分查询串和 OCR 词，做 Unicode NFKC + `casefold`，仅忽略大小写。
2. 在**同一文章**内按编目顺序把块展平，块内按 OCR 词序排列。
3. 只匹配连续词元；可以跨块、跨版面，但文章边界不可跨越。
4. 每处命中单独返回，包含文章编号、标题和每个词的位置。

## 一致性与事务设计

- SQLite 开启 WAL 和外键。
- 单版 OCR 重传使用一个 `BEGIN IMMEDIATE` 事务：
  1. 读取该版被哪些文章引用；
  2. 若请求缺少其中任何块，立即回滚整版；
  3. 替换该版 OCR 数据；
  4. 按编目顺序重建且仅重建这些文章的整篇索引（跨版位置一并更新）；
  5. 把本次可采变更写入采集变更日志；
  6. 一次性提交。
- 编目新建/替换在同一事务内更新文章、块引用、检索索引和采集变更日志；校验失败（如替换时 OCR 块缺失）整体回滚，变更日志也不留痕。
- 源数据、检索索引和可采变更在同一个 SQLite 事务里一起生效，任何一步失败都不改库；查询使用单个 deferred read transaction，候选命中、词框和文章标题来自同一个快照。并发时只能看到提交前完整结果或提交后完整结果。
- 未引用该版面的文章索引行不会删除或重写。

### 采集批号的不可变性与全量/增量衔接

- 每次 OCR 或编目提交在 `collection_commits` / `collection_commit_articles` 留一条有序提交记录，内容为提交时刻各篇 OCR 齐全文章的整篇快照（有序词与字框）。只有 OCR 齐全的文章入日志；先编目后到 OCR 的文章在补齐那次提交才出现。
- 开批号时在一个写事务内把该轮全部组**物化**到 `collection_batch_groups`：
  - 全量批号：创建时刻所有 OCR 齐全文章，虚拟提交号 `0`，一篇一组，锚点为当时最新提交号；
  - 增量批号：只物化接续位置之后的提交日志，一次提交一个组、永不拆页，组内即那次提交涉及的全部文章。
- 因此批号的范围与内容在申请时即固定：翻页、重取、重启后接着取都不受后续更正影响，也不会用后来版本替换组内内容。
- 末页的 `continuation` 是锚点提交号的不透明令牌。下一轮增量取严格大于该号的提交：全量锚定之后才提交的更正只进增量，锚定之前的内容只在全量，衔接不漏不重。
- 没有新提交的增量轮返回空批号（0 组），其空最终页仍带同一令牌，可安全轮询。

## 目录

```text
app/
  main.py          FastAPI 路由、启动装载样例
  repository.py    SQLite schema、事务、重建、检索、采集批号与变更日志
  schemas.py       请求/响应模型与 Swagger 示例
samples/
  demo.json                    本地演示语料
  page-002-corrected.json      单版更正请求体
  page-002-missing-block.json  缺块拒绝请求体
scripts/
  demo_collection.py           全量采集中途更正、再增量采齐的 HTTP 演示
static/scans/
  page-001.svg, page-002.svg   由样例字框生成的演示版面图
tests/
  test_api.py               检索、更正、回滚、输入形式
  test_atomic.py            写事务暂停时的读快照验证
  test_collection.py        全量/增量采集、批号不可变、衔接、重启、校验
  test_collection_atomic.py 采集变更日志与源数据同事务的原子性
```

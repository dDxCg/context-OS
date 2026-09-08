# Chrono Context - Version control cho context nguồn tri thức của AI agent

[![CI](https://github.com/dDxCg/chrono-ctx/actions/workflows/ci.yaml/badge.svg)](https://github.com/dDxCg/chrono-ctx/actions/workflows/ci.yaml)
![Python](https://img.shields.io/badge/python-3.10%2B-blue)

[🇬🇧 English](README.md) · 🇻🇳 Tiếng Việt

## Vấn đề

- Context nạp cho AI agent (docs, prompts, workflows) thay đổi liên tục,
  không ai theo dõi version — agent có thể đọc nhầm bản đã sửa hỏng, không
  có cách nào rollback hay biết ai/khi nào đổi.
- Biến thẳng thư mục nguồn thành git repo không hợp: nguồn nằm rải rác
  nhiều chỗ, không phải ai cũng muốn `.git` lẫn vào tài liệu của họ, và
  agent/non-tech user không thao tác git trực tiếp được.
- Không có bề mặt tra cứu chuẩn — cho agent (MCP) lẫn hệ thống khác (HTTP)
  — để hỏi "file này đổi gì, khi nào, ai đổi" mà không phải import thẳng
  codebase.

## Đối tượng người dùng

- **AI agent** (qua MCP): đọc/ghi context source, cần biết version hiện
  tại, cần guardrail phạm vi (không đọc/ghi ngoài scope đã duyệt).
- **Developer/operator** (qua CLI + daemon): khai báo nguồn cần theo dõi,
  chạy daemon watch nền.
- **Hệ thống khác / approval UI tương lai** (qua HTTP API): tra lịch sử/
  diff read-only, không cần import Python.

## Giải pháp

```
Filesystem  --watch-->  VCS Runtime daemon  --commit-->  Git mirror repos (data/repo/)
                              |                                    ^
                              v                                    |
                       SQLite (định danh: contexts/locations)       |
                                                                     |
AI agent  --MCP (stdio)-->  MCP server  --plain fs I/O-->  Filesystem (watcher tự nhặt lại)
HTTP client  --GET /v1/*-->  HTTP API  --read-only-->  audit.py --> git mirror repos
```

Kiến trúc chi tiết (container/sequence diagram, data model, concurrency,
actor attribution, quyết định thiết kế) → [`ARCHITECTURE.md`](ARCHITECTURE.md).

| Tính năng | Làm gì | Trạng thái |
| --- | --- | --- |
| Watch + versioning engine | Watch local, similarity gate quyết định version mới, commit vào git mirror riêng theo watch target | Xong |
| Git mirror backend | Mỗi watch target một git repo riêng (không phải thư mục nguồn) — least-privilege theo scope | Xong |
| `audit.py` (`get_sources`/`get_version_list`/`check_diff`) | Đọc lịch sử/diff thật trên git backend | Xong |
| Config hot-reload | Thêm/xoá nguồn qua `config.yaml` không cần restart daemon | Đang phát triển |
| MCP tool server | 5 tool (`read/write/create/delete/move_file`) + guardrail thật (elicitation, fail-closed); edit qua MCP đã gắn actor thật qua pending hint watcher nhặt lại | Xong |
| HTTP API | 3 route read-only (`/v1/sources`, `/history`, `/diff`), fail-closed 403, auth `X-API-Key` (1 key chung, chưa per-caller scope) | Xong |
| CLI | `source list/add/remove`, `history`, `diff`, `rollback` dùng git rev string xuyên suốt | Xong |
| `ctx daemon` | Chạy nền watch daemon: detached process + PID file, dừng graceful cross-platform (`SIGTERM`/`CTRL_BREAK_EVENT`), `start/stop/status` | Xong |

## Tech Stack

| Layer | Technology |
| --- | --- |
| Runtime daemon | Python 3.13 · watchdog · in-process pub/sub tự xây, shape theo AMQP topic exchange (sẵn sàng swap RabbitMQ) |
| Storage | `git` (subprocess) mirror repo theo watch target · SQLite (raw `sqlite3`, định danh qua `st_ino`/`st_dev`) |
| MCP | FastMCP (stdio) |
| HTTP API | FastAPI + Uvicorn (read-only) |
| CLI | Typer |
| Kiểm thử | pytest + ruff · GitHub Actions CI (Python 3.10–3.14, ubuntu-latest) |

Chi tiết từng module (data model, concurrency, actor attribution) →
[`ARCHITECTURE.md`](ARCHITECTURE.md).

## Bắt đầu nhanh

```bash
git clone https://github.com/dDxCg/chrono-ctx.git && cd chrono-ctx
uv sync                          # hoặc: pip install -e ".[dev]"
cp config.example.yaml config.yaml   # khai báo nguồn cần theo dõi
```

Cần `git` trên PATH (storage backend shell ra `git` qua subprocess).

```bash
ctx daemon start                  # watch + versioning, chạy nền (PID: data/ctx.pid, log: data/ctx.log)
ctx daemon status
ctx daemon stop

uv run python -m app.mcp.server   # MCP server (stdio) — hoặc qua fastmcp.json
uv run python -m app.api.server   # HTTP API, read-only (cần set HTTP_API_KEY)

ctx source add <path>
ctx source remove <path>
```

| Dịch vụ | Địa chỉ |
| --- | --- |
| HTTP API | http://127.0.0.1:8000 |
| MCP server | stdio, không có URL — đăng ký qua `fastmcp.json` với MCP client |

| Biến           | Ý nghĩa                                   | Mặc định           |
| -------------- | ------------------------------------------ | ------------------ |
| `MODE`         | `dev` hoặc khác `dev` (prod)                | `dev`              |
| `DATABASE_URL` | Đường dẫn SQLite (đặt trong `.env.dev`/`.env.prod`) | -           |
| `CONFIG_PATH`  | Đường dẫn file cấu hình nguồn                | `config.yaml`      |
| `SCHEMA_PATH`  | Đường dẫn schema SQL                         | `data/schema.sql`  |
| `GIT_REPO_DIR` | Thư mục chứa các git mirror repo             | `data/repo`        |
| `HTTP_API_KEY` | Giá trị `X-API-Key` bắt buộc cho `/v1/*` — chưa set thì mọi request bị từ chối | -    |

## Test

```bash
uv run pytest
uv run ruff check .
```

## Tài liệu

| Tài liệu | Nội dung |
| --- | --- |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Kiến trúc chi tiết: container/sequence diagram, data model, concurrency, actor attribution, quyết định thiết kế, gap còn mở |

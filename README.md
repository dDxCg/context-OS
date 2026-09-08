# Chrono Context

Hệ thống quản lý phiên bản (version control) cho các **context** dùng làm nguồn
tri thức nạp cho AI agent (docs, prompts, workflows, ...) — thay vì dùng Git,
Chrono Context theo dõi các nguồn cấu hình được, tự động versioning theo nội
dung, và (dự kiến) cung cấp giao diện tool-calling (CLI/MCP) để agent tự tra
cứu lịch sử, rollback, diff.

Xem thêm lý do thiết kế / định hướng ở [docs/note/note.md](docs/note/note.md).

## Trạng thái hiện tại

Dự án đang ở giai đoạn WIP:

- ✅ **Watch + versioning engine** (local sources): hoạt động — theo dõi
  thư mục/file cục bộ, hash nội dung, so sánh similarity để quyết định có tạo
  version mới hay không, lưu vào SQLite + blob storage.
- 🚧 **Config hot-reload** (thêm/xoá nguồn qua `config.yaml` mà không cần
  restart): còn 2 issue mở (shared-queue race, config file chưa được watch)
  — xem [docs/issues.md](docs/issues.md).
- 🚧 **CLI** (`ctx source add/remove/list`, `ctx history/rollback/diff`):
  khung lệnh đã có, phần audit (`history`, `rollback`, `diff`, `list`) còn là
  stub (`src/vcs/services/audit.py`).
- 🚧 **MCP tool server** (`src/app/mcp/server.py`): tool `read/write/create/
  delete/move_file` bản thân vẫn là stub (chưa nối vào versioning thật), nhưng
  đã có **guardrail** thật — mỗi lời gọi được kiểm tra phạm vi nguồn trong
  `config.yaml` (`vcs.services.configure.is_path_in_scope`); nếu ngoài phạm
  vi, server gửi yêu cầu chấp thuận cho client qua MCP elicitation
  (`src/app/mcp/guardrail.py`), chấp thuận thì thêm đúng file đó vào
  `config.yaml`, từ chối/không hỗ trợ elicitation thì chặn (fail-closed). Xem
  thiết kế đầy đủ ở [docs/rabbitmq-migration.md](docs/rabbitmq-migration.md)
  (mục "MCP guardrail").
- ⛔ **HTTP API** (`src/app/api/server.py`): chưa triển khai (toàn bộ đang bị
  comment out).

## Kiến trúc

```
VCSRuntime
├── Initializer            # tạo schema, tạo/snapshot config.yaml, sync nguồn ban đầu
└── LocalRuntime
    ├── WatchWorker         # watchdog observer, 1 watch / source path
    ├── ConsumerWorker      # thread tiêu thụ SourceEvent -> vcs/services/versioning.py
    └── ConfigConsumerWorker
        └── ConfigConsumer  # tiêu thụ Config*Event -> add/remove watch, recover config,
                             #   re-sync nguồn khi config.yaml thay đổi
```

- Sự kiện file (create/modify/delete/move) từ `watchdog` được chuẩn hoá qua
  `utils/formatter.py` thành `SourceEvent` (hoặc `Config*Event` nếu là chính
  file config).
- `vcs/services/versioning.py` quyết định version mới dựa trên
  `text_similarity` (ngưỡng `NEW_VERSION_THRESHOLD` trong
  `vcs/shared/config.py`), lưu nội dung vào `data/snapshots/blobs`.
- Định danh file dùng `(st_ino, st_dev)` (`utils/helper.get_path_stats`) thay vì
  path string, để chịu được rename/move.

## Cài đặt

Yêu cầu Python >= 3.10 (repo dùng 3.13, xem `.python-version`).

```bash
uv sync            # hoặc: pip install -e ".[dev]"
```

Tạo file môi trường (đã có sẵn `.env`, `.env.dev`, `.env.prod` mẫu):

| Biến           | Ý nghĩa                                   | Mặc định           |
|----------------|--------------------------------------------|--------------------|
| `MODE`         | `dev` hoặc khác `dev` (prod)                | `dev`              |
| `DATABASE_URL` | đường dẫn SQLite (đặt trong `.env.dev`/`.env.prod`) | -           |
| `CONFIG_PATH`  | đường dẫn file cấu hình nguồn                | `config.yaml`      |
| `SCHEMA_PATH`  | đường dẫn schema SQL                         | `data/schema.sql`  |

Copy `config.example.yaml` → `config.yaml` và khai báo các nguồn local cần
theo dõi:

```yaml
sources:
  - type: local
    path: path/to/docs
```

## Chạy

```bash
uv run python -m vcs.runtime      # khởi tạo schema/config, bắt đầu watch + versioning
ctx source add <path>      # thêm nguồn vào config.yaml
ctx source remove <path>   # xoá nguồn khỏi config.yaml
```

## Test

```bash
pytest
```

## Tài liệu liên quan

- [docs/note/note.md](docs/note/note.md) — ghi chú thiết kế, kiến trúc, vấn đề đang mở.
- [docs/issues.md](docs/issues.md) — danh sách lỗi phát hiện qua review (config
  hot-reload subsystem).
- [docs/mcp-test-plan.md](docs/mcp-test-plan.md) — test plan cho MCP server
  (guardrail + 5 tools): coverage matrix, gap còn lại, cách chạy tự động và
  test thủ công qua MCP Inspector.

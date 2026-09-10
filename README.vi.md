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
| MCP tool server | 5 tool (`read/write/create/delete/move_file`) + guardrail thật (elicitation, fail-closed); edit qua MCP đã gắn actor thật qua pending hint watcher nhặt lại; `write`/`delete` nhận `expected_version` tuỳ chọn để check optimistic-concurrency | Xong |
| HTTP API | 3 route read-only (`/v1/sources`, `/history`, `/diff`), fail-closed 403, auth `X-API-Key` (1 key chung, chưa per-caller scope) | Xong |
| CLI | `source list/add/remove`, `history`, `diff`, `rollback`, `rollback-session` (undo mọi thứ 1 actor đã làm, qua mọi watch target) | Xong |
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

## Cài đặt

```bash
pipx install chrono-ctx    # khuyến nghị - venv riêng biệt, tự thêm ctx vào PATH
```

```bash
pip install chrono-ctx     # cũng chạy được, nhưng ctx có thể nằm ngoài PATH -
                            # pip sẽ cảnh báo nếu vậy, ctx cũng tự cảnh báo mỗi
                            # lần chạy cho tới khi được sửa
```

```bash
# không cần cài - chạy trong venv ephemeral có cache. Entry point tên `ctx`,
# không phải `chrono-ctx`, nên cần --from (uvx tự báo nếu quên):
uvx --from chrono-ctx ctx --version
uvx --from chrono-ctx ctx daemon start
```

Nếu `pip`/`ctx` cảnh báo `ctx` chưa có trên PATH, thêm thư mục được in ra vào PATH:

```powershell
# Windows (PowerShell) - cần mở shell mới để có hiệu lực
setx PATH "%PATH%;<thư mục trong cảnh báo>"
```

```bash
# Linux/macOS - thêm vào ~/.bashrc hoặc ~/.zshrc để giữ lại qua các phiên
export PATH="$PATH:<thư mục trong cảnh báo>"
```

Cần `git` trên PATH (storage backend shell ra `git` qua subprocess).

## Kết nối AI agent (MCP)

MCP server chạy qua **stdio** — không URL, không port. Mọi client bên dưới đều
spawn nó như một subprocess, nên thứ duy nhất cần khai báo là câu lệnh.

### 1. Lệnh khởi chạy

Dùng runner nào cũng được — cả hai tự tải hoặc tái dùng package, không cần cài
gì trước:

```bash
uvx --from chrono-ctx python -m app.mcp.server        # uv
pipx run --spec chrono-ctx python -m app.mcp.server   # pipx
```

Ví dụ bên dưới dùng dạng `uvx`; muốn đổi sang `pipx run` thì thay cặp
`command`/`args` tương ứng.

### 2. Đăng ký với client

**Claude Code** — một lệnh, không cần sửa file:

```bash
claude mcp add chrono-ctx -- uvx --from chrono-ctx python -m app.mcp.server
claude mcp add --scope project chrono-ctx -- <command> <args...>   # ghi vào .mcp.json để commit cho cả team
```

**Claude Desktop** — `claude_desktop_config.json` (Settings → Developer → Edit
Config; `%APPDATA%\Claude\` trên Windows, `~/Library/Application Support/Claude/`
trên macOS):

```json
{
  "mcpServers": {
    "chrono-ctx": {
      "command": "uvx",
      "args": ["--from", "chrono-ctx", "python", "-m", "app.mcp.server"]
    }
  }
}
```

**Codex CLI** — `~/.codex/config.toml` (chú ý `mcp_servers`, có dấu gạch dưới).
Nên dùng `codex mcp add`, vì một dòng TOML sai cú pháp làm chết toàn bộ server:

```toml
[mcp_servers.chrono-ctx]
command = "uvx"
args = ["--from", "chrono-ctx", "python", "-m", "app.mcp.server"]
```

**VS Code / GitHub Copilot** — `.vscode/mcp.json` (workspace) hoặc file trong
user profile. Key gốc của VS Code là `servers`, **không phải** `mcpServers`:

```json
{
  "servers": {
    "chrono-ctx": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "chrono-ctx", "python", "-m", "app.mcp.server"]
    }
  }
}
```

**Cursor** — `.cursor/mcp.json` (project) hoặc `~/.cursor/mcp.json` (global);
**Windsurf** — `~/.codeium/windsurf/mcp_config.json`
(`%USERPROFILE%\.codeium\windsurf\` trên Windows); **Gemini CLI** —
`.gemini/settings.json` (project) hoặc `~/.gemini/settings.json`. Cả ba dùng
đúng block `mcpServers` như Claude Desktop ở trên. Sửa xong nhớ khởi động lại
client.

### 3. Trỏ agent về cùng dữ liệu với daemon

MCP server cố tình bỏ qua cwd (client là bên chọn cwd, và dựa vào nó sẽ âm thầm
tách hai tiến trình sang hai file `config.yaml` khác nhau). Nó resolve mọi thứ
theo `CHRONO_CTX_HOME`, mặc định là thư mục dữ liệu per-user.

Mặc định đó đã khớp với daemon cài bằng `pipx`/`pip`, nên đa số setup không cần
làm gì thêm. **Chỉ lệch khi daemon chạy từ source checkout**, vì lúc đó nó anchor
vào repo root — triệu chứng rất im lặng: file vẫn được ghi xuống đĩa nhưng không
có gì được versioning. Trường hợp đó pin rõ:

```json
"env": { "CHRONO_CTX_HOME": "/path/to/chrono-ctx" }
```

### 4. Kiểm chứng

Chạy daemon trước (`ctx daemon start`) — MCP server ghi file, daemon mới là bên
tạo version. Sau đó từ agent đọc/ghi một file trong nguồn đang được watch rồi
kiểm tra:

```bash
ctx history <path>          # lần ghi hiện ra thành một rev, attribute agent:<id>
tail data/mcp.log           # mỗi tool call một dòng vào/ra, kèm thời gian chạy
```

`data/mcp.log` chỉ ghi đường dẫn và thời lượng — không bao giờ ghi nội dung
file. Mọi call đều có bound (chờ lock 30s, 4s mỗi git call, worst case 48s), nên
call bị kẹt sẽ fail bằng lỗi có cấu trúc thay vì treo.

## Bắt đầu nhanh (từ source, để đóng góp)

```bash
git clone https://github.com/dDxCg/chrono-ctx.git && cd chrono-ctx
uv sync --extra dev              # hoặc: pip install -e ".[dev]"
cp config.example.yaml config.yaml   # khai báo nguồn cần theo dõi
```

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
| MCP server | stdio, không có URL — xem [Kết nối AI agent (MCP)](#kết-nối-ai-agent-mcp) |

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

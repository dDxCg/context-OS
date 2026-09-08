# 015 — `TempFile.TMP_DIR` cwd-relative

Status: implemented

## Context

[issues.md #19](../agents/issues.md): every other configured path
(`SNAPSHOT_DIR`, `BLOB_DIR`, `GIT_REPO_DIR`, `CONFIG_SNAPSHOT_FILE` in
`vcs/shared/config.py`) is anchored to the project root via `anchored()` —
deliberately, since the MCP server and the VCS runtime are separate
processes whose working directories need not match. `TempFile.TMP_DIR`
(`vcs/shared/temp_file.py`) was missed: it's a bare `Path("data/tmp")`,
resolved against whatever the process's cwd happens to be. Harmless when
launched from the repo root; scatters `data/tmp` wherever a detached daemon
happens to start otherwise, and `modified_handle` stages blobs outside the
real data directory.

## Scope

**In**
- `vcs/shared/temp_file.py`: anchor `TMP_DIR` via `anchored()`, matching
  `SNAPSHOT_DIR`'s pattern in `vcs/shared/config.py`. Env-overridable via
  `TMP_DIR`, same convention as `GIT_REPO_DIR`/`SNAPSHOT_DIR`.

**Out**
- Changing how `TMP_DIR` is consumed (`make_dirs`, `create_tmp_file`, etc.)
  — those already just use the class attribute, no call site changes needed.

## Acceptance criteria

- AC-1. `TempFile.TMP_DIR` resolves to an absolute path anchored at the
  project root (`PROJECT_ROOT / "data/tmp"` by default), not a path relative
  to the process's cwd.

## Error cases

None — static path resolution fix, no new runtime branch.

## Contracts

- `vcs/shared/temp_file.py`: `TempFile.TMP_DIR: Path`, computed the same way
  `vcs/shared/config.py`'s `SNAPSHOT_DIR` is:
  `Path(anchored(os.getenv("TMP_DIR", "data/tmp")))`.

## Non-goals / open questions

None outstanding.

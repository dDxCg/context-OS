# Draft — mirror storage: stop duplicating the current version

Status: **implemented** ([spec 025](../specs/025-bare-mirror-plumbing-writes.md)).
Two real surprises found while building it, worth keeping here since they
weren't predictable from reading git's docs alone:

1. On a **bare** repo specifically, plain `git rev-parse HEAD` on an unborn
   HEAD (zero commits yet) exits **0** and prints the literal string
   `"HEAD"` unresolved - a non-bare repo correctly exits non-zero for the
   same case. `git rev-parse --verify HEAD` fixes it (guarantees a real
   SHA-1 or a non-zero exit, identically for both repo shapes).
2. `git update-index --force-remove` **refuses to run at all without a
   work tree** (`fatal: this operation must be run in a work tree`) - not
   usable for a bare-repo delete despite looking like the obvious
   index-only removal primitive. `git rm --cached -r --ignore-unmatch`
   (the `--cached` mode of the porcelain `rm`, not the plumbing
   `update-index`) is the one removal command that works purely on the
   index, and handles the directory-subtree case for free too.

Original draft below, unchanged.

## Where the "x2 storage" actually comes from, precisely

Not git history bloat — git's object store is already content-addressed
(SHA) and already deduplicates identical blobs across commits; `write()`
even short-circuits to a no-op when content is byte-identical to HEAD
(`git_store.py:118-121`). The duplication is structural, from how the
mirror repos are built: `git_store.write()` (`git_store.py:105-129`)
**writes the real file into the mirror's working tree**
(`target.write_bytes(content)`) before `git add`/`git commit`. `remove()`
(`git rm`) and `move()` (`git mv`) are porcelain commands too — both
require a working tree the same way.

Net effect, for every tracked file's *current* version, three copies exist
on disk simultaneously:

1. The real source file, at its original location — required, this is the
   actual file the user/agent reads and writes; not something to eliminate.
2. A checked-out copy inside the mirror repo's working tree
   (`GIT_REPO_DIR/.../relpath`) — a byte-for-byte duplicate of #1 as of the
   last commit, uncompressed.
3. Git's own object-store copy of that same blob (`.git/objects`, loose or
   packed) — needed for history/diff/rollback, reasonably compressed
   (zlib, better once `gc.auto` packs+deltas it).

\#2 is the pure waste — a full, uncompressed duplicate that exists *only*
because `write()`/`remove()`/`move()` use git's porcelain commands, which
are working-tree commands by design. Historical (non-HEAD) versions are
**not** duplicated this way — a working tree only ever holds the checked-out
state, so older versions live purely in the (already deduplicated,
already-being-packed) object store. This is why the fix below targets the
*current-version* redundancy specifically, not history.

## Recommended fix: bare mirror repos, git plumbing instead of porcelain

A bare repo (`git init --bare`) has no working tree at all — writes go
straight into the object database via plumbing commands. Every *read* path
already works unmodified against a bare repo, because none of them touch a
working tree: `show()` (`git show rev:path`), `log_history()`/
`commits_by_author()` (`git log`), `diff()` (`git diff rev1 rev2 -- path`),
`head_rev()` (`git log -1`) — all object-store reads, bare-compatible as-is.

What needs to change — contained to `git_store.py`, nothing above it (spec
012's `write_with_check`, `rollback_source`/`rollback_session` in
`audit.py`, every MCP/CLI/HTTP caller) needs to know or care:

- **`init_repo()`**: `git init --bare` instead of `git init`. `git config
  user.email`/`user.name` work identically on a bare repo.
- **`_require_initialized()`**: currently checks `(repo_path /
  ".git").is_dir()` — a bare repo has no `.git` subdirectory, its contents
  (`HEAD`, `objects/`, `refs/`) sit directly under `repo_path`. Check
  becomes e.g. `(repo_path / "HEAD").exists() and (repo_path /
  "objects").is_dir()`, or shell out to `git rev-parse --is-bare-repository`.
- **`write()`**: replace `target.write_bytes(content)` + `git add` with
  plumbing: hash the content into the object store (`git hash-object -w
  --stdin`, content piped in — never touches a working-tree file at all),
  stage it into the index at `relpath` (`git update-index --add
  --cacheinfo 100644,<blob-sha>,<relpath>` — an index update, not a
  filesystem write), then `git write-tree` + `git commit-tree` (parented on
  the current HEAD, or none for the first commit) + move the ref. The
  existing no-op-on-identical-content check (`git diff --cached --quiet`)
  needs an equivalent against the *would-be* tree instead of a working-tree
  diff — compare the new tree's SHA to HEAD's tree SHA directly, simpler
  than the current diff-based check.
- **`remove()`**: `git update-index --force-remove relpath` (index-only)
  instead of `git rm`, then the same write-tree/commit-tree/move-ref
  sequence.
- **`move()`**: currently checks `(repo_path / src_relpath).exists()` as
  its "is there anything to move" precondition — a working-tree check that
  breaks once there's no working tree. Spec 024 already built exactly the
  replacement primitive for this shape of question:
  `path_exists_at_rev(repo_path, src_relpath, "HEAD")` (tree-membership
  check, not filesystem existence). The move itself becomes: read the
  blob's mode+SHA at `src_relpath` from the current tree
  (`git ls-tree HEAD -- src_relpath`), stage it at `dst_relpath`
  (`--cacheinfo`), force-remove `src_relpath` from the index, then the same
  write-tree/commit-tree sequence `write()`/`remove()` use.

All four still run inside the same `_lock_for(repo_path)` cross-process
lock (spec 012/022) — plumbing commands are still real `git` subprocess
calls racing the same way porcelain ones did, nothing about bare repos
changes that.

## Migration for existing (non-bare) mirror repos

A real one-time operational step, not automatic just by shipping the code
change — existing repos under `data/repo/` already have working trees.
Converting one in place: `git config core.bare true` inside it, then delete
every file except `.git`'s own contents (which, for an already-non-bare
repo, live under `repo_path/.git/` — for a repo converted to bare in place,
`repo_path/.git/*` would need to move up to `repo_path/*` first, or more
simply: treat this as "new repos are bare going forward, existing ones stay
as-is until an explicit `ctx mirror-migrate` pass" rather than a silent
auto-migration on next daemon start, since a mid-migration crash on a
production mirror is exactly the kind of thing to avoid rushing). Decide
the exact mechanics (in-place `core.bare` flip vs. `git clone --bare` to a
new path then swap) at spec time, under the same cross-process lock so
nothing writes mid-migration.

## Rough size impact (estimate, not a promise)

For total current-version content size `S` across all tracked files:
today's mirror-side footprint is roughly `S` (working-tree copy,
uncompressed) `+ ~0.5S` (packed object-store copy, ballpark for
compressible text — binaries like PDFs won't compress much either way) on
top of the original `S`, so total ≈ `2.5S`. Removing the working-tree copy
brings mirror-side footprint down to just the object-store copy,
total ≈ `1.5S`. A real, meaningful cut, not a full elimination of
duplication — the original source file and one content-addressed copy in
git's object store are both genuinely necessary.

## Alternatives considered, not recommended

- **Hardlink the mirror's working-tree file to the source instead of
  copying bytes** — rejected: a hardlink shares the *same* inode, so it
  doesn't capture a point-in-time snapshot at all; if the source changes
  again before the next commit, the "old" mirror copy would silently
  reflect the new content too. Fundamentally wrong for a versioning tool,
  independent of the storage question. Going bare (no working-tree file at
  all) is strictly better than trying to make a working-tree file cheaper.
- **Content-defined chunking / block-level dedup** (restic/borgbackup-style)
  — real technique, over-engineered for this project's stated shape (a
  bare dev-machine tool versioning docs/prompts/configs, not large
  binaries at scale — see [STATE.md](../STATE.md)'s repeated stance against
  building ahead of an actual need). Git's whole-blob content addressing
  plus pack-time delta compression already covers the stated use case.
- **Git LFS for large binaries** — a real, standard answer if binary
  content (e.g. `knowledge.example/docs/*.pdf`) turns out to dominate
  storage, but adds an external LFS store dependency, which cuts against
  the "reduce footprint" motivation this session raised for related work
  ([install-integration-plan.md](install-integration-plan.md)). Revisit
  only if binary bloat is an actual observed problem, not preemptively.
- **`ctx gc`** ([FUTURE.md](../FUTURE.md) item 4) — complementary, not a
  substitute: still worth building eventually to reclaim loose-object
  churn, but doesn't touch the working-tree duplication this plan targets,
  which is the larger and more clearly "x2" contributor.

## Not done here

- The exact plumbing command sequences above are a sketch, not verified
  against a real git invocation yet — confirm exact flags
  (`--cacheinfo` mode string, `commit-tree` parent handling for the
  first-ever commit) when writing the spec.
- The existing-repo migration mechanics (in-place flip vs. re-clone) —
  decide at spec time.
- Whether this becomes one spec or two (bare-repo write path, then
  separately the migration tool) — likely two, given `AGENTS.md`'s "one
  behavior per cycle" rule.

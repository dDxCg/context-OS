import yaml

import vcs.services.configure as configure
from utils.helper import path_normalize


def _write_config(path, sources):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump({"sources": sources}))


def _mkdir(parent, name):
    d = parent / name
    d.mkdir()
    return d


def test_init_config_file_creates_missing_file(config_path):
    assert not config_path.exists()

    configure.init_config_file()

    assert config_path.exists()
    assert config_path.read_text() == ""


def test_init_config_file_leaves_existing_file_untouched(config_path):
    _write_config(config_path, [{"type": "local", "path": "/a"}])
    before = config_path.read_text()

    configure.init_config_file()

    assert config_path.read_text() == before


def test_reset_config_file_truncates_existing_file(config_path):
    _write_config(config_path, [{"type": "local", "path": "/a"}])

    configure.reset_config_file()

    assert config_path.exists()
    assert config_path.read_text() == ""


def test_add_sources_appends_new_paths_and_skips_duplicates(config_path, tmp_path):
    existing = tmp_path / "existing"
    existing.mkdir()
    _write_config(config_path, [{"type": "local", "path": path_normalize(str(existing))}])

    new_source = tmp_path / "new_source"
    new_source.mkdir()

    configure.add_sources([str(new_source), str(existing)])

    saved = yaml.safe_load(config_path.read_text())
    paths = {s["path"] for s in saved["sources"]}
    assert paths == {path_normalize(str(existing)), path_normalize(str(new_source))}
    assert len(saved["sources"]) == 2


def test_remove_sources_filters_matching_paths(config_path, tmp_path):
    keep = tmp_path / "keep"
    drop = tmp_path / "drop"
    keep.mkdir()
    drop.mkdir()
    _write_config(config_path, [
        {"type": "local", "path": path_normalize(str(keep))},
        {"type": "local", "path": path_normalize(str(drop))},
    ])

    configure.remove_sources([str(drop)])

    saved = yaml.safe_load(config_path.read_text())
    assert [s["path"] for s in saved["sources"]] == [path_normalize(str(keep))]


def test_is_path_in_scope_matches_exact_source_path(config_path, tmp_path):
    source_file = tmp_path / "doc.txt"
    source_file.write_text("hi")
    _write_config(config_path, [{"type": "local", "path": str(source_file)}])

    assert configure.is_path_in_scope(str(source_file)) is True


def test_is_path_in_scope_matches_nested_file_under_source_dir(config_path, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    nested = source_dir / "sub" / "doc.txt"
    _write_config(config_path, [{"type": "local", "path": str(source_dir)}])

    assert configure.is_path_in_scope(str(nested)) is True


def test_is_path_in_scope_rejects_path_outside_every_source(config_path, tmp_path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    outside = tmp_path / "outside" / "doc.txt"
    _write_config(config_path, [{"type": "local", "path": str(source_dir)}])

    assert configure.is_path_in_scope(str(outside)) is False


def test_is_path_in_scope_ignores_non_local_sources(config_path, tmp_path):
    target = tmp_path / "doc.txt"
    _write_config(config_path, [{"type": "3rd-party", "path": str(tmp_path)}])

    assert configure.is_path_in_scope(str(target)) is False


def test_parse_config_dedups_equivalent_paths(config_path, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [
        {"type": "local", "path": str(source)},
        {"type": "local", "path": str(source) + "/."},
    ])

    parsed = configure.parse_config()

    assert len(parsed["sources"]) == 1


def test_store_config_snapshot_writes_normalized_sources(config_path, config_snapshot_file, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    _write_config(config_path, [{"type": "local", "path": str(source)}])

    configure.store_config_snapshot()

    assert config_snapshot_file.exists()
    snapshot = yaml.safe_load(config_snapshot_file.read_text())
    assert snapshot["sources"][0]["path"] == path_normalize(str(source))


def test_get_config_diff_reports_added_and_deleted_sources(config_path, config_snapshot_file, tmp_path):
    kept = tmp_path / "kept"
    removed = tmp_path / "removed"
    added = tmp_path / "added"
    kept.mkdir()
    removed.mkdir()
    added.mkdir()

    _write_config(config_path, [
        {"type": "local", "path": str(kept)},
        {"type": "local", "path": str(removed)},
    ])
    configure.store_config_snapshot()

    _write_config(config_path, [
        {"type": "local", "path": str(kept)},
        {"type": "local", "path": str(added)},
    ])

    diff = configure.get_config_diff()

    assert diff == {
        "added": [path_normalize(str(added))],
        "deleted": [path_normalize(str(removed))],
    }


def test_parse_config_on_freshly_initialized_file_does_not_crash(config_path):
    configure.init_config_file()

    parsed = configure.parse_config()

    assert parsed == {"sources": []}


def test_recover_config_restores_file_from_snapshot(config_path, config_snapshot_file, tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    config_snapshot_file.parent.mkdir(parents=True, exist_ok=True)
    config_snapshot_file.write_text(
        yaml.safe_dump({"sources": [{"type": "local", "path": str(source)}]})
    )

    configure.recover_config()

    assert config_path.exists()
    restored = yaml.safe_load(config_path.read_text())
    assert restored["sources"][0]["path"] == path_normalize(str(source))


def test_derive_watch_targets_maps_file_source_to_its_directory(config_path, tmp_path):
    """watchdog watches directories; the guardrail grants files."""
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    source_file = source_dir / "a.txt"
    source_file.write_text("a")
    _write_config(config_path, [{"type": "local", "path": str(source_file)}])

    assert configure.derive_watch_targets() == [source_dir.as_posix()]


def test_derive_watch_targets_collapses_siblings_to_one_watch(config_path, tmp_path):
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    for name in ("a.txt", "b.txt", "c.txt"):
        (source_dir / name).write_text(name)
    _write_config(config_path, [
        {"type": "local", "path": str(source_dir / name)}
        for name in ("a.txt", "b.txt", "c.txt")
    ])

    assert configure.derive_watch_targets() == [source_dir.as_posix()]


def test_derive_watch_targets_drops_directory_covered_by_ancestor(config_path, tmp_path):
    parent = tmp_path / "docs"
    child = parent / "nested"
    child.mkdir(parents=True)
    _write_config(config_path, [
        {"type": "local", "path": str(parent)},
        {"type": "local", "path": str(child)},
    ])

    assert configure.derive_watch_targets() == [parent.as_posix()]


def test_derive_watch_targets_keeps_disjoint_directories(config_path, tmp_path):
    a = _mkdir(tmp_path, "a")
    b = _mkdir(tmp_path, "b")
    _write_config(config_path, [
        {"type": "local", "path": str(a)},
        {"type": "local", "path": str(b)},
    ])

    assert sorted(configure.derive_watch_targets()) == sorted([a.as_posix(), b.as_posix()])


def test_derive_watch_targets_walks_up_to_nearest_existing_dir(config_path, tmp_path):
    existing = tmp_path / "docs"
    existing.mkdir()
    missing = existing / "gone" / "deeper" / "a.txt"
    _write_config(config_path, [{"type": "local", "path": str(missing)}])

    assert configure.derive_watch_targets() == [existing.as_posix()]


def test_derive_watch_targets_skips_source_with_no_existing_ancestor(config_path):
    """Must not fall back to a filesystem root - that would put the whole disk
    under the watcher."""
    _write_config(config_path, [{"type": "local", "path": "Z:/nonexistent/a.txt"}])

    assert configure.derive_watch_targets() == []


def test_derive_watch_targets_skips_non_local_sources(config_path, tmp_path):
    source = tmp_path / "docs"
    source.mkdir()
    _write_config(config_path, [
        {"type": "3rd-party", "path": str(source)},
    ])

    assert configure.derive_watch_targets() == []


def test_add_sources_skips_path_already_covered_by_a_source(config_path, tmp_path):
    """The guardrail grants one path per approval; without subsumption
    config.yaml accumulates entries a broader source already covers."""
    source_dir = tmp_path / "docs"
    source_dir.mkdir()
    nested = source_dir / "a.txt"
    nested.write_text("a")
    _write_config(config_path, [{"type": "local", "path": str(source_dir)}])

    configure.add_sources([str(nested)])

    saved = yaml.safe_load(config_path.read_text())
    assert [s["path"] for s in saved["sources"]] == [path_normalize(str(source_dir))]


def test_store_config_snapshot_records_the_config_it_was_given(config_path, config_snapshot_file, tmp_path):
    """The baseline must only advance to state that was actually applied.

    config.yaml can be rewritten while the config consumer works - the MCP
    guardrail appends one path per approval. If the snapshot re-read the file
    instead of recording the config the diff was computed from, it would
    advance past the newer entry and that change would be lost permanently:
    every later diff reports nothing, since current == snapshot.
    """
    a = _mkdir(tmp_path, "a")
    b = _mkdir(tmp_path, "b")
    _write_config(config_path, [{"type": "local", "path": str(a)}])
    applied = configure.parse_config()

    # A concurrent writer adds b/ after we read but before we snapshot.
    _write_config(config_path, [
        {"type": "local", "path": str(a)},
        {"type": "local", "path": str(b)},
    ])
    configure.store_config_snapshot(config_content=applied)

    snapshot = yaml.safe_load(config_snapshot_file.read_text())
    assert [s["path"] for s in snapshot["sources"]] == [path_normalize(str(a))]
    # b/ is still pending, so the next diff must surface it.
    assert configure.get_config_diff()["added"] == [path_normalize(str(b))]


def test_get_config_diff_uses_supplied_config(config_path, config_snapshot_file, tmp_path):
    a = _mkdir(tmp_path, "a")
    b = _mkdir(tmp_path, "b")
    _write_config(config_path, [{"type": "local", "path": str(a)}])
    configure.store_config_snapshot()
    supplied = {"sources": [
        {"type": "local", "path": path_normalize(str(a))},
        {"type": "local", "path": path_normalize(str(b))},
    ]}

    diff = configure.get_config_diff(config=supplied)

    assert diff["added"] == [path_normalize(str(b))]

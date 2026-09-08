from pathlib import Path


from utils.helper import collect_files, gen_hash, gen_ulid, path_normalize
from utils.logger import log_enabled

from vcs.shared.types import ContextEntry
from vcs.db.sqlite import DBHandler
from vcs.services.configure import derive_watch_targets
from vcs.services.versioning import _append_context

class LocalAdapter:
    def __init__(self, db_handler: DBHandler):
        self.db_handler = db_handler

    def local_file_processing(self, file_path: Path, watch_targets: list[str] | None = None):
        watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
        if file_path is not Path:
            file_path = Path(file_path)
        file_content = file_path.read_bytes()
        content_hash = gen_hash(file_content)
        context_id = gen_ulid()

        context_entry = ContextEntry(
            context_id=context_id,
            provider="local",
            location=path_normalize(file_path),
            content_hash=content_hash
        )

        # git_store commits the content (see versioning._append_context) -
        # no separate BLOB_DIR write here anymore; that was this path's own
        # duplicate of what created_handle's watcher path does.
        _append_context(self.db_handler, context_entry, watch_targets)

    def local_directory_processing(self, dir_path, watch_targets: list[str] | None = None):
        watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
        files = collect_files(dir_path)
        for file_path in files:
            self.local_file_processing(file_path, watch_targets=watch_targets)

    @log_enabled
    def local_processing(self, path, watch_targets: list[str] | None = None):
        watch_targets = watch_targets if watch_targets is not None else derive_watch_targets()
        p = Path(path).resolve()

        if p.is_file():
            self.local_file_processing(path, watch_targets=watch_targets)

        if p.is_dir():
            self.local_directory_processing(path, watch_targets=watch_targets)



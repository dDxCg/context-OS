
from vcs.shared.types import CONFIG_EVENTS, SourceEvent, MovedEvent, ModifiedEvent, DeletedEvent, CreatedEvent
from vcs.services.versioning import moved_handle, modified_handle, deleted_handle, created_handle
from vcs.shared.temp_file import TempFile
from vcs.workers.interfaces.consumer import Consumer

from utils.logger import log_enabled


class LocalConsumer(Consumer):
    @log_enabled
    def handle(self, event: SourceEvent):
        if isinstance(event, CONFIG_EVENTS):
            return
        if isinstance(event, MovedEvent):
            moved_handle(self.db_handler, event)
        if isinstance(event, ModifiedEvent):
            modified_handle(self.db_handler, event, tmp_file=TempFile.from_path(event.src))
        if isinstance(event, DeletedEvent):
            deleted_handle(self.db_handler, event)
        if isinstance(event, CreatedEvent):
            created_handle(self.db_handler, event)
        
        





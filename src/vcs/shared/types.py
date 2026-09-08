from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from utils.helper import gen_ulid, gen_hash, get_config_path, path_normalize

@dataclass
class ContextEntry:
    context_id: str
    location: str
    provider: str
    content_hash: str

    #Construct for local path
    def __init__(
            self, 
            context_id: str, 
            location: str, 
            provider: str, 
            content_hash: str 
            ):
            self.context_id = context_id
            self.location = location
            self.provider = provider
            self.content_hash = content_hash

    @classmethod
    def from_path(cls, path: Path):
        file_content = Path(path).read_bytes()
        content_hash = gen_hash(file_content)
        context_id = gen_ulid()
        location = path
        provider = 'local'
        return cls(context_id, location, provider, content_hash)
        
@dataclass
class Location:
    context_id: str
    location: str
    provider: str
    status: int

@dataclass
class Version:
    version_number: int 
    context_id: str
    content_hash: str

@dataclass
class Query:
    query: str
    params: tuple[Any, ...] | None = None

@dataclass
class SourceEvent:
    src: str
    type: str
    provider: str = "local"
    is_dir: bool = False
    actor: str | None = None

@dataclass
class CreatedEvent(SourceEvent):
    type: str = field(init=False, default="added")

@dataclass
class ModifiedEvent(SourceEvent):
    type: str = field(init=False, default="modified")

@dataclass
class DeletedEvent(SourceEvent):
    type: str = field(init=False, default="deleted")

@dataclass(kw_only=True)
class MovedEvent(SourceEvent):
    type: str = field(init=False, default="moved")
    dst: str

@dataclass
class ConfigCreatedEvent(CreatedEvent):
    src: str = field(default_factory=lambda: path_normalize(get_config_path()))

@dataclass
class ConfigModifiedEvent(ModifiedEvent):
    src: str = field(default_factory=lambda: path_normalize(get_config_path()))

@dataclass
class ConfigMovedEvent(MovedEvent):
    src: str = field(default_factory=lambda: path_normalize(get_config_path()))

@dataclass
class ConfigDeletedEvent(DeletedEvent):
    src: str = field(default_factory=lambda: path_normalize(get_config_path()))

# Config events subclass the plain events, so isinstance(e, CreatedEvent) is true
# for a ConfigCreatedEvent. Dispatch and routing must check this tuple explicitly.
CONFIG_EVENTS = (
    ConfigCreatedEvent,
    ConfigModifiedEvent,
    ConfigMovedEvent,
    ConfigDeletedEvent,
)

# --- Pub/sub routing -------------------------------------------------------
#
# Routing keys are coarse: the category, not the verb. The verb stays an
# isinstance concern inside each consumer.
#
# Subscribers bind with "source.#" / "config.#" rather than an exact match. In
# AMQP topic exchanges "#" matches *zero or more* words, so those patterns match
# the bare keys below as well as a future "source.created" - publishing can be
# refined later without touching any binding.
SOURCE_TOPIC = "source"
CONFIG_TOPIC = "config"


def topic_for(event) -> str:
    """Routing key for `event`."""
    return CONFIG_TOPIC if isinstance(event, CONFIG_EVENTS) else SOURCE_TOPIC


# Wire name -> class. Not used locally (events are passed by reference), but it
# is the contract a real broker will inherit, so it is defined and tested now.
# The names are finer than the routing keys on purpose: they already carry the
# verb, so refining routing later needs no new vocabulary.
_EVENT_NAMES = {
    "source.created": CreatedEvent,
    "source.modified": ModifiedEvent,
    "source.deleted": DeletedEvent,
    "source.moved": MovedEvent,
    "config.created": ConfigCreatedEvent,
    "config.modified": ConfigModifiedEvent,
    "config.deleted": ConfigDeletedEvent,
    "config.moved": ConfigMovedEvent,
}
_EVENT_CLASS_NAMES = {cls: name for name, cls in _EVENT_NAMES.items()}


def event_name(event) -> str:
    """Stable wire name for `event`, e.g. "source.modified"."""
    try:
        return _EVENT_CLASS_NAMES[type(event)]
    except KeyError:
        raise ValueError(f"unregistered event type: {type(event).__name__}") from None


def event_to_dict(event) -> dict:
    """Serialize `event` to a broker-friendly dict."""
    data = {
        "event": event_name(event),
        "src": event.src,
        "provider": event.provider,
        "is_dir": event.is_dir,
        "actor": event.actor,
    }
    dst = getattr(event, "dst", None)
    if dst is not None:
        data["dst"] = dst
    return data


def event_from_dict(data: dict):
    """Rebuild an event from `event_to_dict` output.

    `src` is always passed explicitly. The Config*Event classes default it via
    a factory reading the *current* config path, so relying on that default
    would silently rewrite src on a round-trip.
    """
    name = data.get("event")
    cls = _EVENT_NAMES.get(name)
    if cls is None:
        raise ValueError(f"unknown event name: {name!r}")

    kwargs = {
        "src": data["src"],
        "provider": data.get("provider", "local"),
        "is_dir": data.get("is_dir", False),
        "actor": data.get("actor"),
    }
    if issubclass(cls, MovedEvent):
        kwargs["dst"] = data["dst"]
    return cls(**kwargs)


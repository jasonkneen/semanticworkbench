import asyncio
import datetime
import logging
from dataclasses import dataclass, field
from enum import StrEnum
from time import perf_counter
from typing import (
    IO,
    Any,
    AsyncContextManager,
    Awaitable,
    Callable,
    Generic,
    Literal,
    Mapping,
    Protocol,
    TypeVar,
    Union,
)

import typing_extensions
from semantic_workbench_api_model import workbench_model

from .context import AssistantContext, ConversationContext

logger = logging.getLogger(__name__)


@dataclass
class AssistantConversationInspectorStateDataModel:
    data: dict[str, Any]
    json_schema: dict[str, Any] | None = field(default=None)
    ui_schema: dict[str, Any] | None = field(default=None)


class ReadOnlyAssistantConversationInspectorStateProvider(Protocol):
    @property
    def display_name(self) -> str: ...
    @property
    def description(self) -> str: ...

    async def is_enabled(self, context: ConversationContext) -> bool: ...

    async def get(self, context: ConversationContext) -> AssistantConversationInspectorStateDataModel: ...


class WriteableAssistantConversationInspectorStateProvider(ReadOnlyAssistantConversationInspectorStateProvider):
    async def set(
        self,
        context: ConversationContext,
        data: dict[str, Any],
    ) -> None: ...


AssistantConversationInspectorStateProvider = typing_extensions.TypeAliasType(
    "AssistantConversationInspectorStateProvider",
    Union[
        ReadOnlyAssistantConversationInspectorStateProvider,
        WriteableAssistantConversationInspectorStateProvider,
    ],
)


class AssistantDataExporter(Protocol):
    """
    Protocol to support the export and import of assistant-managed state.
    """

    def export(self, context: AssistantContext) -> AsyncContextManager[IO[bytes]]: ...

    async def import_(self, context: AssistantContext, stream: IO[bytes]) -> None: ...


class ConversationDataExporter(Protocol):
    """
    Protocol to support the export and import of assistant-managed-conversation state.
    """

    def export(self, context: ConversationContext) -> AsyncContextManager[IO[bytes]]: ...

    async def import_(self, context: ConversationContext, stream: IO[bytes]) -> None: ...


@dataclass
class AssistantConfigDataModel:
    config: dict[str, Any]
    errors: list[str] | None = field(default=None)
    json_schema: dict[str, Any] | None = field(default=None)
    ui_schema: dict[str, Any] | None = field(default=None)


class AssistantConfigProvider(Protocol):
    async def get(self, assistant_context: AssistantContext) -> AssistantConfigDataModel: ...
    async def set(self, assistant_context: AssistantContext, config: dict[str, Any]) -> None: ...
    def default_for(self, template_id: str) -> AssistantConfigDataModel: ...


@dataclass
class AssistantTemplate:
    id: str
    name: str
    description: str


EventHandlerT = TypeVar("EventHandlerT")


IncludeEventsFromActors = Literal["all", "others", "this_assistant_service"]


class EventHandlerList(Generic[EventHandlerT], list[tuple[EventHandlerT, IncludeEventsFromActors]]):
    async def __call__(self, external_event: bool, *args, **kwargs):
        for handler, include in self:
            if external_event and include == "this_assistant_service":
                continue
            if not external_event and include == "others":
                continue

            handler_module = getattr(handler, "__module__", None)
            handler_name = getattr(handler, "__name__", None)
            start = perf_counter()
            try:
                if asyncio.iscoroutinefunction(handler):
                    await handler(*args, **kwargs)
                    continue

                if callable(handler):
                    handler(*args, **kwargs)
                    continue

            except Exception:
                logger.exception("error in event handler; name: %s.%s", handler_module, handler_name)
                return

            finally:
                end = perf_counter()
                logger.debug(
                    "event handler metrics; name: %s.%s, duration: %s",
                    handler_module,
                    handler_name,
                    datetime.timedelta(seconds=end - start),
                )

            raise TypeError(f"EventHandler {handler} is not a coroutine or callable")


class ObjectEventHandlers(Generic[EventHandlerT]):
    def __init__(self, on_created=True, on_updated=True, on_deleted=True) -> None:
        if on_created:
            self._on_created_handlers = EventHandlerList[EventHandlerT]()
            self.on_created = _create_decorator(self._on_created_handlers, "others")
            """event handler for created event; excluding events from this assistant service"""
            self.on_created_including_mine = _create_decorator(self._on_created_handlers, "all")
            """event handler for created event; including events from this assistant service"""

        if on_updated:
            self._on_updated_handlers = EventHandlerList[EventHandlerT]()
            self.on_updated = _create_decorator(self._on_updated_handlers, "others")
            """event handler for updated event; excluding events from this assistant service"""
            self.on_updated_including_mine = _create_decorator(self._on_updated_handlers, "all")
            """event handler for updated event; including events from this assistant service"""

        if on_deleted:
            self._on_deleted_handlers = EventHandlerList[EventHandlerT]()
            self.on_deleted = _create_decorator(self._on_deleted_handlers, "others")
            """event handler for deleted event; excluding events from this assistant service"""
            self.on_deleted_including_mine = _create_decorator(self._on_deleted_handlers, "all")
            """event handler for deleted event; including events from this assistant service"""


LifecycleEventHandler = Callable[[], Awaitable[None] | None]


class LifecycleEventHandlers:
    def __init__(self) -> None:
        self._on_service_start_handlers = EventHandlerList[LifecycleEventHandler]()
        self.on_service_start = _create_decorator(self._on_service_start_handlers, "all")

        self._on_service_shutdown_handlers = EventHandlerList[LifecycleEventHandler]()
        self.on_service_shutdown = _create_decorator(self._on_service_shutdown_handlers, "all")


def _create_decorator(
    handler_list: EventHandlerList[EventHandlerT], filter: IncludeEventsFromActors
) -> Callable[[EventHandlerT], EventHandlerT]:
    def _decorator(func: EventHandlerT) -> EventHandlerT:
        handler_list.append((func, filter))
        return func

    return _decorator


AssistantEventHandler = Callable[[AssistantContext], Awaitable[None] | None]

ConversationEventHandler = Callable[[ConversationContext], Awaitable[None] | None]

ConversationParticipantEventHandler = Callable[
    [ConversationContext, workbench_model.ConversationEvent, workbench_model.ConversationParticipant],
    Awaitable[None] | None,
]

ConversationMessageEventHandler = Callable[
    [ConversationContext, workbench_model.ConversationEvent, workbench_model.ConversationMessage],
    Awaitable[None] | None,
]

ConversationFileEventHandler = Callable[
    [
        ConversationContext,
        workbench_model.ConversationEvent,
        workbench_model.File,
    ],
    Awaitable[None] | None,
]

ServiceLifecycleEventHandler = Callable[[None], Awaitable[None] | None]


class MessageEvents(ObjectEventHandlers[ConversationMessageEventHandler]):
    def __init__(self) -> None:
        super().__init__(on_updated=False)

        self.chat = ObjectEventHandlers[ConversationMessageEventHandler](on_updated=False)
        self.log = ObjectEventHandlers[ConversationMessageEventHandler](on_updated=False)
        self.note = ObjectEventHandlers[ConversationMessageEventHandler](on_updated=False)
        self.notice = ObjectEventHandlers[ConversationMessageEventHandler](on_updated=False)
        self.command = ObjectEventHandlers[ConversationMessageEventHandler](on_updated=False)
        self.command_response = ObjectEventHandlers[ConversationMessageEventHandler](on_updated=False)
        # ensure we have an event handler for each message type
        for event_type in workbench_model.MessageType:
            assert getattr(self, str(event_type).replace("-", "_"))

    def __getitem__(self, key: workbench_model.MessageType) -> ObjectEventHandlers[ConversationMessageEventHandler]:
        match key:
            case workbench_model.MessageType.chat:
                return self.chat
            case workbench_model.MessageType.log:
                return self.log
            case workbench_model.MessageType.note:
                return self.note
            case workbench_model.MessageType.notice:
                return self.notice
            case workbench_model.MessageType.command:
                return self.command
            case workbench_model.MessageType.command_response:
                return self.command_response
            case _:
                raise KeyError(key)


class ConversationEvents(ObjectEventHandlers[ConversationEventHandler]):
    def __init__(self) -> None:
        super().__init__()

        self.participant = ObjectEventHandlers[ConversationParticipantEventHandler](on_deleted=False)
        self.file = ObjectEventHandlers[ConversationFileEventHandler]()
        self.message = MessageEvents()


class Events(LifecycleEventHandlers):
    def __init__(self) -> None:
        super().__init__()

        self.assistant = ObjectEventHandlers[AssistantEventHandler]()
        self.conversation = ConversationEvents()


class ContentInterceptor(Protocol):
    """
    Protocol to support the interception of incoming and outgoing messages.

    **Methods**
    - **intercept_incoming_event(context, event) -> ConversationEvent | None**
        - Intercept incoming events before they are processed by the assistant.
    - **intercept_outgoing_messages(context, messages) -> list[NewConversationMessage]**
        - Intercept outgoing messages before they are sent to the conversation.
    """

    async def intercept_incoming_event(
        self, context: ConversationContext, event: workbench_model.ConversationEvent
    ) -> workbench_model.ConversationEvent | None: ...

    async def intercept_outgoing_messages(
        self, context: ConversationContext, messages: list[workbench_model.NewConversationMessage]
    ) -> list[workbench_model.NewConversationMessage]: ...


class AssistantCapability(StrEnum):
    """Enum for the capabilities of the assistant."""

    supports_conversation_files = "supports_conversation_files"
    """Advertise support for awareness of files in the conversation."""

    supports_artifacts = "supports_artifacts"
    """Advertise support for artifacts in the conversation."""


class AssistantAppProtocol(Protocol):
    @property
    def events(self) -> Events: ...

    @property
    def assistant_service_id(self) -> str: ...

    @property
    def assistant_service_name(self) -> str: ...

    @property
    def assistant_service_description(self) -> str: ...

    @property
    def assistant_service_metadata(self) -> dict[str, Any]: ...

    @property
    def config_provider(self) -> AssistantConfigProvider: ...

    @property
    def templates(self) -> dict[str, AssistantTemplate]: ...

    @property
    def data_exporter(self) -> AssistantDataExporter: ...

    @property
    def conversation_data_exporter(self) -> ConversationDataExporter: ...

    @property
    def content_interceptor(self) -> ContentInterceptor | None: ...

    @property
    def inspector_state_providers(self) -> Mapping[str, AssistantConversationInspectorStateProvider]: ...

    def add_capability(self, capability: AssistantCapability) -> None: ...

    def add_inspector_state_provider(
        self,
        state_id: str,
        provider: AssistantConversationInspectorStateProvider,
    ) -> None: ...

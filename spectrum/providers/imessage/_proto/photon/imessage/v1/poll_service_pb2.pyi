from spectrum.providers.imessage._proto.photon.imessage.v1 import poll_types_pb2 as _poll_types_pb2
from spectrum.providers.imessage._proto.photon.imessage.v1 import streaming_pb2 as _streaming_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class CreatePollRequest(_message.Message):
    __slots__ = ("chat_guid", "title", "options", "client_message_id")
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    TITLE_FIELD_NUMBER: _ClassVar[int]
    OPTIONS_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    title: str
    options: _containers.RepeatedScalarFieldContainer[str]
    client_message_id: str
    def __init__(self, chat_guid: _Optional[str] = ..., title: _Optional[str] = ..., options: _Optional[_Iterable[str]] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class PollResponse(_message.Message):
    __slots__ = ("poll",)
    POLL_FIELD_NUMBER: _ClassVar[int]
    poll: _poll_types_pb2.PollInfo
    def __init__(self, poll: _Optional[_Union[_poll_types_pb2.PollInfo, _Mapping]] = ...) -> None: ...

class VotePollRequest(_message.Message):
    __slots__ = ("poll_message_guid", "option_identifier", "client_message_id")
    POLL_MESSAGE_GUID_FIELD_NUMBER: _ClassVar[int]
    OPTION_IDENTIFIER_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    poll_message_guid: str
    option_identifier: str
    client_message_id: str
    def __init__(self, poll_message_guid: _Optional[str] = ..., option_identifier: _Optional[str] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class UnvotePollRequest(_message.Message):
    __slots__ = ("poll_message_guid", "client_message_id")
    POLL_MESSAGE_GUID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    poll_message_guid: str
    client_message_id: str
    def __init__(self, poll_message_guid: _Optional[str] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class AddPollOptionRequest(_message.Message):
    __slots__ = ("poll_message_guid", "option_text", "client_message_id")
    POLL_MESSAGE_GUID_FIELD_NUMBER: _ClassVar[int]
    OPTION_TEXT_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    poll_message_guid: str
    option_text: str
    client_message_id: str
    def __init__(self, poll_message_guid: _Optional[str] = ..., option_text: _Optional[str] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class GetPollRequest(_message.Message):
    __slots__ = ("poll_message_guid",)
    POLL_MESSAGE_GUID_FIELD_NUMBER: _ClassVar[int]
    poll_message_guid: str
    def __init__(self, poll_message_guid: _Optional[str] = ...) -> None: ...

class SubscribePollEventsRequest(_message.Message):
    __slots__ = ("poll_message_guid",)
    POLL_MESSAGE_GUID_FIELD_NUMBER: _ClassVar[int]
    poll_message_guid: str
    def __init__(self, poll_message_guid: _Optional[str] = ...) -> None: ...

class SubscribePollEventsResponse(_message.Message):
    __slots__ = ("sequence", "poll_changed", "heartbeat")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    POLL_CHANGED_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    poll_changed: _poll_types_pb2.PollChangeEvent
    heartbeat: _streaming_pb2.Heartbeat
    def __init__(self, sequence: _Optional[int] = ..., poll_changed: _Optional[_Union[_poll_types_pb2.PollChangeEvent, _Mapping]] = ..., heartbeat: _Optional[_Union[_streaming_pb2.Heartbeat, _Mapping]] = ...) -> None: ...

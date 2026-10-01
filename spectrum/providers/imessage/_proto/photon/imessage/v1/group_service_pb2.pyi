from google.protobuf import empty_pb2 as _empty_pb2
from spectrum.providers.imessage._proto.photon.imessage.v1 import chat_types_pb2 as _chat_types_pb2
from spectrum.providers.imessage._proto.photon.imessage.v1 import group_types_pb2 as _group_types_pb2
from spectrum.providers.imessage._proto.photon.imessage.v1 import streaming_pb2 as _streaming_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class SetDisplayNameRequest(_message.Message):
    __slots__ = ("chat_guid", "display_name", "client_message_id")
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    DISPLAY_NAME_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    display_name: str
    client_message_id: str
    def __init__(self, chat_guid: _Optional[str] = ..., display_name: _Optional[str] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class SetDisplayNameResponse(_message.Message):
    __slots__ = ("chat",)
    CHAT_FIELD_NUMBER: _ClassVar[int]
    chat: _chat_types_pb2.Chat
    def __init__(self, chat: _Optional[_Union[_chat_types_pb2.Chat, _Mapping]] = ...) -> None: ...

class AddParticipantsRequest(_message.Message):
    __slots__ = ("chat_guid", "addresses", "client_message_id")
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    addresses: _containers.RepeatedScalarFieldContainer[str]
    client_message_id: str
    def __init__(self, chat_guid: _Optional[str] = ..., addresses: _Optional[_Iterable[str]] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class AddParticipantsResponse(_message.Message):
    __slots__ = ("chat",)
    CHAT_FIELD_NUMBER: _ClassVar[int]
    chat: _chat_types_pb2.Chat
    def __init__(self, chat: _Optional[_Union[_chat_types_pb2.Chat, _Mapping]] = ...) -> None: ...

class RemoveParticipantsRequest(_message.Message):
    __slots__ = ("chat_guid", "addresses", "client_message_id")
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    ADDRESSES_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    addresses: _containers.RepeatedScalarFieldContainer[str]
    client_message_id: str
    def __init__(self, chat_guid: _Optional[str] = ..., addresses: _Optional[_Iterable[str]] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class RemoveParticipantsResponse(_message.Message):
    __slots__ = ("chat",)
    CHAT_FIELD_NUMBER: _ClassVar[int]
    chat: _chat_types_pb2.Chat
    def __init__(self, chat: _Optional[_Union[_chat_types_pb2.Chat, _Mapping]] = ...) -> None: ...

class LeaveGroupRequest(_message.Message):
    __slots__ = ("chat_guid", "client_message_id")
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    client_message_id: str
    def __init__(self, chat_guid: _Optional[str] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class SetIconRequest(_message.Message):
    __slots__ = ("chat_guid", "data", "client_message_id")
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    DATA_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    data: bytes
    client_message_id: str
    def __init__(self, chat_guid: _Optional[str] = ..., data: _Optional[bytes] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class RemoveIconRequest(_message.Message):
    __slots__ = ("chat_guid", "client_message_id")
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    client_message_id: str
    def __init__(self, chat_guid: _Optional[str] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class GetIconRequest(_message.Message):
    __slots__ = ("chat_guid",)
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    def __init__(self, chat_guid: _Optional[str] = ...) -> None: ...

class GetIconResponse(_message.Message):
    __slots__ = ("data", "mime_type")
    DATA_FIELD_NUMBER: _ClassVar[int]
    MIME_TYPE_FIELD_NUMBER: _ClassVar[int]
    data: bytes
    mime_type: str
    def __init__(self, data: _Optional[bytes] = ..., mime_type: _Optional[str] = ...) -> None: ...

class SubscribeGroupEventsRequest(_message.Message):
    __slots__ = ("chat_guid",)
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    def __init__(self, chat_guid: _Optional[str] = ...) -> None: ...

class SubscribeGroupEventsResponse(_message.Message):
    __slots__ = ("sequence", "group_changed", "heartbeat")
    SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    GROUP_CHANGED_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    sequence: int
    group_changed: _group_types_pb2.GroupChangeEvent
    heartbeat: _streaming_pb2.Heartbeat
    def __init__(self, sequence: _Optional[int] = ..., group_changed: _Optional[_Union[_group_types_pb2.GroupChangeEvent, _Mapping]] = ..., heartbeat: _Optional[_Union[_streaming_pb2.Heartbeat, _Mapping]] = ...) -> None: ...

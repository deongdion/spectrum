from spectrum.providers.imessage._proto.photon.imessage.v1 import location_types_pb2 as _location_types_pb2
from spectrum.providers.imessage._proto.photon.imessage.v1 import streaming_pb2 as _streaming_pb2
from google.protobuf.internal import containers as _containers
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Iterable as _Iterable, Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class ListSharedFriendLocationsRequest(_message.Message):
    __slots__ = ()
    def __init__(self) -> None: ...

class ListSharedFriendLocationsResponse(_message.Message):
    __slots__ = ("locations",)
    LOCATIONS_FIELD_NUMBER: _ClassVar[int]
    locations: _containers.RepeatedCompositeFieldContainer[_location_types_pb2.SharedFriendLocation]
    def __init__(self, locations: _Optional[_Iterable[_Union[_location_types_pb2.SharedFriendLocation, _Mapping]]] = ...) -> None: ...

class GetSharedFriendLocationRequest(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class GetSharedFriendLocationResponse(_message.Message):
    __slots__ = ("location",)
    LOCATION_FIELD_NUMBER: _ClassVar[int]
    location: _location_types_pb2.SharedFriendLocation
    def __init__(self, location: _Optional[_Union[_location_types_pb2.SharedFriendLocation, _Mapping]] = ...) -> None: ...

class RequestFriendLocationSharingRequest(_message.Message):
    __slots__ = ("chat_guid", "address", "client_message_id")
    CHAT_GUID_FIELD_NUMBER: _ClassVar[int]
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    CLIENT_MESSAGE_ID_FIELD_NUMBER: _ClassVar[int]
    chat_guid: str
    address: str
    client_message_id: str
    def __init__(self, chat_guid: _Optional[str] = ..., address: _Optional[str] = ..., client_message_id: _Optional[str] = ...) -> None: ...

class RequestFriendLocationSharingResponse(_message.Message):
    __slots__ = ("address", "status", "reason", "message_guid")
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    STATUS_FIELD_NUMBER: _ClassVar[int]
    REASON_FIELD_NUMBER: _ClassVar[int]
    MESSAGE_GUID_FIELD_NUMBER: _ClassVar[int]
    address: str
    status: str
    reason: str
    message_guid: str
    def __init__(self, address: _Optional[str] = ..., status: _Optional[str] = ..., reason: _Optional[str] = ..., message_guid: _Optional[str] = ...) -> None: ...

class WatchSharedFriendLocationsRequest(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class WatchSharedFriendLocationsResponse(_message.Message):
    __slots__ = ("location_updated", "heartbeat")
    LOCATION_UPDATED_FIELD_NUMBER: _ClassVar[int]
    HEARTBEAT_FIELD_NUMBER: _ClassVar[int]
    location_updated: _location_types_pb2.SharedFriendLocationUpdated
    heartbeat: _streaming_pb2.Heartbeat
    def __init__(self, location_updated: _Optional[_Union[_location_types_pb2.SharedFriendLocationUpdated, _Mapping]] = ..., heartbeat: _Optional[_Union[_streaming_pb2.Heartbeat, _Mapping]] = ...) -> None: ...

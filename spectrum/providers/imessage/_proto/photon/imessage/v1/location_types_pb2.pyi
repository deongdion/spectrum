import datetime

from google.protobuf import timestamp_pb2 as _timestamp_pb2
from google.protobuf.internal import enum_type_wrapper as _enum_type_wrapper
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class FriendLocationType(int, metaclass=_enum_type_wrapper.EnumTypeWrapper):
    __slots__ = ()
    FRIEND_LOCATION_TYPE_UNSPECIFIED: _ClassVar[FriendLocationType]
    FRIEND_LOCATION_TYPE_LEGACY: _ClassVar[FriendLocationType]
    FRIEND_LOCATION_TYPE_LIVE: _ClassVar[FriendLocationType]
    FRIEND_LOCATION_TYPE_SHALLOW: _ClassVar[FriendLocationType]
FRIEND_LOCATION_TYPE_UNSPECIFIED: FriendLocationType
FRIEND_LOCATION_TYPE_LEGACY: FriendLocationType
FRIEND_LOCATION_TYPE_LIVE: FriendLocationType
FRIEND_LOCATION_TYPE_SHALLOW: FriendLocationType

class SharedFriendLocation(_message.Message):
    __slots__ = ("address", "name", "latitude", "longitude", "accuracy", "long_address", "short_address", "is_locating_in_progress", "location_type", "location_timestamp", "expires_at")
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    NAME_FIELD_NUMBER: _ClassVar[int]
    LATITUDE_FIELD_NUMBER: _ClassVar[int]
    LONGITUDE_FIELD_NUMBER: _ClassVar[int]
    ACCURACY_FIELD_NUMBER: _ClassVar[int]
    LONG_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    SHORT_ADDRESS_FIELD_NUMBER: _ClassVar[int]
    IS_LOCATING_IN_PROGRESS_FIELD_NUMBER: _ClassVar[int]
    LOCATION_TYPE_FIELD_NUMBER: _ClassVar[int]
    LOCATION_TIMESTAMP_FIELD_NUMBER: _ClassVar[int]
    EXPIRES_AT_FIELD_NUMBER: _ClassVar[int]
    address: str
    name: str
    latitude: float
    longitude: float
    accuracy: float
    long_address: str
    short_address: str
    is_locating_in_progress: bool
    location_type: FriendLocationType
    location_timestamp: _timestamp_pb2.Timestamp
    expires_at: _timestamp_pb2.Timestamp
    def __init__(self, address: _Optional[str] = ..., name: _Optional[str] = ..., latitude: _Optional[float] = ..., longitude: _Optional[float] = ..., accuracy: _Optional[float] = ..., long_address: _Optional[str] = ..., short_address: _Optional[str] = ..., is_locating_in_progress: _Optional[bool] = ..., location_type: _Optional[_Union[FriendLocationType, str]] = ..., location_timestamp: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ..., expires_at: _Optional[_Union[datetime.datetime, _timestamp_pb2.Timestamp, _Mapping]] = ...) -> None: ...

class SharedFriendLocationUpdated(_message.Message):
    __slots__ = ("source_sequence", "location")
    SOURCE_SEQUENCE_FIELD_NUMBER: _ClassVar[int]
    LOCATION_FIELD_NUMBER: _ClassVar[int]
    source_sequence: int
    location: SharedFriendLocation
    def __init__(self, source_sequence: _Optional[int] = ..., location: _Optional[_Union[SharedFriendLocation, _Mapping]] = ...) -> None: ...

from spectrum.providers.imessage._proto.photon.imessage.v1 import address_types_pb2 as _address_types_pb2
from google.protobuf import descriptor as _descriptor
from google.protobuf import message as _message
from collections.abc import Mapping as _Mapping
from typing import ClassVar as _ClassVar, Optional as _Optional, Union as _Union

DESCRIPTOR: _descriptor.FileDescriptor

class GetAddressInfoRequest(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class GetAddressInfoResponse(_message.Message):
    __slots__ = ("info",)
    INFO_FIELD_NUMBER: _ClassVar[int]
    info: _address_types_pb2.MultiServiceAddressInfo
    def __init__(self, info: _Optional[_Union[_address_types_pb2.MultiServiceAddressInfo, _Mapping]] = ...) -> None: ...

class GetFocusStatusRequest(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class GetFocusStatusResponse(_message.Message):
    __slots__ = ("is_silenced_by_focus",)
    IS_SILENCED_BY_FOCUS_FIELD_NUMBER: _ClassVar[int]
    is_silenced_by_focus: bool
    def __init__(self, is_silenced_by_focus: _Optional[bool] = ...) -> None: ...

class GetIMessageAvailabilityRequest(_message.Message):
    __slots__ = ("address",)
    ADDRESS_FIELD_NUMBER: _ClassVar[int]
    address: str
    def __init__(self, address: _Optional[str] = ...) -> None: ...

class GetIMessageAvailabilityResponse(_message.Message):
    __slots__ = ("is_available",)
    IS_AVAILABLE_FIELD_NUMBER: _ClassVar[int]
    is_available: bool
    def __init__(self, is_available: _Optional[bool] = ...) -> None: ...

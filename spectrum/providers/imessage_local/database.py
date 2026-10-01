"""Read-only access to the macOS Messages database (``~/Library/Messages/chat.db``).

The process needs **Full Disk Access** (System Settings → Privacy & Security). Message text
lives in ``message.text`` or, on newer macOS releases, only inside ``attributedBody`` (an
archived ``NSAttributedString``), which ``decode_attributed_body`` extracts.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=UTC)
DEFAULT_DB = Path("~/Library/Messages/chat.db").expanduser()

# chat.style: 43 = group chat, 45 = one-to-one
GROUP_STYLE = 43


@dataclass(frozen=True, slots=True)
class AttachmentRow:
    path: Path
    name: str
    mime_type: str | None
    size: int | None


@dataclass(slots=True)
class MessageRow:
    rowid: int
    guid: str
    text: str | None
    is_from_me: bool
    date: datetime
    service: str | None
    handle: str | None
    chat_guid: str | None
    is_group: bool
    item_type: int
    reaction_type: int
    reaction_target: str | None
    attachments: list[AttachmentRow] = field(default_factory=list)


def apple_time(value: int | None) -> datetime:
    """Messages stores nanoseconds (or seconds on very old macOS) since 2001-01-01 UTC."""
    if not value:
        return datetime.now(UTC)
    seconds = value / 1e9 if value > 10**11 else float(value)
    return APPLE_EPOCH + timedelta(seconds=seconds)


def decode_attributed_body(blob: bytes | None) -> str | None:
    """Extract the plain string from an archived NSAttributedString (typedstream)."""
    if not blob:
        return None
    start = blob.find(b"NSString")
    if start < 0:
        return None
    plus = blob.find(b"+", start + len(b"NSString"))
    if plus < 0 or plus + 1 >= len(blob):
        return None
    i = plus + 1
    marker = blob[i]
    i += 1
    if marker == 0x81:  # 16-bit length follows
        length = int.from_bytes(blob[i : i + 2], "little")
        i += 2
    elif marker == 0x82:  # 32-bit length follows
        length = int.from_bytes(blob[i : i + 4], "little")
        i += 4
    else:
        length = marker
    return blob[i : i + length].decode("utf-8", errors="replace") or None


_MESSAGES_SQL = """
SELECT m.ROWID, m.guid, m.text, m.attributedBody, m.is_from_me, m.date, m.service,
       m.cache_has_attachments, m.item_type,
       COALESCE(m.associated_message_type, 0), m.associated_message_guid,
       h.id, c.guid, c.style
FROM message m
LEFT JOIN handle h ON h.ROWID = m.handle_id
LEFT JOIN chat_message_join cmj ON cmj.message_id = m.ROWID
LEFT JOIN chat c ON c.ROWID = cmj.chat_id
WHERE m.ROWID > ?
ORDER BY m.ROWID
LIMIT ?
"""

_ATTACHMENTS_SQL = """
SELECT a.filename, a.transfer_name, a.mime_type, a.total_bytes
FROM attachment a
JOIN message_attachment_join maj ON maj.attachment_id = a.ROWID
WHERE maj.message_id = ?
ORDER BY a.ROWID
"""

_BY_GUID_SQL = _MESSAGES_SQL.replace("WHERE m.ROWID > ?\nORDER BY m.ROWID\nLIMIT ?", "WHERE m.guid = ?")


class MessagesDatabase:
    def __init__(self, path: Path = DEFAULT_DB) -> None:
        self._path = Path(path).expanduser()
        self._conn: sqlite3.Connection | None = None

    @property
    def path(self) -> Path:
        return self._path

    def open(self) -> None:
        if not self._path.exists():
            raise FileNotFoundError(f"Messages database not found at {self._path}")
        uri = f"file:{self._path}?mode=ro"
        self._conn = sqlite3.connect(uri, uri=True, check_same_thread=False, timeout=5)
        self._conn.execute("SELECT 1 FROM message LIMIT 1").fetchall()  # fails without Full Disk Access

    def close(self) -> None:
        if self._conn is not None:
            self._conn.close()
            self._conn = None

    def _db(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("database is not open")
        return self._conn

    def max_rowid(self) -> int:
        row = self._db().execute("SELECT COALESCE(MAX(ROWID), 0) FROM message").fetchone()
        return int(row[0])

    def messages_after(self, rowid: int, limit: int = 200) -> list[MessageRow]:
        return [self._row(r) for r in self._db().execute(_MESSAGES_SQL, (rowid, limit)).fetchall()]

    def message_by_guid(self, guid: str) -> MessageRow | None:
        row = self._db().execute(_BY_GUID_SQL, (guid,)).fetchone()
        return self._row(row) if row else None

    def _row(self, r: tuple) -> MessageRow:
        (rowid, guid, text, body, from_me, date, service, has_att, item_type,
         assoc_type, assoc_guid, handle, chat_guid, style) = r  # fmt: skip
        message = MessageRow(
            rowid=rowid,
            guid=guid,
            text=text if text else decode_attributed_body(body),
            is_from_me=bool(from_me),
            date=apple_time(date),
            service=service,
            handle=handle,
            chat_guid=chat_guid,
            is_group=style == GROUP_STYLE,
            item_type=item_type or 0,
            reaction_type=assoc_type or 0,
            reaction_target=assoc_guid,
        )
        if has_att:
            for filename, transfer_name, mime, size in self._db().execute(_ATTACHMENTS_SQL, (rowid,)):
                if not filename:
                    continue
                path = Path(filename).expanduser()
                message.attachments.append(AttachmentRow(path, transfer_name or path.name, mime, size))
        return message

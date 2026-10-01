"""Minimal vCard 3.0 serialization for contact cards."""

from __future__ import annotations

import re

from ..models.content import Contact, ContactAddress, ContactField, ContactName, ContactOrg
from ..models.enums import ContactFieldType

_TYPE_PARAM = re.compile(r"TYPE=([^;:]+)", re.IGNORECASE)


def _escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace(",", "\\,").replace(";", "\\;")


def _unescape(value: str) -> str:
    return (
        value.replace("\\n", "\n")
        .replace("\\N", "\n")
        .replace("\\,", ",")
        .replace("\\;", ";")
        .replace("\\\\", "\\")
    )


def _field_type(params: str) -> ContactFieldType | None:
    for match in _TYPE_PARAM.finditer(params):
        for token in match.group(1).lower().split(","):
            token = {"cell": "mobile", "iphone": "mobile"}.get(token, token)
            try:
                return ContactFieldType(token)
            except ValueError:
                continue
    return None


def to_vcard(contact: Contact) -> str:
    lines = ["BEGIN:VCARD", "VERSION:3.0"]
    name = contact.name or ContactName()
    display = name.display or (contact.user.id if contact.user else None) or "Contact"
    lines.append(f"FN:{_escape(display)}")
    lines.append(
        "N:"
        + ";".join(_escape(p or "") for p in (name.last, name.first, name.middle, name.prefix, name.suffix))
    )
    phones = list(contact.phones)
    if not phones and contact.user and contact.user.id.startswith("+"):
        phones.append(ContactField(contact.user.id, ContactFieldType.MOBILE))
    emails = list(contact.emails)
    if not emails and contact.user and "@" in contact.user.id:
        emails.append(ContactField(contact.user.id))
    for phone in phones:
        kind = {"mobile": "CELL"}.get(phone.type or "", (phone.type or "").upper())
        lines.append(f"TEL{';TYPE=' + kind if kind else ''}:{phone.value}")
    for email in emails:
        lines.append(f"EMAIL{';TYPE=' + email.type.upper() if email.type else ''}:{email.value}")
    for addr in contact.addresses:
        parts = [
            "",
            "",
            addr.street or "",
            addr.city or "",
            addr.region or "",
            addr.postal_code or "",
            addr.country or "",
        ]
        lines.append(
            f"ADR{';TYPE=' + addr.type.upper() if addr.type else ''}:" + ";".join(_escape(p) for p in parts)
        )
    if contact.org:
        if contact.org.name or contact.org.department:
            lines.append(f"ORG:{_escape(contact.org.name or '')};{_escape(contact.org.department or '')}")
        if contact.org.title:
            lines.append(f"TITLE:{_escape(contact.org.title)}")
    for url in contact.urls:
        lines.append(f"URL:{url}")
    if contact.birthday:
        lines.append(f"BDAY:{contact.birthday}")
    if contact.note:
        lines.append(f"NOTE:{_escape(contact.note)}")
    lines.append("END:VCARD")
    return "\r\n".join(lines) + "\r\n"


def from_vcard(text: str) -> Contact:
    # unfold continuation lines (RFC 6350 §3.2)
    unfolded = re.sub(r"\r?\n[ \t]", "", text)
    name = ContactName()
    formatted: str | None = None
    phones: list[ContactField] = []
    emails: list[ContactField] = []
    addresses: list[ContactAddress] = []
    org_name = org_dept = title = None
    urls: list[str] = []
    birthday = note = None

    for line in unfolded.splitlines():
        if ":" not in line:
            continue
        head, _, value = line.partition(":")
        key, _, params = head.partition(";")
        key = key.split(".")[-1].upper()  # drop item1. grouping prefixes
        if key == "FN":
            formatted = _unescape(value)
        elif key == "N":
            parts = [_unescape(p) or None for p in value.split(";")] + [None] * 5
            name = ContactName(
                last=parts[0], first=parts[1], middle=parts[2], prefix=parts[3], suffix=parts[4]
            )
        elif key == "TEL":
            phones.append(ContactField(value.strip(), _field_type(params)))
        elif key == "EMAIL":
            emails.append(ContactField(value.strip(), _field_type(params)))
        elif key == "ADR":
            p = [_unescape(x) or None for x in value.split(";")] + [None] * 7
            addresses.append(ContactAddress(p[2], p[3], p[4], p[5], p[6], _field_type(params)))
        elif key == "ORG":
            org_parts = [_unescape(x) or None for x in value.split(";")] + [None]
            org_name, org_dept = org_parts[0], org_parts[1]
        elif key == "TITLE":
            title = _unescape(value)
        elif key == "URL":
            urls.append(value.strip())
        elif key == "BDAY":
            birthday = value.strip()
        elif key == "NOTE":
            note = _unescape(value)

    name = ContactName(formatted, name.first, name.last, name.middle, name.prefix, name.suffix)
    org = ContactOrg(org_name, title, org_dept) if (org_name or title or org_dept) else None
    return Contact(
        name=name,
        phones=phones,
        emails=emails,
        addresses=addresses,
        org=org,
        urls=urls,
        birthday=birthday,
        note=note,
        raw=text,
    )


def vcard_file_name(contact: Contact) -> str:
    base = (
        (contact.name.display if contact.name else None)
        or (contact.user.id if contact.user else None)
        or "contact"
    )
    return re.sub(r"[^A-Za-z0-9_\-.]", "_", base) + ".vcf"

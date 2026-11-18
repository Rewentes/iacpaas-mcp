"""Normalize an operator-selected OpenAI-compatible API address."""

import ipaddress
from urllib.parse import urlsplit, urlunsplit


def normalize_base_url(value):
    message = "Укажите адрес API вида https://example.com/v1 или http://localhost:1234/v1."
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise ValueError(message)
    value = value.strip()
    if any(ord(char) <= 32 for char in value) or "\\" in value:
        raise ValueError(message)
    try:
        parsed = urlsplit(value)
        host = parsed.hostname
        port = parsed.port
        if (
            parsed.scheme not in {"http", "https"}
            or not host
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or port == 0
        ):
            raise ValueError
        try:
            address = ipaddress.ip_address(host)
        except ValueError:
            address = None
        if address and (address.is_link_local or address.is_multicast or address.is_unspecified):
            raise ValueError
        if host.casefold().rstrip(".") == "metadata.google.internal":
            raise ValueError
    except ValueError:
        raise ValueError(message) from None
    path = parsed.path.rstrip("/")
    if path.endswith("/chat/completions"):
        path = path[: -len("/chat/completions")].rstrip("/")
    return urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))

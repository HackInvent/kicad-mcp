"""Standard-library validation shared by HTTP startup and session discovery."""

import re


_LOCAL_HOST = re.compile(r"(?:127\.0\.0\.1|localhost)(?::([0-9]{1,5}))?", re.IGNORECASE)


def validate_http_token(token: str) -> None:
    """Reject configuration that cannot round-trip through an HTTP auth header."""
    if not isinstance(token, str) or not token or any(not 33 <= ord(char) <= 126 for char in token):
        raise ValueError("HTTP bearer token must contain only visible ASCII characters without whitespace")


def valid_local_host(host: str) -> bool:
    match = _LOCAL_HOST.fullmatch(host)
    return match is not None and (match[1] is None or 1 <= int(match[1]) <= 65535)

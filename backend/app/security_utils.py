"""Secret redaction, URL safety (SSRF guard) and URL validation shared across the app."""
from __future__ import annotations

import ipaddress
import logging
import re
import socket
from urllib.parse import urlsplit

# ---------- redaction ----------
_PATTERNS = [
    (re.compile(r"(?i)\b(api[_-]?key|apikey|access[_-]?token|token|secret|password|passwd|signature)=([^&\s'\"]+)"), r"\1=[REDACTED]"),
    (re.compile(r"sk-ant-[A-Za-z0-9_\-]+"), "[REDACTED]"),
    (re.compile(r"(?i)\b(bearer|basic)\s+[A-Za-z0-9._~+/=\-]{8,}"), r"\1 [REDACTED]"),
    (re.compile(r"(?i)(x-admin-token|authorization|x-api-key)\s*[:=]\s*\S+"), r"\1: [REDACTED]"),
]


def redact(text: object) -> str:
    """Strip anything that looks like a credential from a string before it is logged or stored."""
    s = str(text)
    for pat, repl in _PATTERNS:
        s = pat.sub(repl, s)
    return s


class RedactingFilter(logging.Filter):
    def filter(self, record: logging.LogRecord) -> bool:
        try:
            record.msg = redact(record.getMessage())
            record.args = ()
        except Exception:
            pass
        return True


# ---------- URL safety ----------
ALLOWED_PORTS = {80, 443}


class UnsafeUrl(ValueError):
    pass


def _bad_ip(ip: ipaddress._BaseAddress) -> bool:
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or (isinstance(ip, ipaddress.IPv4Address) and ip in ipaddress.ip_network("100.64.0.0/10")))


def assert_public_url(url: str) -> None:
    """Raise UnsafeUrl unless url is http(s) on a standard port and EVERY address its host resolves to is public.
    Call it for the first URL and for every redirect hop. (Residual risk: DNS can change between this check and
    the connection; the pages fetched here only feed an extractor and are never executed or relayed back.)"""
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as e:
        raise UnsafeUrl(f"malformed url: {e}") from None
    if parts.scheme not in ("http", "https"):
        raise UnsafeUrl(f"scheme {parts.scheme!r} not allowed")
    if parts.username or parts.password:
        raise UnsafeUrl("credentials in url")
    host = parts.hostname
    if not host:
        raise UnsafeUrl("no host")
    if (port or (443 if parts.scheme == "https" else 80)) not in ALLOWED_PORTS:
        raise UnsafeUrl(f"port {port} not allowed")
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as e:
        raise UnsafeUrl(f"cannot resolve {host}") from None
    if not infos:
        raise UnsafeUrl(f"cannot resolve {host}")
    for info in infos:
        ip = ipaddress.ip_address(info[4][0].split("%")[0])
        if _bad_ip(ip):
            raise UnsafeUrl(f"{host} resolves to a non-public address")


def safe_job_url(url: str | None) -> str | None:
    """Validate a URL we will store and later show to users as a clickable link. http(s) only, no credentials,
    no control characters, bounded length. Returns the stripped URL or None. No DNS lookup (storing is not fetching)."""
    if not url:
        return None
    u = url.strip()
    if len(u) > 2000 or re.search(r"[\x00-\x20\x7f]", u):
        return None
    try:
        p = urlsplit(u)
        _ = p.port
    except ValueError:
        return None
    if p.scheme not in ("http", "https") or not p.hostname or p.username or p.password:
        return None
    return u

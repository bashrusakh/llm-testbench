"""Endpoint URL safety check.

User-supplied ``base_url`` is used in outbound HTTP requests. Block
obvious SSRF targets (cloud metadata endpoints, RFC1918, link-local,
loopback for IPv4 and IPv6) so a malicious caller can't reach internal
services. Resolution-based check catches literal IPs, short names, and
CNAMEs pointing at private addresses.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _validate_endpoint_url(base_url: str) -> None:
    """Reject URLs that point at private/loopback/link-local ranges.

    User-supplied ``base_url`` is used in outbound HTTP requests. Block
    obvious SSRF targets (cloud metadata endpoints, RFC1918, link-local,
    loopback for IPv4 and IPv6). Resolve the hostname first so a literal
    IP, a short name, or a CNAME pointing to a private address all fail.
    """
    parsed = urlparse(base_url)
    if parsed.scheme not in {"http", "https"}:
        raise ValueError(f"Endpoint URL scheme must be http or https, got {parsed.scheme!r}")
    host = (parsed.hostname or "").strip()
    if not host:
        raise ValueError("Endpoint URL must include a hostname")
    try:
        infos = socket.getaddrinfo(host, None)
    except socket.gaierror as exc:
        raise ValueError(f"Could not resolve endpoint hostname {host!r}: {exc}") from exc
    for info in infos:
        sockaddr = info[4]
        ip = ipaddress.ip_address(sockaddr[0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Endpoint URL resolves to a non-public address ({ip}); "
                "refusing to issue requests to private/loopback/link-local hosts"
            )

"""Endpoint URL safety check.

User-supplied ``base_url`` is used in outbound HTTP requests. The
benchmark server is local-only (defaults to 127.0.0.1) and its primary
purpose is benchmarking locally-running LLM servers (Ollama on 11434,
LM Studio on 1234, llama.cpp on 8080, etc.) and self-hosted OpenAI-
compatible endpoints on a LAN. So we explicitly allow loopback and
RFC1918 — blocking them breaks the primary use case.

What we still block is the realistic SSRF attack surface: cloud
metadata endpoints (AWS/GCP/Azure expose them at 169.254.169.254 and
the IPv6 link-local fe80::/10 range), plus address ranges that can
never be a real LLM server (multicast, reserved, unspecified).
Resolution-based check catches literal IPs, short names, and CNAMEs
pointing at the blocked ranges.
"""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


def _validate_endpoint_url(base_url: str) -> None:
    """Reject only the actually-dangerous endpoint targets.

    Blocks: bad scheme, cloud metadata link-local (169.254.0.0/16,
    fe80::/10 and friends), multicast, reserved, and unspecified
    addresses. Allows loopback (127.0.0.0/8, ::1) and RFC1918
    (10/8, 172.16/12, 192.168/16) because that is where local LLM
    servers live. Resolve the hostname first so a literal IP, a short
    name, or a CNAME pointing to a blocked range all fail.
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
            ip.is_link_local
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise ValueError(
                f"Endpoint URL resolves to a non-routable address ({ip}); "
                "refusing to issue requests to link-local/multicast/unspecified hosts"
            )

"""URL normalization and SSRF protection for targets (ADR-0010, threat model A1/A5).

A target host must resolve only to public unicast addresses. Private, loopback, link-local,
CGNAT, multicast, reserved and cloud-metadata ranges are rejected (unless
``RE_ALLOW_PRIVATE_TARGETS`` is set for local development).
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

from reliability.platform.errors import AppError
from reliability.platform.settings import get_settings

Resolver = Callable[[str], list[str]]

_BLOCKED_NETWORKS = [
    ipaddress.ip_network(n)
    for n in ("100.64.0.0/10", "169.254.0.0/16", "192.0.0.0/24", "198.18.0.0/15", "fd00:ec2::/32", "64:ff9b::/96")
]
_BLOCKED_HOSTNAMES = {"localhost", "metadata.google.internal", "metadata", "instance-data"}


@dataclass(frozen=True)
class NormalizedUrl:
    base_url: str
    scheme: str
    host: str
    port: int | None


def normalize_url(raw: str) -> NormalizedUrl:
    parts = urlsplit(raw.strip())
    if parts.scheme not in ("http", "https"):
        raise AppError("VALIDATION_FAILED", "target URL must use http or https")
    if parts.username or parts.password:
        raise AppError("VALIDATION_FAILED", "target URL must not contain credentials")
    if not parts.hostname:
        raise AppError("VALIDATION_FAILED", "target URL has no host")
    try:
        host = parts.hostname.encode("idna").decode("ascii").lower().rstrip(".")
    except UnicodeError as exc:
        raise AppError("VALIDATION_FAILED", "invalid host name") from exc
    port = parts.port
    netloc = host if port is None else f"{host}:{port}"
    path = parts.path.rstrip("/")
    return NormalizedUrl(urlunsplit((parts.scheme, netloc, path, "", "")), parts.scheme, host, port)


def system_resolver(host: str) -> list[str]:
    try:
        infos = socket.getaddrinfo(host, None, proto=socket.IPPROTO_TCP)
    except socket.gaierror as exc:
        raise AppError("VALIDATION_FAILED", f"host {host} does not resolve") from exc
    return sorted({str(info[4][0]) for info in infos})


def is_public_address(addr: str) -> bool:
    ip = ipaddress.ip_address(addr)
    if isinstance(ip, ipaddress.IPv6Address) and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
        return False
    return not any(ip in net for net in _BLOCKED_NETWORKS)


def check_host_is_public(host: str, resolver: Resolver = system_resolver) -> list[str]:
    """Resolve ``host`` and fail with UNSAFE_TARGET unless every address is public."""
    if get_settings().allow_private_targets:
        return resolver(host)
    if host in _BLOCKED_HOSTNAMES or host.endswith((".internal", ".local", ".localhost")):
        raise AppError("UNSAFE_TARGET", "target host is not allowed", {"host": host})
    addresses = resolver(host)
    unsafe = [a for a in addresses if not is_public_address(a)]
    if not addresses or unsafe:
        raise AppError("UNSAFE_TARGET", "target resolves to a non-public address", {"host": host, "addresses": unsafe})
    return addresses

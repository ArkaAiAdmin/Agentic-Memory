"""Shared SSRF guard utilities for agentic-memory (OWASP A10-001).

Extracted from multi_modal.py so authlib_sso.py, alert.py, and any future
outbound-request path can validate URLs and hosts before fetching.
"""

from __future__ import annotations

import ipaddress
import socket
import urllib.request
from urllib.parse import urlparse

# Ranges that must never be fetched (metadata services, internal networks).
_BLOCKED_NETWORKS = (
    ipaddress.ip_network("0.0.0.0/8"),
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("100.64.0.0/10"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("169.254.0.0/16"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("::1/128"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("fe80::/10"),
)


def _resolve_ip(hostname: str) -> list[str]:
    """Resolve a hostname to its IP addresses via socket.getaddrinfo."""
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror as exc:
        raise ValueError(f"SSRF guard: could not resolve host {hostname!r}: {exc}") from exc
    ips: list[str] = []
    for info in infos:
        addr = str(info[4][0])
        if "%" in addr:  # strip IPv6 scope id
            addr = addr.split("%", 1)[0]
        if addr not in ips:
            ips.append(addr)
    if not ips:
        raise ValueError(f"SSRF guard: no addresses resolved for {hostname!r}")
    return ips


def _ssrf_block_private(ip: str) -> None:
    """Reject loopback, link-local, private, reserved, and metadata addresses."""
    if ip == "169.254.169.254":
        raise ValueError("SSRF guard: cloud metadata endpoint 169.254.169.254 is blocked")
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError as exc:
        raise ValueError(f"SSRF guard: invalid resolved IP {ip!r}") from exc
    if addr.is_private or addr.is_loopback or addr.is_link_local or addr.is_reserved or addr.is_multicast:
        raise ValueError(f"SSRF guard: blocked address {ip} (private/loopback/link-local)")
    for net in _BLOCKED_NETWORKS:
        if addr in net:
            raise ValueError(f"SSRF guard: blocked network address {ip} ({net})")


def _ssrf_validate_host(hostname: str, allowed_hosts: frozenset[str] | None = None) -> None:
    """Validate a host: optional allowlist, then resolve + reject private IPs."""
    if not hostname:
        raise ValueError("SSRF guard: missing host")
    if allowed_hosts:
        if hostname.lower() in allowed_hosts:
            return
        raise ValueError(f"SSRF guard: host {hostname!r} not in allowlist {sorted(allowed_hosts)}")
    for ip in _resolve_ip(hostname):
        _ssrf_block_private(ip)


def _ssrf_validate_url(target_url: str, allowed_hosts: frozenset[str] | None = None) -> None:
    """Validate scheme + host of a URL before fetching (and on each redirect)."""
    parsed = urlparse(target_url)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(f"SSRF guard: only http/https allowed, got {parsed.scheme!r}")
    if not parsed.hostname:
        raise ValueError("SSRF guard: URL has no host")
    _ssrf_validate_host(parsed.hostname, allowed_hosts=allowed_hosts)


class _SSRFRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Re-validate the host on every redirect hop before following it."""

    def __init__(self, allowed_hosts: frozenset[str] | None = None) -> None:
        super().__init__()
        self._allowed_hosts = allowed_hosts

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _ssrf_validate_url(newurl, allowed_hosts=self._allowed_hosts)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


_opener = urllib.request.build_opener(_SSRFRedirectHandler())

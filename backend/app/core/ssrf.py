"""SSRF protection and browser/HTTP destination allowlist validation."""

from __future__ import annotations

import ipaddress
import socket
from urllib.parse import urlparse


class UnsafeURLError(ValueError):
    """Raised when a URL fails SSRF, scheme, or domain allowlist policy."""


_BLOCKED_HOSTNAMES: frozenset[str] = frozenset(
    {
        "localhost",
        "localhost.localdomain",
        "metadata.google.internal",
        "metadata",
        "169.254.169.254",
        "0.0.0.0",
    }
)


def _is_forbidden_ip(ip_obj: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_multicast
        or ip_obj.is_reserved
        or ip_obj.is_unspecified
    )


def extract_origin(url: str) -> str:
    """Return normalized scheme://hostname[:port] origin for a URL."""
    parsed = urlparse(url)
    host = (parsed.hostname or "").lower()
    if ":" in host and not host.startswith("["):
        host = f"[{host}]"
    port = f":{parsed.port}" if parsed.port else ""
    return f"{parsed.scheme.lower()}://{host}{port}"


def _is_loopback_hostname(host: str) -> bool:
    clean = host.strip("[]").strip().rstrip(".").lower()
    if clean == "localhost":
        return True
    try:
        return ipaddress.ip_address(clean).is_loopback
    except ValueError:
        return False


def validate_external_url(
    url: str,
    *,
    allow_http_public: bool = False,
    allow_test_origins: set[str] | None = None,
    allow_local_origins: set[str] | None = None,
    allowed_domains: set[str] | None = None,
    resolve_dns: bool = False,
) -> str:
    """Validate URL safety against SSRF, private IPs, metadata endpoints, and unsafe schemes.

    Loopback origins are strictly rejected unless exact origin is explicitly allowlisted
    for a configured local service (for example, local Ollama/OpenAI-compatible endpoint).
    """
    if not url or any(ch in url for ch in ("\r", "\n", "\x00")):
        raise UnsafeURLError("URL inválida ou com caracteres de controle.")

    parsed = urlparse(url.strip())
    scheme = parsed.scheme.lower()
    if scheme not in {"http", "https"}:
        raise UnsafeURLError(f"Esquema não permitido: {scheme!r}")
    if parsed.username or parsed.password:
        raise UnsafeURLError("Credenciais embutidas na URL são proibidas.")
    if not parsed.hostname:
        raise UnsafeURLError("URL sem hostname válido.")

    host = parsed.hostname.lower().rstrip(".")
    origin = extract_origin(url)
    local_origins = {o.lower() for o in (allow_test_origins or set())} | {
        o.lower() for o in (allow_local_origins or set())
    }
    is_explicit_local_origin = origin in local_origins

    if scheme == "http" and not (is_explicit_local_origin or allow_http_public):
        raise UnsafeURLError("HTTP simples bloqueado; use HTTPS para alvos externos.")

    if is_explicit_local_origin:
        if not _is_loopback_hostname(host):
            raise UnsafeURLError("Origem local permitida somente em loopback.")
        return url.strip()

    if host in _BLOCKED_HOSTNAMES or host.endswith(".localhost") or host.endswith(".local"):
        raise UnsafeURLError(f"Destino interno/loopback bloqueado (SSRF): {host}")

    # Check literal IP addresses
    try:
        ip_literal = ipaddress.ip_address(host)
    except ValueError:
        ip_literal = None

    if ip_literal is not None and _is_forbidden_ip(ip_literal):
        raise UnsafeURLError(f"IP privado/reservado bloqueado (SSRF): {host}")

    if allowed_domains is not None:
        normalized_domains = {d.lower().lstrip(".") for d in allowed_domains}
        if not any(host == d or host.endswith(f".{d}") for d in normalized_domains):
            raise UnsafeURLError(f"Domínio fora da allowlist permitida: {host}")

    if resolve_dns and ip_literal is None:
        try:
            infos = socket.getaddrinfo(host, parsed.port or (443 if scheme == "https" else 80))
        except socket.gaierror as exc:
            raise UnsafeURLError(f"Falha ao resolver DNS de {host}: {exc}") from exc
        for info in infos:
            addr_raw = str(info[4][0]).split("%")[0]
            resolved_ip = ipaddress.ip_address(addr_raw)
            if _is_forbidden_ip(resolved_ip):
                raise UnsafeURLError(
                    f"DNS de {host} resolveu para IP proibido ({resolved_ip})."
                )

    return url.strip()

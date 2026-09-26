"""SSRF-safe outbound HTTP for HTTP tool capabilities and the MCP client (P7-11).

Ported from ``AienginnerAgentOs@15235dc9827d5c8b08dd9fcb8e23c959882ed5b2:apps/api/app/tool_runtime.py``
(``_execute_http``, ``_blocked_address``, ``_resolve_endpoint``, ``validate_parameters``; ADR-0018 §4
PORT; the donor has no licence file, so the P7-13 licence check is open), with two changes:

* An address is refused unless ``ip.is_global`` (and not multicast), including the IPv4 address
  inside an IPv4-mapped, NAT64, 6to4 or Teredo IPv6 address. The donor's private/loopback/link-local/
  reserved list let ``100.64.0.0/10`` (carrier-grade NAT, often cloud-internal) through.
* Parameters are validated with ``jsonschema`` (Draft 2020-12) instead of a hand-written subset.

Order of checks, every one before a byte is sent: scheme http(s), no credentials in the URL, the
hostname on the operator's allowlist, every resolved address public (or the host/network explicitly
listed in ``outbound_private_hosts`` by the operator), then the connection goes to the vetted IP
itself -- the Host header and TLS SNI/certificate check keep the hostname -- so a second DNS answer
cannot swap in an internal address (DNS rebinding). No proxies from the environment, no redirects
(a 3xx is refused, never followed), and the body is streamed and cut at a byte cap.
"""
from __future__ import annotations

import ipaddress
import json
import socket
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit

from analystos.core.errors import InvalidInput, PolicyDenied, UpstreamUnavailable

IPAddress = ipaddress.IPv4Address | ipaddress.IPv6Address
Resolver = Callable[[str, int], list[IPAddress]]
DEFAULT_MAX_BYTES = 1_000_000
DEFAULT_TIMEOUT_SECONDS = 30.0
METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE")
_NAT64 = ipaddress.ip_network("64:ff9b::/96")


class OutboundRefused(PolicyDenied):
    """An outbound call the SSRF guard will not make."""


def _embedded_ipv4(address: IPAddress) -> list[ipaddress.IPv4Address]:
    if not isinstance(address, ipaddress.IPv6Address):
        return []
    out = [a for a in (address.ipv4_mapped, address.sixtofour) if a is not None]
    if address.teredo is not None:
        out += list(address.teredo)
    if address in _NAT64:
        out.append(ipaddress.IPv4Address(int(address) & 0xFFFFFFFF))
    return out


def address_is_public(address: IPAddress) -> bool:
    """``is_global`` and not multicast, for the address and every IPv4 address it carries."""
    return all(a.is_global and not a.is_multicast for a in [address, *_embedded_ipv4(address)])


def private_allowed(hostname: str, address: IPAddress, private_hosts: Iterable[str]) -> bool:
    for entry in private_hosts:
        entry = entry.strip().lower()
        if not entry:
            continue
        if entry == hostname:
            return True
        try:
            if address in ipaddress.ip_network(entry, strict=False):
                return True
        except ValueError:
            continue
    return False


def system_resolver(hostname: str, port: int) -> list[IPAddress]:
    try:
        infos = socket.getaddrinfo(hostname, port, type=socket.SOCK_STREAM)
    except (socket.gaierror, UnicodeError) as exc:
        raise OutboundRefused(f"host {hostname} could not be resolved") from exc
    out: list[IPAddress] = []
    for info in infos:
        try:
            address = ipaddress.ip_address(str(info[4][0]).split("%", 1)[0])
        except ValueError:
            continue
        if address not in out:
            out.append(address)
    if not out:
        raise OutboundRefused(f"host {hostname} could not be resolved")
    return out


@dataclass(frozen=True)
class PinnedTarget:
    """A vetted URL and the one address every request to it connects to."""

    url: str
    scheme: str
    hostname: str
    port: int
    address: IPAddress

    @property
    def netloc(self) -> str:
        host = f"[{self.address}]" if isinstance(self.address, ipaddress.IPv6Address) else str(self.address)
        return f"{host}:{self.port}"


def pin(url: str, *, allowlist: Iterable[str] | None, private_hosts: Iterable[str] = (),
        resolver: Resolver | None = None) -> PinnedTarget:
    """Validate ``url`` and resolve it once. ``allowlist`` None means the caller has its own allowlist
    (an owner-allowed MCP server); an empty allowlist refuses everything."""
    parts = urlsplit((url or "").strip())
    hostname = (parts.hostname or "").lower().rstrip(".")
    if parts.scheme not in ("http", "https") or not hostname:
        raise OutboundRefused("only http(s) URLs with a host can be called")
    if parts.username or parts.password:
        raise OutboundRefused("URLs must not embed credentials; use a secret reference")
    try:
        port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError as exc:
        raise OutboundRefused("the URL has an invalid port") from exc
    if allowlist is not None and hostname not in {h.strip().lower() for h in allowlist if h.strip()}:
        raise OutboundRefused(f"host {hostname} is not on the outbound allowlist")
    private_hosts = list(private_hosts)
    addresses = (resolver or system_resolver)(hostname, port)
    blocked = [str(a) for a in addresses if not address_is_public(a) and not private_allowed(hostname, a, private_hosts)]
    if blocked:
        raise OutboundRefused(f"host {hostname} resolves to a non-public address ({', '.join(blocked)}); an operator "
                              "must list it in outbound_private_hosts to allow it")
    return PinnedTarget(url=url.strip(), scheme=parts.scheme, hostname=hostname, port=port, address=addresses[0])


def _check_host(target: PinnedTarget, host: str, port: int | None) -> None:
    if host.lower().rstrip(".") != target.hostname or (port or (443 if target.scheme == "https" else 80)) != target.port:
        raise OutboundRefused(f"request to {host} does not match the vetted host {target.hostname}")


def _rewrite(request: Any, target: PinnedTarget) -> None:
    """Point the request at the vetted IP; the Host header and TLS SNI keep the hostname."""
    _check_host(target, request.url.host, request.url.port)
    request.url = request.url.copy_with(host=str(target.address))
    if target.scheme == "https":
        request.extensions = {**request.extensions, "sni_hostname": target.hostname}


def pinned_async_transport(target: PinnedTarget, httpx_module: Any, inner: Any | None = None) -> Any:
    """An async transport (httpx or httpx2) that only talks to ``target``'s vetted address."""

    class _Pinned(httpx_module.AsyncBaseTransport):
        def __init__(self) -> None:
            self.inner = inner or httpx_module.AsyncHTTPTransport(trust_env=False)

        async def handle_async_request(self, request: Any) -> Any:
            _rewrite(request, target)
            response = await self.inner.handle_async_request(request)
            if 300 <= response.status_code < 400:
                await response.aclose()
                raise OutboundRefused(f"{target.hostname} answered with a redirect ({response.status_code}); "
                                      "redirects are not followed")
            return response

        async def aclose(self) -> None:
            await self.inner.aclose()

    return _Pinned()


def validate_parameters(schema: dict[str, Any] | None, parameters: dict[str, Any]) -> None:
    """jsonschema (Draft 2020-12) validation; every error is listed, none is sent upstream."""
    from jsonschema import Draft202012Validator
    from jsonschema.exceptions import SchemaError

    schema = schema or {"type": "object"}
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError as exc:
        raise InvalidInput(f"the tool's parameter schema is invalid: {exc.message}") from None
    errors = sorted(Draft202012Validator(schema).iter_errors(parameters), key=lambda e: list(e.path))
    if errors:
        raise InvalidInput(f"parameters do not match the tool schema: {errors[0].message}",
                           details={"errors": [{"loc": ["parameters", *e.path], "msg": e.message} for e in errors[:20]]})


def call(url: str, *, method: str = "POST", parameters: dict[str, Any] | None = None,
         allowlist: Iterable[str] | None, private_hosts: Iterable[str] = (), timeout: float = DEFAULT_TIMEOUT_SECONDS,
         max_bytes: int = DEFAULT_MAX_BYTES, headers: dict[str, str] | None = None, resolver: Resolver | None = None,
         transport: Any | None = None) -> dict[str, Any]:
    """One guarded request. Parameters go in the JSON body (the query string for GET)."""
    import httpx

    method = method.upper()
    if method not in METHODS:
        raise InvalidInput(f"HTTP method {method} is not supported")
    target = pin(url, allowlist=allowlist, private_hosts=private_hosts, resolver=resolver)
    parts = urlsplit(target.url)
    pinned_url = parts._replace(netloc=target.netloc).geturl()
    default_port = target.port == (443 if target.scheme == "https" else 80)
    send_headers = {**(headers or {}), "Host": target.hostname if default_port else f"{target.hostname}:{target.port}"}
    extensions = {"sni_hostname": target.hostname} if target.scheme == "https" else {}
    body = {"params": parameters} if method == "GET" else {"json": parameters or {}}
    cap = max(1, int(max_bytes))
    try:
        with httpx.Client(timeout=timeout, follow_redirects=False, trust_env=False, transport=transport) as client, \
                client.stream(method, pinned_url, headers=send_headers, extensions=extensions, **body) as response:
            if 300 <= response.status_code < 400:
                raise OutboundRefused(f"{target.hostname} answered with a redirect ({response.status_code}); "
                                      "redirects are not followed")
            declared = response.headers.get("content-length", "")
            if declared.isdigit() and int(declared) > cap:
                raise OutboundRefused(f"response exceeds the {cap}-byte cap")
            chunks, received = [], 0
            for chunk in response.iter_bytes():
                received += len(chunk)
                if received > cap:
                    raise OutboundRefused(f"response exceeds the {cap}-byte cap")
                chunks.append(chunk)
            status, content_type = response.status_code, response.headers.get("content-type", "")
            encoding = response.encoding or "utf-8"
    except httpx.HTTPError as exc:
        raise UpstreamUnavailable(f"{target.hostname} unavailable: {type(exc).__name__}") from None
    raw = b"".join(chunks).decode(encoding, errors="replace")
    content: Any = raw[:100_000]
    if "json" in content_type:
        try:
            content = json.loads(raw or "null")
        except ValueError:
            raise UpstreamUnavailable(f"{target.hostname} returned invalid JSON") from None
    if status >= 400:
        raise UpstreamUnavailable(f"{target.hostname} answered {status}", details={"status_code": status})
    return {"status_code": status, "content": content, "bytes": received}


# ------------------------------------------------------------------------------ HTTP tool capabilities
def http_spec(manifest: Any) -> dict[str, Any]:
    """``spec.http`` of an ``entry: http:<name>`` capability: ``url`` and ``method`` (default POST)."""
    body = (manifest.spec or {}).get("http")
    if not isinstance(body, dict) or not isinstance(body.get("url"), str):
        raise InvalidInput(f"{manifest.id} has an http entry but no spec.http.url")
    method = str(body.get("method") or "POST").upper()
    if method not in METHODS:
        raise InvalidInput(f"{manifest.id}: spec.http.method {method} is not supported")
    parts = urlsplit(body["url"])
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.username or parts.password:
        raise InvalidInput(f"{manifest.id}: spec.http.url must be an http(s) URL without credentials")
    return {"url": body["url"], "method": method}


def invoke_manifest(manifest: Any, arguments: dict[str, Any], *, resolver: Resolver | None = None,
                    transport: Any | None = None) -> dict[str, Any]:
    """Run an HTTP tool capability: schema, then the guarded call with the platform's allowlist."""
    from analystos.core.config import get_settings

    settings = get_settings()
    spec = http_spec(manifest)
    validate_parameters(manifest.input_schema, arguments)
    return call(spec["url"], method=spec["method"], parameters=arguments,
                allowlist=split_hosts(settings.http_tool_allowlist), private_hosts=split_hosts(settings.outbound_private_hosts),
                timeout=float(settings.http_tool_timeout_seconds), max_bytes=int(settings.http_tool_max_bytes),
                resolver=resolver, transport=transport)


def split_hosts(value: str | Iterable[str] | None) -> list[str]:
    if value is None:
        return []
    items = value.split(",") if isinstance(value, str) else list(value)
    return [i.strip().lower() for i in items if i and i.strip()]

"""P7-11: SSRF fixtures for outbound HTTP (``tools/http.py``): HTTP tool capabilities and the MCP client.

Covered: carrier-grade NAT (100.64/10, which the donor's check let through), loopback, link-local
(cloud metadata), private ranges, IPv4 hidden in IPv6 (mapped, NAT64, 6to4), multicast; allowlist
and credentials in the URL; DNS rebinding (a second answer cannot move a pinned connection, and a
fresh call re-checks); redirects refused and never followed; the byte cap; jsonschema parameters;
and the MCP client refusing a non-public server before any request."""
from __future__ import annotations

import ipaddress
import json
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from types import SimpleNamespace

import httpx
import pytest

from analystos.contracts.capability import CapabilityManifest
from analystos.core.errors import InvalidInput, UpstreamUnavailable
from analystos.tools import http as outbound
from analystos.tools.http import OutboundRefused

PUBLIC = ipaddress.ip_address("93.184.216.34")


def resolver(*answers: str):  # noqa: ANN201
    """A DNS double: each call returns the next answer (the last one repeats)."""
    seq = [[ipaddress.ip_address(a) for a in ans.split(",")] for ans in answers]
    calls = []

    def resolve(host: str, port: int) -> list:
        calls.append(host)
        return seq[min(len(calls), len(seq)) - 1]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


@pytest.mark.parametrize("address", [
    "100.64.0.1", "100.127.255.254",  # CGNAT: passes the donor's private/loopback/link-local/reserved test
    "127.0.0.1", "::1", "169.254.169.254", "fe80::1", "10.0.0.5", "172.16.3.4", "192.168.1.1", "0.0.0.0", "::",
    "224.0.0.1", "ff02::1", "fc00::1", "::ffff:127.0.0.1", "::ffff:100.64.0.1", "64:ff9b::a9fe:a9fe",
    "2002:7f00:1::", "192.0.2.1", "198.18.0.1",
])
def test_non_public_addresses_are_refused(address: str) -> None:
    assert not outbound.address_is_public(ipaddress.ip_address(address))
    with pytest.raises(OutboundRefused, match="non-public"):
        outbound.pin("https://tool.example.com/x", allowlist=["tool.example.com"], resolver=resolver(address))


@pytest.mark.parametrize("address", ["93.184.216.34", "8.8.8.8", "2606:4700:4700::1111"])
def test_public_addresses_pass(address: str) -> None:
    assert outbound.address_is_public(ipaddress.ip_address(address))
    t = outbound.pin("https://tool.example.com/x", allowlist=["tool.example.com"], resolver=resolver(address))
    assert str(t.address) == address and t.port == 443 and t.hostname == "tool.example.com"


def test_one_non_public_answer_among_public_ones_refuses() -> None:
    with pytest.raises(OutboundRefused, match="169.254.169.254"):
        outbound.pin("http://tool.example.com", allowlist=["tool.example.com"], resolver=resolver("8.8.8.8,169.254.169.254"))


@pytest.mark.parametrize("url,match", [
    ("https://evil.example.net/x", "allowlist"),
    ("https://user:pw@tool.example.com/x", "credentials"),
    ("ftp://tool.example.com/x", "http"),
    ("file:///etc/passwd", "http"),
    ("https://tool.example.com:99999/x", "port"),
])
def test_url_shape_and_allowlist(url: str, match: str) -> None:
    with pytest.raises(OutboundRefused, match=match):
        outbound.pin(url, allowlist=["tool.example.com"], resolver=resolver("8.8.8.8"))


def test_empty_allowlist_refuses_everything() -> None:
    with pytest.raises(OutboundRefused, match="allowlist"):
        outbound.pin("https://tool.example.com", allowlist=[], resolver=resolver("8.8.8.8"))


def test_operator_listed_private_host_or_network_is_allowed() -> None:
    assert outbound.pin("http://mcp.internal:8080", allowlist=None, private_hosts=["mcp.internal"],
                        resolver=resolver("10.1.2.3")).port == 8080
    assert outbound.pin("http://mcp.internal", allowlist=None, private_hosts=["10.0.0.0/8"], resolver=resolver("10.1.2.3"))
    with pytest.raises(OutboundRefused):
        outbound.pin("http://mcp.internal", allowlist=None, private_hosts=["10.0.0.0/8"], resolver=resolver("100.64.0.9"))


# ------------------------------------------------------------------ the call itself
class Recorder:
    def __init__(self, respond) -> None:  # noqa: ANN001
        self.requests: list[httpx.Request] = []
        self.respond = respond

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.respond(request)


def _call(transport, dns, **kw):  # noqa: ANN001, ANN003, ANN202
    return outbound.call("https://tool.example.com/api/run", method="POST", parameters={"a": 1},
                         allowlist=["tool.example.com"], resolver=dns, transport=httpx.MockTransport(transport), **kw)


def test_connection_is_pinned_to_the_vetted_address_with_host_and_sni() -> None:
    rec = Recorder(lambda r: httpx.Response(200, json={"ok": True}))
    out = _call(rec, resolver("93.184.216.34"))
    assert out == {"status_code": 200, "content": {"ok": True}, "bytes": len(b'{"ok":true}')}
    (req,) = rec.requests
    assert req.url.host == "93.184.216.34" and req.headers["host"] == "tool.example.com"
    assert req.extensions["sni_hostname"] == "tool.example.com" and json.loads(req.content) == {"a": 1}


def test_dns_rebinding_cannot_move_a_call() -> None:
    dns = resolver("93.184.216.34", "127.0.0.1")
    rec = Recorder(lambda r: httpx.Response(200, text="first"))
    assert _call(rec, dns)["content"] == "first"
    assert rec.requests[0].url.host == "93.184.216.34"  # connected to what was vetted, not a later answer
    with pytest.raises(OutboundRefused, match="non-public"):  # the next call re-resolves and re-checks
        _call(rec, dns)
    assert len(rec.requests) == 1


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
def test_redirects_are_refused_not_followed(status: int) -> None:
    rec = Recorder(lambda r: httpx.Response(status, headers={"location": "http://169.254.169.254/latest/meta-data"}))
    with pytest.raises(OutboundRefused, match="redirect"):
        _call(rec, resolver("93.184.216.34"))
    assert len(rec.requests) == 1


def test_byte_cap_streamed_and_declared() -> None:
    big = Recorder(lambda r: httpx.Response(200, content=b"x" * 5000))
    with pytest.raises(OutboundRefused, match="byte cap"):
        _call(big, resolver("93.184.216.34"), max_bytes=1000)

    def streamed(r: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=httpx.ByteStream(b"y" * 5000))  # no content-length to trust

    with pytest.raises(OutboundRefused, match="byte cap"):
        _call(Recorder(streamed), resolver("93.184.216.34"), max_bytes=1000)


def test_upstream_errors_are_reported_not_raised_raw() -> None:
    with pytest.raises(UpstreamUnavailable, match="500"):
        _call(Recorder(lambda r: httpx.Response(500, text="boom")), resolver("93.184.216.34"))


def test_a_real_socket_call_reaches_only_the_pinned_address() -> None:
    """No mocks: a local server; the hostname never resolves through DNS, the double pins it."""
    seen: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            seen.append(self.headers["Host"])
            body = json.dumps({"got": json.loads(self.rfile.read(int(self.headers["Content-Length"])))}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *a) -> None:  # noqa: ANN002
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        port = server.server_address[1]
        out = outbound.call(f"http://tool.test:{port}/run", parameters={"q": "x"}, allowlist=["tool.test"],
                            private_hosts=["127.0.0.1/32"], resolver=resolver("127.0.0.1"))
        assert out["content"] == {"got": {"q": "x"}} and seen == [f"tool.test:{port}"]
        with pytest.raises(OutboundRefused):  # the same server is refused without the operator's listing
            outbound.call(f"http://tool.test:{port}/run", allowlist=["tool.test"], resolver=resolver("127.0.0.1"))
    finally:
        server.shutdown()


# ------------------------------------------------------------------ HTTP tool capability
def _manifest(**over) -> CapabilityManifest:  # noqa: ANN003
    body = {"kind": "Tool", "id": "tool.ticket_lookup", "summary": "Look up a ticket", "entry": "http:ticket_lookup",
            "side_effect": "read_source",
            "input_schema": {"type": "object", "properties": {"ticket": {"type": "string", "pattern": "^T-[0-9]+$"}},
                             "required": ["ticket"], "additionalProperties": False},
            "spec": {"http": {"url": "https://tickets.example.com/lookup", "method": "POST"}}}
    return CapabilityManifest.model_validate({**body, **over})


@pytest.mark.parametrize("args", [{}, {"ticket": "DROP"}, {"ticket": "T-1", "extra": 1}, {"ticket": 5}])
def test_schema_invalid_parameters_are_refused_before_any_call(args: dict, monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setattr(outbound, "call", lambda *a, **k: pytest.fail("no call may be made"))
    with pytest.raises(InvalidInput, match="parameters do not match"):
        outbound.invoke_manifest(_manifest(), args)


def test_http_tool_uses_the_platform_allowlist(monkeypatch) -> None:  # noqa: ANN001
    settings = SimpleNamespace(http_tool_allowlist="", outbound_private_hosts="", http_tool_timeout_seconds=5,
                               http_tool_max_bytes=1000)
    monkeypatch.setattr("analystos.core.config.get_settings", lambda: settings)
    with pytest.raises(OutboundRefused, match="allowlist"):
        outbound.invoke_manifest(_manifest(), {"ticket": "T-1"}, resolver=resolver("93.184.216.34"))
    settings.http_tool_allowlist = "tickets.example.com"
    rec = Recorder(lambda r: httpx.Response(200, json={"status": "open"}))
    out = outbound.invoke_manifest(_manifest(), {"ticket": "T-1"}, resolver=resolver("93.184.216.34"),
                                   transport=httpx.MockTransport(rec))
    assert out["content"] == {"status": "open"} and rec.requests[0].url.path == "/lookup"


def test_http_tool_manifest_validation() -> None:
    from analystos.capabilities.validation import validate_kinds

    ok = _manifest()
    bad = _manifest(id="tool.bad_http", spec={"http": {"url": "https://u:p@x.example.com/", "method": "POST"}})
    none = _manifest(id="tool.none_http", spec={})
    problems = validate_kinds({m.id: m for m in (ok, bad, none)}, references=False)
    assert not [p for p in problems if "ticket_lookup" in p]
    assert any("tool.bad_http" in p and "credentials" in p for p in problems)
    assert any("tool.none_http" in p and "spec.http.url" in p for p in problems)


def test_http_tool_is_invocable_through_the_capability_runtime() -> None:
    from analystos.capabilities import invoke

    assert invoke.executable(_manifest()) and invoke.is_http_tool(_manifest())
    assert invoke.schema_errors(_manifest(), {"ticket": "nope"})


# ------------------------------------------------------------------ MCP client
def test_pinned_async_transport_rewrites_and_refuses() -> None:
    import asyncio

    import httpx2

    target = outbound.pin("https://mcp.example.com/mcp", allowlist=None, resolver=resolver("93.184.216.34"))
    seen: list = []

    def handler(request):  # noqa: ANN001, ANN202
        seen.append(request)
        if request.url.path == "/redirect":
            return httpx2.Response(302, headers={"location": "http://127.0.0.1/"})
        return httpx2.Response(200, json={"ok": 1})

    async def go() -> None:
        transport = outbound.pinned_async_transport(target, httpx2, inner=httpx2.MockTransport(handler))
        async with httpx2.AsyncClient(transport=transport, follow_redirects=False, trust_env=False) as c:
            r = await c.post("https://mcp.example.com/mcp", json={})
            assert r.status_code == 200
            with pytest.raises(OutboundRefused, match="redirect"):
                await c.get("https://mcp.example.com/redirect")
            with pytest.raises(OutboundRefused, match="does not match"):
                await c.get("https://other.example.com/mcp")

    asyncio.run(go())
    assert seen[0].url.host == "93.184.216.34" and seen[0].headers["host"] == "mcp.example.com"
    assert seen[0].extensions["sni_hostname"] == "mcp.example.com" and len(seen) == 2


@pytest.mark.parametrize("address", ["100.64.0.1", "169.254.169.254", "10.0.0.1", "::1"])
def test_mcp_client_refuses_a_non_public_server_before_any_request(address: str, monkeypatch) -> None:  # noqa: ANN001
    from analystos.mcp import client as mc

    monkeypatch.setattr(outbound, "system_resolver", resolver(address))
    monkeypatch.setattr(mc, "_private_hosts", lambda: [])
    server = SimpleNamespace(name="bi", url="https://mcp.example.com/mcp", secret_ref=None, config={})
    with pytest.raises(OutboundRefused, match="non-public"):
        mc._call_remote(server, lambda client: pytest.fail("no MCP request may be made"))


def test_mcp_registration_refuses_a_non_public_address_literal(monkeypatch) -> None:  # noqa: ANN001
    from analystos.mcp import client as mc

    monkeypatch.setattr(mc, "_private_hosts", lambda: [])
    for url in ("http://169.254.169.254/mcp", "http://100.64.1.1:8080/mcp", "http://[::1]/mcp"):
        with pytest.raises(InvalidInput, match="not public"):
            mc._validate_url(url)
    assert mc._validate_url("https://mcp.example.com/mcp") == "https://mcp.example.com/mcp"
    monkeypatch.setattr(mc, "_private_hosts", lambda: ["127.0.0.1"])
    assert mc._validate_url("http://127.0.0.1:9000/mcp")

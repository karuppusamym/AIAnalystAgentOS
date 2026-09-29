"""P4-X05: the platform MCP host allowlist and the SSRF bypasses it must survive.

A workspace owner decides whether to use an MCP server; only the platform (env + admin setting) decides
which hosts may be registered or reached at all. Covered: allowlist grammar (wildcards, ports, networks),
URL tricks (userinfo, `#@`, backslashes, whitespace), numeric host encodings (decimal, octal, hex, short
forms), IPv6 forms (mapped, zone ids), wildcard hosts resolving private, metadata never, air-gapped
egress, DNS rebinding between registration and connection, redirects, and the admin setting contract."""
from __future__ import annotations

import asyncio
import ipaddress
from types import SimpleNamespace

import httpx
import pytest
from pydantic import ValidationError

from analystos.contracts.platform import OutboundSettings, PlatformSettings
from analystos.core.errors import InvalidInput
from analystos.mcp import client as mc
from analystos.tools import http as outbound
from analystos.tools.http import OutboundRefused


def resolver(*answers: str):  # noqa: ANN201
    seq = [[ipaddress.ip_address(a) for a in ans.split(",")] for ans in answers]
    calls: list[str] = []

    def resolve(host: str, port: int) -> list:
        calls.append(host)
        return seq[min(len(calls), len(seq)) - 1]

    resolve.calls = calls  # type: ignore[attr-defined]
    return resolve


@pytest.fixture()
def platform(monkeypatch):  # noqa: ANN001, ANN201
    """Set the platform allowlist and the operator lists without a database."""
    state = SimpleNamespace(allowlist=[], operator=[], air_gapped=False)
    monkeypatch.setattr(mc, "_platform_allowlist", lambda: list(state.allowlist))
    monkeypatch.setattr(mc, "_operator_private_hosts", lambda: list(state.operator))
    monkeypatch.setattr(mc, "_private_hosts", lambda: ["127.0.0.0/8", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"])
    monkeypatch.setattr(mc, "_air_gapped", lambda: state.air_gapped)
    return state


# ------------------------------------------------------------------ allowlist grammar
@pytest.mark.parametrize("entry,host,port,expected", [
    ("mcp.example.com", "mcp.example.com", 443, "explicit"),
    ("mcp.example.com", "evil-mcp.example.com", 443, None),
    ("mcp.example.com", "mcp.example.com.evil.net", 443, None),
    ("MCP.Example.COM.", "mcp.example.com", 443, "explicit"),
    ("mcp.example.com:8443", "mcp.example.com", 8443, "explicit"),
    ("mcp.example.com:8443", "mcp.example.com", 443, None),
    ("*.example.com", "a.b.example.com", 443, "wildcard"),
    ("*.example.com", "example.com", 443, None),
    ("*.example.com", "badexample.com", 443, None),
    ("*", "anything.test", 80, "wildcard"),
    ("10.20.0.0/16", "10.20.3.4", 80, "explicit"),
    ("10.20.0.0/16", "mcp.internal", 80, None),  # networks match address-literal URLs only
    ("[fd12::1]:9000", "fd12::1", 9000, "explicit"),
    ("fd12::/16", "fd12::7", 443, "explicit"),
])
def test_entry_matching(entry: str, host: str, port: int, expected: str | None) -> None:
    assert outbound.match_host(host, port, [entry]) == expected


def test_explicit_beats_wildcard_and_malformed_entries_never_match() -> None:
    assert outbound.match_host("mcp.example.com", 443, ["*", "mcp.example.com"]) == "explicit"
    assert outbound.match_host("mcp.example.com", 443, ["*.", "mcp.example.com:abc", "*.com"]) is None


@pytest.mark.parametrize("entry", ["*.", "*.com", "host:abc", "host:70000", "[::1]x", "127.1", "0x7f000001", "bad host",
                                   "*.exa mple.com", "bücher.de"])
def test_malformed_entries_are_rejected(entry: str) -> None:
    with pytest.raises(InvalidInput):
        outbound.parse_entry(entry)


def test_admin_setting_validates_and_normalises() -> None:
    s = OutboundSettings(mcp_host_allowlist=["MCP.example.com", "mcp.example.com", "*.corp.example", "10.0.0.0/8"])
    assert s.mcp_host_allowlist == ["mcp.example.com", "*.corp.example", "10.0.0.0/8"]
    with pytest.raises(ValidationError):
        OutboundSettings(mcp_host_allowlist=["*.com"])
    assert PlatformSettings().outbound.mcp_host_allowlist == []  # fail closed: no MCP host until an admin lists one


# ------------------------------------------------------------------ URL and host encodings
@pytest.mark.parametrize("url", [
    "http://2130706433/mcp",            # decimal 127.0.0.1
    "http://0x7f000001/mcp",            # hex
    "http://0x7f.0.0.1/mcp",            # hex octet
    "http://0177.0.0.1/mcp",            # octal
    "http://127.1/mcp",                 # short form
    "http://017700000001/mcp",          # octal integer
    "http://169.254.169.254.1/mcp",     # five numeric labels
    "http://[fe80::1%25eth0]/mcp",      # IPv6 zone id
    "http://mcp.example.com\\@evil.test/mcp",  # backslash parser differential
    "http://mcp.example.com /mcp",      # whitespace
    "http://mcp.example.com\t.evil.test/mcp",
    "http://exаmple.com/mcp",      # non-ASCII (Cyrillic a) homograph
])
def test_ambiguous_urls_are_refused_before_any_resolution(url: str) -> None:
    dns = resolver("93.184.216.34")
    with pytest.raises(OutboundRefused):
        outbound.pin(url, allowlist=["*"], resolver=dns)
    assert dns.calls == []


@pytest.mark.parametrize("url", [
    "http://mcp.example.com:80@evil.test/mcp",   # userinfo that looks like a port
    "http://mcp.example.com@evil.test/mcp",
    "http://@evil.test/mcp",
])
def test_userinfo_tricks_are_refused(url: str) -> None:
    with pytest.raises(OutboundRefused, match="credentials"):
        outbound.pin(url, allowlist=["mcp.example.com", "evil.test"], resolver=resolver("93.184.216.34"))


def test_fragment_at_sign_does_not_change_the_host(platform) -> None:  # noqa: ANN001
    platform.allowlist = ["mcp.example.com"]
    with pytest.raises(OutboundRefused, match="not on the platform MCP host allowlist"):
        mc._pin("http://evil.test#@mcp.example.com/mcp", resolver("93.184.216.34"))


@pytest.mark.parametrize("address", ["::ffff:169.254.169.254", "::ffff:a9fe:a9fe", "64:ff9b::a9fe:a9fe",
                                     "fd00:ec2::254", "100.100.100.200", "168.63.129.16", "169.254.170.2"])
def test_metadata_is_never_reachable_even_when_listed(address: str, platform) -> None:  # noqa: ANN001
    platform.allowlist = ["mcp.internal"]
    platform.operator = ["mcp.internal", "0.0.0.0/0", "::/0"]
    with pytest.raises(OutboundRefused, match="never allowed"):
        mc._pin("http://mcp.internal/mcp", resolver(address))


# ------------------------------------------------------------------ the platform allowlist in the MCP client
def test_empty_platform_allowlist_refuses_every_server(platform) -> None:  # noqa: ANN001
    dns = resolver("93.184.216.34")
    with pytest.raises(OutboundRefused, match="platform MCP host allowlist"):
        mc._validate_url("https://mcp.example.com/mcp", dns)
    assert dns.calls == []  # refused before DNS


def test_registration_resolves_and_refuses_a_private_answer_for_a_wildcard(platform) -> None:  # noqa: ANN001
    platform.allowlist = ["*.example.com"]
    assert mc._validate_url("https://mcp.example.com/mcp", resolver("93.184.216.34"))
    for private in ("10.0.0.5", "127.0.0.1", "192.168.1.9", "::1"):
        with pytest.raises(OutboundRefused, match="non-public"):
            mc._validate_url("https://mcp.example.com/mcp", resolver(private))


def test_exact_entry_reaches_in_cluster_ranges_but_not_cgnat(platform) -> None:  # noqa: ANN001
    platform.allowlist = ["mcp.internal"]
    assert mc._pin("http://mcp.internal:8080/mcp", resolver("10.4.5.6")).port == 8080
    with pytest.raises(OutboundRefused, match="non-public"):
        mc._pin("http://mcp.internal/mcp", resolver("100.64.0.9"))
    with pytest.raises(OutboundRefused, match="allowlist"):
        mc._pin("http://other.internal/mcp", resolver("10.4.5.6"))


def test_port_restricted_entry(platform) -> None:  # noqa: ANN001
    platform.allowlist = ["mcp.example.com:8443"]
    assert mc._pin("https://mcp.example.com:8443/mcp", resolver("93.184.216.34"))
    with pytest.raises(OutboundRefused, match="allowlist"):
        mc._pin("https://mcp.example.com/mcp", resolver("93.184.216.34"))
    with pytest.raises(OutboundRefused, match="allowlist"):
        mc._pin("http://mcp.example.com:6379/mcp", resolver("93.184.216.34"))


def test_address_literal_needs_a_network_entry(platform) -> None:  # noqa: ANN001
    platform.allowlist = ["mcp.example.com"]
    with pytest.raises(OutboundRefused, match="allowlist"):
        mc._validate_url("http://93.184.216.34/mcp")
    platform.allowlist = ["10.20.0.0/16"]
    assert mc._validate_url("http://10.20.1.2:9000/mcp")
    with pytest.raises(OutboundRefused, match="allowlist"):
        mc._validate_url("http://10.21.1.2:9000/mcp")


def test_ipv6_mapped_private_literal_is_refused_under_a_wildcard(platform) -> None:  # noqa: ANN001
    platform.allowlist = ["*"]
    for url in ("http://[::ffff:127.0.0.1]/mcp", "http://[::ffff:7f00:1]/mcp", "http://[::1]/mcp", "http://[fc00::1]/mcp"):
        with pytest.raises(OutboundRefused, match="non-public"):
            mc._validate_url(url)


def test_air_gapped_refuses_public_addresses(platform) -> None:  # noqa: ANN001
    platform.allowlist = ["*.example.com", "mcp.internal"]
    platform.air_gapped = True
    with pytest.raises(OutboundRefused, match="air-gapped"):
        mc._pin("https://mcp.example.com/mcp", resolver("93.184.216.34"))
    assert mc._pin("http://mcp.internal/mcp", resolver("10.1.1.1"))


def test_http_tools_honour_air_gapped(monkeypatch) -> None:  # noqa: ANN001
    with pytest.raises(OutboundRefused, match="air-gapped"):
        outbound.pin("https://tool.example.com/x", allowlist=["tool.example.com"], resolver=resolver("8.8.8.8"),
                     internal_only=True)


def test_removal_from_the_platform_allowlist_stops_the_next_connection(platform) -> None:  # noqa: ANN001
    """The check runs on every connection, not just at registration."""
    platform.allowlist = ["mcp.example.com"]
    assert mc._validate_url("https://mcp.example.com/mcp", resolver("93.184.216.34"))
    platform.allowlist = []
    server = SimpleNamespace(name="bi", url="https://mcp.example.com/mcp", secret_ref=None, config={})
    with pytest.raises(OutboundRefused, match="allowlist"):
        mc._call_remote(server, lambda client: pytest.fail("no MCP request may be made"))


def test_dns_rebinding_after_registration_is_caught_at_connect(platform, monkeypatch) -> None:  # noqa: ANN001
    platform.allowlist = ["*.example.com"]
    dns = resolver("93.184.216.34", "127.0.0.1")
    assert mc._validate_url("https://mcp.example.com/mcp", dns)  # registration saw a public answer
    monkeypatch.setattr(outbound, "system_resolver", dns)
    server = SimpleNamespace(name="bi", url="https://mcp.example.com/mcp", secret_ref=None, config={})
    with pytest.raises(OutboundRefused, match="non-public"):
        mc._call_remote(server, lambda client: pytest.fail("no MCP request may be made"))


def test_pinned_connection_ignores_later_answers_and_refuses_redirects(platform) -> None:  # noqa: ANN001
    platform.allowlist = ["mcp.example.com"]
    dns = resolver("93.184.216.34", "169.254.169.254")
    target = mc._pin("https://mcp.example.com/mcp", dns)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if request.url.path == "/r":
            return httpx.Response(307, headers={"location": "http://169.254.169.254/latest/meta-data"})
        return httpx.Response(200, json={})

    async def go() -> None:
        t = outbound.pinned_async_transport(target, httpx, inner=httpx.MockTransport(handler))
        async with httpx.AsyncClient(transport=t, follow_redirects=False, trust_env=False) as c:
            for _ in range(3):
                assert (await c.post("https://mcp.example.com/mcp", json={})).status_code == 200
            with pytest.raises(OutboundRefused, match="redirect"):
                await c.get("https://mcp.example.com/r")
            with pytest.raises(OutboundRefused, match="does not match"):
                await c.get("https://mcp.example.com:8443/mcp")

    asyncio.run(go())
    assert {r.url.host for r in seen} == {"93.184.216.34"} and dns.calls == ["mcp.example.com"]

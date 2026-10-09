#  Pyrogram - Telegram MTProto API Client Library for Python
#  Copyright (C) 2017-present Dan <https://github.com/delivrance>
#
#  This file is part of Pyrogram.
#
#  Pyrogram is free software: you can redistribute it and/or modify
#  it under the terms of the GNU Lesser General Public License as published
#  by the Free Software Foundation, either version 3 of the License, or
#  (at your option) any later version.
#
#  Pyrogram is distributed in the hope that it will be useful,
#  but WITHOUT ANY WARRANTY; without even the implied warranty of
#  MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#  GNU Lesser General Public License for more details.
#
#  You should have received a copy of the GNU Lesser General Public License
#  along with Pyrogram.  If not, see <http://www.gnu.org/licenses/>.

from __future__ import annotations as _annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import socket
import time
from http import HTTPStatus
from types import SimpleNamespace
from typing import Final, NamedTuple

import pytest
from python_socks import ProxyType

import pyrogram
from pyrogram import Client, raw
from pyrogram.connection.connection import (
    Connection,
    protocol_dc_id,
    transport_class_for,
    transport_error,
)
from pyrogram.connection.proxy import (
    HTTPS_PORT,
    HTTPProxy,
    MTProxy,
    Proxy,
    ProxyAddress,
    SOCKS4Proxy,
    SOCKS5Proxy,
    WebProxy,
    canonicalize_web_hostname,
    client_proxy_address,
    normalize_proxy,
)
from pyrogram.connection.transport import TCP, TCPAbridged, TCPFull, TCPPaddedIntermediate
from pyrogram.connection.transport.tcp import (
    TCPAbridged,
    TCPPaddedIntermediate,
    web_proxy_carrier,
    TCP,
)
from pyrogram.connection.transport.tcp.faketls_records import (
    APPLICATION_DATA_PREFIX,
    CHANGE_CIPHER_SPEC,
    RECORD_HEADER_SIZE,
    RECORD_LENGTH_SIZE,
    FakeTlsRecords,
    _MAX_RECORD_PAYLOAD,
)
from pyrogram.connection.transport.tcp.tcp import (
    ABRIDGED_OBFUSCATE_TAG,
    PADDED_INTERMEDIATE_OBFUSCATE_TAG,
    TCP,
    _OBFUSCATED2_RESERVED_PREFIXES,
    generate_obfuscated2_nonce,
)
from pyrogram.connection.transport.tcp.tcp_abridged import TCPAbridged
from pyrogram.connection.transport.tcp.tcp_padded_intermediate import TCPPaddedIntermediate
from pyrogram.connection.transport.tcp.web_proxy_carrier import (
    FRAME_HEADER_SIZE,
    FRAME_MAX_PAYLOAD,
    Frame,
    FrameParseError,
    FrameType,
    WebCarrierError,
    WebProxyCarrier,
    _DOWNLINK_GRANT_THRESHOLD,
    _HttpConnection,
    _INITIAL_STREAM_WINDOW,
    _STREAM_ID,
    _UPLINK_FRAME_MAX,
    _WINDOW_PAYLOAD_SIZE,
    derive_bridge_capability,
    parse_frame_message,
    parse_frames,
    serialize_frame,
)
from pyrogram.crypto import aes, faketls
from pyrogram.enums import ProxyScheme
from pyrogram.errors import PhoneMigrate, UserMigrate
from pyrogram.methods.auth.connect import Connect
from pyrogram.methods.auth.send_code import SendCode
from pyrogram.methods.auth.sign_in_bot import SignInBot
from pyrogram.session.session import Session


# A made-up value. Every test using it only parses or re-encodes it, so nothing
#  here needs a secret that belongs to a real deployment.
PLAIN_SECRET_HEX: Final[str] = "0123456789abcdef0123456789abcdef"
DD_SECRET_HEX: Final[str] = "dd" + PLAIN_SECRET_HEX


# The domain an ee secret carries. Also made up: the tests round-trip it through
#  the secret encoding and into the greeting, and never resolve it.
SNI_DOMAIN: Final[str] = "www.example.com"


# Normative capability vectors. The relay publishes the same two in its own
#  protocol spec, so client and relay agree on the derivation byte for byte.
#  https://github.com/telegramdesktop/tproxy-server/blob/52a5feb7fac38f68da5afef9cedd9b3bfc8473ca/PROTOCOL.md#L28-L31
BRIDGE_CAPABILITY_VECTORS: Final[tuple[tuple[str, str, str], ...]] = (
    (
        "proxy.example.com",
        "000102030405060708090a0b0c0d0e0f",
        "MHLEY5PmW1GWqJkSrlmJpvJUiLhBH_QKy6yKg8a0JPk",
    ),
    (
        "proxy.example.com",
        "dd000102030405060708090a0b0c0d0e0f",
        "IpJrt3e7sKtzPyoXy6w-Zj6GGEvsvclN66JzQEfPYLA",
    ),
)


# TDLib's `MAX_DOMAIN_LENGTH`, written out rather than imported: what the two
#  tests below pin is the number TDLib publishes, and importing ours would only
#  make them agree with whatever it happens to say.
#  https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/td/mtproto/ProxySecret.h#L18
_MAX_SNI_DOMAIN_SIZE: Final[int] = 182


def test_hostname_canonicalization_matches_normative_vector_host() -> None:
    # §2.4/§10: different normalizations of the same host derive different
    #  capabilities, so a mixed-case hostname must still hit the vector for its
    #  lowercase form.
    assert canonicalize_web_hostname("Proxy.Example.com") == "proxy.example.com"


@pytest.mark.parametrize("hostname", ["203.0.113.5", "relay", "", "  "])
def test_invalid_web_hostname_forms_are_rejected(hostname: str) -> None:
    with pytest.raises(ValueError):
        canonicalize_web_hostname(hostname)


def test_normalize_proxy_none_passes_through() -> None:
    assert normalize_proxy(None) is None


@pytest.mark.parametrize("empty", [{}, ""])
def test_normalize_proxy_empty_means_no_proxy(empty) -> None:
    assert normalize_proxy(empty) is None


def test_client_normalizes_a_proxy_assigned_after_init() -> None:
    client = Client("proxy_property", api_id=1, api_hash="a", in_memory=True)
    client.proxy = {"scheme": "socks5", "hostname": "127.0.0.1", "port": 1080}

    assert client.proxy == SOCKS5Proxy(hostname="127.0.0.1", port=1080)

    client.proxy = {}

    assert client.proxy is None


def test_normalize_proxy_is_idempotent_on_a_dataclass() -> None:
    web_proxy = WebProxy(hostname="relay.example.com", secret=bytes.fromhex(PLAIN_SECRET_HEX))

    assert normalize_proxy(web_proxy) is web_proxy


def test_normalize_proxy_web_dict_form() -> None:
    web_proxy = normalize_proxy(
        {"scheme": "web", "hostname": "RELAY.Example.COM", "secret": PLAIN_SECRET_HEX}
    )

    assert isinstance(web_proxy, WebProxy)
    assert web_proxy.scheme is ProxyScheme.WEB
    assert web_proxy.hostname == "relay.example.com"
    assert web_proxy.secret == bytes.fromhex(PLAIN_SECRET_HEX)


def test_normalize_proxy_web_dict_form_keeps_dd_marker() -> None:
    web_proxy = normalize_proxy(
        {"scheme": "web", "hostname": "relay.example.com", "secret": DD_SECRET_HEX}
    )

    assert web_proxy.secret == bytes.fromhex(DD_SECRET_HEX)


def test_normalize_proxy_scheme_is_case_insensitive() -> None:
    web_proxy = normalize_proxy(
        {"scheme": "WEB", "hostname": "relay.example.com", "secret": PLAIN_SECRET_HEX}
    )

    assert isinstance(web_proxy, WebProxy)


def test_normalize_proxy_socks5_dict_form() -> None:
    proxy = normalize_proxy(
        {
            "scheme": "socks5",
            "hostname": "1.2.3.4",
            "port": 1080,
            "username": "user",
            "password": "pass",
        }
    )

    assert proxy == SOCKS5Proxy(hostname="1.2.3.4", port=1080, username="user", password="pass")


def test_normalize_proxy_socks4_dict_form_without_credentials() -> None:
    proxy = normalize_proxy({"scheme": "socks4", "hostname": "1.2.3.4", "port": 1080})

    assert proxy == SOCKS4Proxy(hostname="1.2.3.4", port=1080)


def test_normalize_proxy_http_dict_form() -> None:
    proxy = normalize_proxy({"scheme": "http", "hostname": "1.2.3.4", "port": 8080})

    assert isinstance(proxy, HTTPProxy)


def test_normalize_proxy_mtproxy_dict_form() -> None:
    proxy = normalize_proxy(
        {"scheme": "mtproxy", "hostname": "1.2.3.4", "port": 443, "secret": PLAIN_SECRET_HEX}
    )

    assert isinstance(proxy, MTProxy)
    assert proxy.port == 443
    assert proxy.secret == bytes.fromhex(PLAIN_SECRET_HEX)


def test_normalize_proxy_unknown_scheme_raises() -> None:
    with pytest.raises(ValueError):
        normalize_proxy({"scheme": "quic", "hostname": "1.2.3.4", "port": 443})


def test_normalize_proxy_missing_scheme_raises() -> None:
    with pytest.raises(ValueError):
        normalize_proxy({"hostname": "1.2.3.4", "port": 443})


@pytest.mark.parametrize("field_name", ["hostname", "port"])
def test_normalize_proxy_socks_missing_required_field_raises(field_name: str) -> None:
    proxy = {"scheme": "socks5", "hostname": "1.2.3.4", "port": 1080}
    del proxy[field_name]

    with pytest.raises(ValueError):
        normalize_proxy(proxy)


def test_normalize_proxy_web_missing_secret_raises() -> None:
    with pytest.raises(ValueError):
        normalize_proxy({"scheme": "web", "hostname": "relay.example.com"})


def test_normalize_proxy_web_ee_secret_names_the_relay() -> None:
    with pytest.raises(ValueError, match="the relay would need to add"):
        normalize_proxy(
            {
                "scheme": "web",
                "hostname": "relay.example.com",
                "secret": "ee" + PLAIN_SECRET_HEX + SNI_DOMAIN.encode("ascii").hex(),
            }
        )


def test_normalize_proxy_mtproxy_ee_secret_splits_key_from_sni_domain() -> None:
    proxy = normalize_proxy(
        {
            "scheme": "mtproxy",
            "hostname": "1.2.3.4",
            "port": 443,
            "secret": "ee" + PLAIN_SECRET_HEX + SNI_DOMAIN.encode("ascii").hex(),
        }
    )

    assert proxy == MTProxy(
        hostname="1.2.3.4",
        port=443,
        secret=bytes.fromhex(PLAIN_SECRET_HEX),
        sni_hostname=SNI_DOMAIN,
    )


def test_normalize_proxy_mtproxy_ee_secret_takes_a_domain_of_the_maximum_length() -> None:
    domain = "a" * _MAX_SNI_DOMAIN_SIZE

    proxy = normalize_proxy(
        {
            "scheme": "mtproxy",
            "hostname": "1.2.3.4",
            "port": 443,
            "secret": "ee" + PLAIN_SECRET_HEX + domain.encode("ascii").hex(),
        }
    )

    assert proxy.sni_hostname == domain


def test_normalize_proxy_mtproxy_sixteen_byte_secret_is_plain_whatever_its_first_byte() -> None:
    # A marker byte only marks anything at 17 bytes and up, so roughly one plain
    #  secret in 256 opens with a byte that would otherwise read as one.
    secret_hex = "ee" + PLAIN_SECRET_HEX[:-2]

    proxy = normalize_proxy(
        {"scheme": "mtproxy", "hostname": "1.2.3.4", "port": 443, "secret": secret_hex}
    )

    assert proxy.secret == bytes.fromhex(secret_hex)
    assert proxy.sni_hostname is None


@pytest.mark.parametrize(
    "secret_hex",
    [
        pytest.param("ee" + PLAIN_SECRET_HEX, id="ee-no-domain"),
        pytest.param("ee" + PLAIN_SECRET_HEX + "ff", id="ee-non-ascii-domain"),
        pytest.param(
            "ee" + PLAIN_SECRET_HEX + ("a" * (_MAX_SNI_DOMAIN_SIZE + 1)).encode("ascii").hex(),
            id="ee-over-long-domain",
        ),
        pytest.param("dd" + PLAIN_SECRET_HEX + "61", id="dd-with-a-trailing-domain"),
    ],
)
def test_normalize_proxy_mtproxy_malformed_secret_raises(secret_hex: str) -> None:
    with pytest.raises(ValueError):
        normalize_proxy(
            {"scheme": "mtproxy", "hostname": "1.2.3.4", "port": 443, "secret": secret_hex}
        )


@pytest.mark.parametrize("secret_hex", [PLAIN_SECRET_HEX, DD_SECRET_HEX])
def test_normalize_proxy_mtproxy_secret_without_ee_marker_has_no_sni(secret_hex: str) -> None:
    # Only fake-TLS needs a domain, so nothing else may invent one - the transport
    #  decides whether to speak TLS by this field being set.
    proxy = normalize_proxy(
        {"scheme": "mtproxy", "hostname": "1.2.3.4", "port": 443, "secret": secret_hex}
    )

    assert proxy.sni_hostname is None


def test_normalize_proxy_invalid_secret_length_raises() -> None:
    with pytest.raises(ValueError):
        normalize_proxy({"scheme": "web", "hostname": "relay.example.com", "secret": "aabbcc"})


def test_normalize_proxy_wrong_type_raises() -> None:
    with pytest.raises(TypeError):
        normalize_proxy(12345)


@pytest.mark.parametrize(
    "link",
    [
        f"tg://webproxy?server=relay.example.com&secret={PLAIN_SECRET_HEX}",
        f"https://t.me/webproxy?server=relay.example.com&secret={PLAIN_SECRET_HEX}",
        # `host=` is the alias the Android fork's links use.
        f"tg://webproxy?host=relay.example.com&secret={PLAIN_SECRET_HEX}",
    ],
)
def test_normalize_proxy_web_string_link_forms(link: str) -> None:
    web_proxy = normalize_proxy(link)

    assert isinstance(web_proxy, WebProxy)
    assert web_proxy.hostname == "relay.example.com"
    assert web_proxy.secret == bytes.fromhex(PLAIN_SECRET_HEX)


def test_normalize_proxy_web_string_link_missing_secret_raises() -> None:
    with pytest.raises(ValueError):
        normalize_proxy("tg://webproxy?server=relay.example.com")


def test_normalize_proxy_socks_telegram_link_form() -> None:
    proxy = normalize_proxy("tg://socks?server=1.2.3.4&port=1080&user=user&pass=pass")

    assert proxy == SOCKS5Proxy(hostname="1.2.3.4", port=1080, username="user", password="pass")


def test_normalize_proxy_generic_url_form() -> None:
    proxy = normalize_proxy("socks5://user:pass@1.2.3.4:1080")

    assert proxy == SOCKS5Proxy(hostname="1.2.3.4", port=1080, username="user", password="pass")


def test_normalize_proxy_generic_url_decodes_an_escaped_credential() -> None:
    proxy = normalize_proxy("socks5://us%40er:p%40ss%3A1@1.2.3.4:1080")

    assert proxy == SOCKS5Proxy(
        hostname="1.2.3.4", port=1080, username="us@er", password="p@ss:1"
    ), (
        "a URL forces `@` and `:` in the userinfo to be escaped, so leaving them "
        "escaped sends the escape text to the proxy as the credential"
    )


def test_normalize_proxy_spells_one_credential_the_same_in_both_link_forms() -> None:
    from_url = normalize_proxy("socks5://us%40er:p%40ss@1.2.3.4:1080")
    from_tg_link = normalize_proxy("tg://socks?server=1.2.3.4&port=1080&user=us%40er&pass=p%40ss")

    assert from_url == from_tg_link


def test_normalize_proxy_generic_url_form_without_port_raises() -> None:
    with pytest.raises(ValueError):
        normalize_proxy("socks5://1.2.3.4")


def test_client_proxy_address_reports_an_mtproxy() -> None:
    mtproxy = MTProxy(hostname="1.2.3.4", port=443, secret=bytes.fromhex(PLAIN_SECRET_HEX))

    assert client_proxy_address(mtproxy) == ProxyAddress(hostname="1.2.3.4", port=443)


def test_client_proxy_address_reports_a_web_proxy_on_the_https_port() -> None:
    web_proxy = WebProxy(hostname="relay.example.com", secret=bytes.fromhex(PLAIN_SECRET_HEX))

    assert client_proxy_address(web_proxy) == ProxyAddress(
        hostname="relay.example.com", port=HTTPS_PORT
    )


@pytest.mark.parametrize(
    "proxy",
    [
        None,
        SOCKS4Proxy(hostname="1.2.3.4", port=1080),
        SOCKS5Proxy(hostname="1.2.3.4", port=1080),
        HTTPProxy(hostname="1.2.3.4", port=8080),
    ],
)
def test_client_proxy_address_reports_nothing_for_a_proxy_telegram_does_not_own(
    proxy: Proxy | None,
) -> None:
    assert client_proxy_address(proxy) is None


def _base64url(secret_hex: str) -> str:
    # Telegram's own links drop the padding, so the tests carry the same shape.
    return base64.urlsafe_b64encode(bytes.fromhex(secret_hex)).decode("ascii").rstrip("=")


@pytest.mark.parametrize(
    "link",
    [
        "tg://proxy?server=1.2.3.4&port=443&secret=" + PLAIN_SECRET_HEX,
        "https://t.me/proxy?server=1.2.3.4&port=443&secret=" + PLAIN_SECRET_HEX,
        "t.me/proxy?server=1.2.3.4&port=443&secret=" + PLAIN_SECRET_HEX,
        "https://telegram.me/proxy?server=1.2.3.4&port=443&secret=" + _base64url(PLAIN_SECRET_HEX),
    ],
)
def test_normalize_proxy_mtproxy_string_link_forms(link: str) -> None:
    proxy = normalize_proxy(link)

    assert proxy == MTProxy(hostname="1.2.3.4", port=443, secret=bytes.fromhex(PLAIN_SECRET_HEX))


def test_normalize_proxy_mtproxy_link_carries_a_dd_secret_whole() -> None:
    proxy = normalize_proxy("tg://proxy?server=1.2.3.4&port=443&secret=" + DD_SECRET_HEX)

    assert proxy == MTProxy(hostname="1.2.3.4", port=443, secret=bytes.fromhex(DD_SECRET_HEX))


def test_normalize_proxy_mtproxy_link_splits_a_base64url_ee_secret() -> None:
    # The form an ee proxy is actually shared in: base64url, no padding.
    ee_secret_hex = "ee" + PLAIN_SECRET_HEX + SNI_DOMAIN.encode("ascii").hex()
    proxy = normalize_proxy(
        "tg://proxy?server=1.2.3.4&port=443&secret=" + _base64url(ee_secret_hex)
    )

    assert proxy == MTProxy(
        hostname="1.2.3.4",
        port=443,
        secret=bytes.fromhex(PLAIN_SECRET_HEX),
        sni_hostname=SNI_DOMAIN,
    )


@pytest.mark.parametrize(
    "link",
    [
        "tg://proxy?server=1.2.3.4&port=443",
        "tg://proxy?server=1.2.3.4&secret=" + PLAIN_SECRET_HEX,
        "tg://proxy?port=443&secret=" + PLAIN_SECRET_HEX,
    ],
)
def test_normalize_proxy_mtproxy_link_missing_a_param_raises(link: str) -> None:
    with pytest.raises(ValueError):
        normalize_proxy(link)


def test_normalize_proxy_webproxy_link_is_not_read_as_an_mtproxy_one() -> None:
    # `/proxy?` is a suffix of `/webproxy?`, so the two patterns can collide.
    proxy = normalize_proxy(
        "https://t.me/webproxy?server=relay.example.com&secret=" + PLAIN_SECRET_HEX
    )

    assert isinstance(proxy, WebProxy)


# Not `PLAIN_SECRET_HEX`: its base64 and base64url forms come out byte-identical,
#  so two of the three vectors below would be the same string and only one
#  alphabet would ever be exercised. This one ends `/w` under base64 and `_w`
#  under base64url.
_ALPHABET_SENSITIVE_SECRET_HEX: Final[str] = "00112233445566778899aabbccddeeff"


@pytest.mark.parametrize(
    "encoded_secret",
    [
        _ALPHABET_SENSITIVE_SECRET_HEX,
        _base64url(_ALPHABET_SENSITIVE_SECRET_HEX),
        base64.b64encode(bytes.fromhex(_ALPHABET_SENSITIVE_SECRET_HEX)).decode("ascii"),
    ],
)
def test_normalize_proxy_mtproxy_accepts_every_encoding_tdlib_accepts(encoded_secret: str) -> None:
    proxy = normalize_proxy(
        {"scheme": "mtproxy", "hostname": "1.2.3.4", "port": 443, "secret": encoded_secret}
    )

    assert isinstance(proxy, MTProxy)
    assert proxy.secret == bytes.fromhex(_ALPHABET_SENSITIVE_SECRET_HEX)


def test_normalize_proxy_mtproxy_rejects_a_secret_in_no_known_encoding() -> None:
    with pytest.raises(ValueError):
        normalize_proxy(
            {"scheme": "mtproxy", "hostname": "1.2.3.4", "port": 443, "secret": "not a secret!"}
        )


# The obfuscated2 handshake is one fixed-size buffer; only its last 8 bytes are
#  encrypted.
_OBFUSCATED2_HEADER_SIZE: Final[int] = 64


# An address the transport must never dial when a proxy is configured. TEST-NET-2
#  is unroutable, so a connect that reaches for it fails instead of passing.
_UNREACHABLE_DC_ADDRESS: Final[tuple[str, int]] = ("198.51.100.1", 443)
_DC_ID: Final[int] = 2


# The offset of the greeting's random field, which both sides authenticate -
#  5 bytes of record header, 4 of handshake header, 2 of version.
_RANDOM_OFFSET: Final[int] = 11
_RANDOM_SIZE: Final[int] = 32


# The last four bytes of the greeting's random carry the clock.
_TIMESTAMP_SIZE: Final[int] = 4


# The intermediate framing opens with a little-endian length.
_LENGTH_PREFIX_SIZE: Final[int] = 4


def _web_proxy(secret_hex: str = PLAIN_SECRET_HEX) -> WebProxy:
    return WebProxy(hostname="relay.example.com", secret=bytes.fromhex(secret_hex))


# Every `serve` below is an `asyncio.start_server` handler, which the stdlib calls
#  with two positional arguments - so those signatures are its shape, not ours.

# Each one closes its writer on the way out. On 3.12 `Server` counts live connections
#  by hand and `wait_closed()` never returns while one is still attached, so a handler
#  that just returned hung eight tests here until the run's own timeout. 3.13 replaced
#  that counter with a `WeakSet`, which the garbage collector empties on its own, and
#  3.11 returned from `wait_closed()` without waiting at all.
#  https://github.com/python/cpython/blob/3bb231a6a5dc02b95658877318bf61501a7209e9/Lib/asyncio/base_events.py#L298-L302
class _ProxyStub(NamedTuple):
    server: asyncio.AbstractServer
    port: int
    received: asyncio.Future[bytes]


async def _start_proxy_stub(*, read_bytes: int) -> _ProxyStub:
    """A local server standing in for an MTProxy: reads `read_bytes` and stops."""
    received: asyncio.Future[bytes] = asyncio.get_running_loop().create_future()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            received.set_result(await reader.readexactly(read_bytes))

        finally:
            writer.close()

    server = await asyncio.start_server(serve, host="127.0.0.1", port=0)

    return _ProxyStub(server=server, port=server.sockets[0].getsockname()[1], received=received)


async def test_tcp_takes_an_already_normalized_proxy_dataclass() -> None:
    web_proxy = _web_proxy()

    transport = TCP(proxy=web_proxy, dc_id=2)

    assert transport.is_web_proxy
    assert transport.proxy is web_proxy


async def test_tcp_is_not_web_proxy_for_a_socks_proxy() -> None:
    transport = TCP(proxy=SOCKS5Proxy(hostname="1.2.3.4", port=1080), dc_id=2)

    assert not transport.is_web_proxy


async def test_tcp_is_not_web_proxy_when_no_proxy_is_set() -> None:
    assert not TCP(dc_id=2).is_web_proxy


class _SlowConnect(TCPAbridged):
    # Stands in for a WEB handshake that outlasts `TCP.TIMEOUT`. The real one
    #  can: its own steps budget 10s for the TLS connect, 10s per uplink POST
    #  over two attempts and 30s for the WELCOME.

    def __init__(self, proxy: Proxy) -> None:
        super().__init__(proxy=proxy, dc_id=2)
        self.finished = False

    async def _connect(self, destination: tuple[str, int]) -> None:
        await asyncio.sleep(TCP.TIMEOUT * 4)
        self.finished = True


@pytest.fixture()
def short_connect_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    # Small enough that a handshake outlasting it does not slow the suite down.
    monkeypatch.setattr(TCP, "TIMEOUT", 0.01)
    monkeypatch.setattr(TCP, "CONNECT_TIMEOUT", 0.01)


async def test_connect_does_not_cut_a_web_handshake_short(short_connect_timeout: None) -> None:
    transport = _SlowConnect(_web_proxy())

    await transport.connect(("1.2.3.4", 443))

    assert transport.finished


async def test_connect_still_bounds_every_other_scheme(short_connect_timeout: None) -> None:
    transport = _SlowConnect(SOCKS5Proxy(hostname="1.2.3.4", port=1080))

    with pytest.raises(TimeoutError, match="Connection timed out"):
        await transport.connect(("1.2.3.4", 443))

    assert not transport.finished


async def test_connect_via_web_proxy_requires_dc_id() -> None:
    transport = TCPAbridged(proxy=_web_proxy(), dc_id=None)

    with pytest.raises(ValueError, match="dc_id"):
        await transport._connect_via_web_proxy()


async def test_connect_via_web_proxy_requires_an_obfuscate_tag() -> None:
    # Bare `TCP` leaves `OBFUSCATE_TAG` unset; only its packet-framing
    #  subclasses define one.
    transport = TCP(proxy=_web_proxy(), dc_id=2)

    with pytest.raises(ValueError, match="OBFUSCATE_TAG"):
        await transport._connect_via_web_proxy()


async def test_connect_via_web_proxy_rejects_dd_secret_on_the_wrong_class() -> None:
    # A dd-prefixed secret asks for padded intermediate framing, which only
    #  `TCPPaddedIntermediate` speaks.
    transport = TCPAbridged(proxy=_web_proxy(DD_SECRET_HEX), dc_id=2)

    with pytest.raises(ValueError, match="TCPPaddedIntermediate"):
        await transport._connect_via_web_proxy()


async def test_connect_via_mtproxy_rejects_a_dd_secret_on_the_wrong_class() -> None:
    # The same check the WEB scheme makes, reached through the shared helper.
    mtproxy = MTProxy(hostname="1.2.3.4", port=443, secret=bytes.fromhex(DD_SECRET_HEX))
    transport = TCPAbridged(proxy=mtproxy, dc_id=_DC_ID)

    with pytest.raises(ValueError, match="TCPPaddedIntermediate"):
        await transport._connect_via_mtproxy()


async def test_connect_via_mtproxy_rejects_an_ee_secret_on_the_wrong_class() -> None:
    # An ee secret is 18 bytes or more before its marker and domain come off, so
    #  it asks for random padding the same way a dd one does.
    mtproxy = MTProxy(
        hostname="1.2.3.4",
        port=443,
        secret=bytes.fromhex(PLAIN_SECRET_HEX),
        sni_hostname=SNI_DOMAIN,
    )
    transport = TCPAbridged(proxy=mtproxy, dc_id=_DC_ID)

    with pytest.raises(ValueError, match="TCPPaddedIntermediate"):
        await transport._connect_via_mtproxy()


class _FakeTlsStub(NamedTuple):
    server: asyncio.AbstractServer
    port: int
    hello: asyncio.Future[bytes]
    received: asyncio.Future[bytes]


def _server_hello(client_random: bytes, *, secret: bytes) -> bytes:
    """The two segments a real proxy answers a greeting with, signed with `secret`."""
    # A ServerHello of the shape TDLib expects, then a change-cipher-spec glued
    #  to an empty application record.
    body = b"\x02\x00\x00\x4c\x03\x03" + bytes(_RANDOM_SIZE) + bytes(42)
    response = (
        b"\x16\x03\x03"
        + len(body).to_bytes(RECORD_LENGTH_SIZE, "big")
        + body
        + CHANGE_CIPHER_SPEC
        + APPLICATION_DATA_PREFIX
        + bytes(RECORD_LENGTH_SIZE)
    )
    digest = hmac.new(secret, client_random + response, hashlib.sha256).digest()

    return response[:_RANDOM_OFFSET] + digest + response[_RANDOM_OFFSET + _RANDOM_SIZE :]


def _client_decrypt_args(header: bytes, *, secret: bytes) -> tuple[bytes, bytearray, bytearray]:
    # The proxy sends under the client's receive keys, which `build_obfuscated2_header`
    #  derives from the same nonce read backwards.
    tail = bytes(bytearray(header)[55:7:-1])

    return hashlib.sha256(tail[:32] + secret).digest(), bytearray(tail[32:48]), bytearray(1)


def _as_records(payload: bytes, *, record_size: int) -> bytes:
    wire = bytearray()

    for start in range(0, len(payload), record_size):
        piece = payload[start : start + record_size]
        wire += APPLICATION_DATA_PREFIX + len(piece).to_bytes(RECORD_LENGTH_SIZE, "big") + piece

    return bytes(wire)


async def _start_fake_tls_stub(
    *,
    secret: bytes,
    expects_packet: bool = False,
    reply: bytes = b"",
    reply_record_size: int = 1,
) -> _FakeTlsStub:
    """A local server standing in for a fake-TLS MTProxy that knows `secret`."""
    loop = asyncio.get_running_loop()
    hello: asyncio.Future[bytes] = loop.create_future()
    received: asyncio.Future[bytes] = loop.create_future()

    async def serve(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await reader.readexactly(RECORD_HEADER_SIZE)
            greeting = head + await reader.readexactly(int.from_bytes(head[3:5], "big"))
            hello.set_result(greeting)

            writer.write(
                _server_hello(
                    greeting[_RANDOM_OFFSET : _RANDOM_OFFSET + _RANDOM_SIZE], secret=secret
                )
            )
            await writer.drain()

            if not expects_packet:
                return

            # Read what a real proxy reads - the change-cipher-spec and then one whole
            #  application record - rather than a byte count the random padding decides.
            #  Reading exactly what the length field declares is also what checks it:
            #  a record that under-declares leaves the stub waiting until the test's
            #  own timeout.
            prologue = await reader.readexactly(len(CHANGE_CIPHER_SPEC) + RECORD_HEADER_SIZE)
            body = await reader.readexactly(int.from_bytes(prologue[-RECORD_LENGTH_SIZE:], "big"))
            received.set_result(prologue + body)

            if not reply:
                return

            encrypted = aes.ctr256_encrypt(
                reply,
                *_client_decrypt_args(body[:_OBFUSCATED2_HEADER_SIZE], secret=secret),
            )

            writer.write(_as_records(encrypted, record_size=reply_record_size))
            await writer.drain()

        finally:
            writer.close()

    server = await asyncio.start_server(serve, host="127.0.0.1", port=0)

    return _FakeTlsStub(
        server=server,
        port=server.sockets[0].getsockname()[1],
        hello=hello,
        received=received,
    )


def _fake_tls_mtproxy(port: int, *, secret: bytes) -> MTProxy:
    return MTProxy(hostname="127.0.0.1", port=port, secret=secret, sni_hostname=SNI_DOMAIN)


async def test_connect_via_mtproxy_greets_a_fake_tls_proxy_with_a_signed_client_hello() -> None:
    secret = bytes.fromhex(PLAIN_SECRET_HEX)
    stub = await _start_fake_tls_stub(secret=secret)
    transport = TCPPaddedIntermediate(
        proxy=_fake_tls_mtproxy(stub.port, secret=secret), dc_id=_DC_ID
    )

    try:
        await transport.connect(_UNREACHABLE_DC_ADDRESS)
        greeting = await asyncio.wait_for(stub.hello, timeout=TCP.TIMEOUT)
    finally:
        await transport.close()
        stub.server.close()
        await stub.server.wait_closed()

    assert greeting[:3] == b"\x16\x03\x01"  # the ClientHello record header
    assert SNI_DOMAIN.encode("ascii") in greeting

    # The proxy authenticates the greeting exactly this way before answering it.
    zeroed = (
        greeting[:_RANDOM_OFFSET] + bytes(_RANDOM_SIZE) + greeting[_RANDOM_OFFSET + _RANDOM_SIZE :]
    )
    digest = bytearray(hmac.new(secret, zeroed, hashlib.sha256).digest())
    stamped = greeting[
        _RANDOM_OFFSET + _RANDOM_SIZE - _TIMESTAMP_SIZE : _RANDOM_OFFSET + _RANDOM_SIZE
    ]
    stamp = int.from_bytes(bytes(digest[-_TIMESTAMP_SIZE:]), "little") ^ int.from_bytes(
        stamped, "little"
    )

    assert greeting[_RANDOM_OFFSET : _RANDOM_OFFSET + _RANDOM_SIZE - _TIMESTAMP_SIZE] == bytes(
        digest[:-_TIMESTAMP_SIZE]
    )
    # How far the value read back may sit from this machine's own clock before
    #  the test calls it wrong.
    assert abs(stamp - int(time.time())) < 60


async def test_connect_via_mtproxy_wraps_the_handshake_in_application_records() -> None:
    # The change-cipher-spec, then one record holding the obfuscated2 header and
    #  the first framed packet - the header rides with that packet rather than in
    #  a record of its own.
    payload: Final[bytes] = b"\x01\x02\x03\x04"
    secret = bytes.fromhex(PLAIN_SECRET_HEX)

    stub = await _start_fake_tls_stub(secret=secret, expects_packet=True)
    transport = TCPPaddedIntermediate(
        proxy=_fake_tls_mtproxy(stub.port, secret=secret), dc_id=_DC_ID
    )

    try:
        await transport.connect(_UNREACHABLE_DC_ADDRESS)
        await transport.send(payload)
        stream = await asyncio.wait_for(stub.received, timeout=TCP.TIMEOUT)
    finally:
        await transport.close()
        stub.server.close()
        await stub.server.wait_closed()

    assert stream[: len(CHANGE_CIPHER_SPEC)] == CHANGE_CIPHER_SPEC

    record = stream[len(CHANGE_CIPHER_SPEC) :]

    assert record[: len(APPLICATION_DATA_PREFIX)] == APPLICATION_DATA_PREFIX

    framed = record[RECORD_HEADER_SIZE:]
    key = hashlib.sha256(framed[8:40] + secret).digest()
    decrypted = aes.ctr256_decrypt(framed, key, bytearray(framed[40:56]), bytearray(1))

    assert decrypted[56:60] == PADDED_INTERMEDIATE_OBFUSCATE_TAG
    assert int.from_bytes(decrypted[60:62], "little", signed=True) == _DC_ID

    packet = decrypted[_OBFUSCATED2_HEADER_SIZE:]
    framed_length = int.from_bytes(packet[:_LENGTH_PREFIX_SIZE], "little", signed=True)

    assert len(packet) == _LENGTH_PREFIX_SIZE + framed_length
    assert packet[_LENGTH_PREFIX_SIZE : _LENGTH_PREFIX_SIZE + len(payload)] == payload
    # The padded transport appends up to 15 random bytes after the payload.
    assert 0 <= framed_length - len(payload) <= 15


async def test_connect_via_mtproxy_reads_a_reply_split_across_several_records() -> None:
    # A record boundary has nothing to do with a packet boundary, so a reply the
    #  proxy cut up must still arrive as the byte stream the transport asked for.
    payload: Final[bytes] = b"\x01\x02\x03\x04"

    # Shorter than 24 bytes and the padded transport reads the frame as a quick
    #  ack or an error code rather than as a packet.
    reply: Final[bytes] = bytes(range(24))
    secret = bytes.fromhex(PLAIN_SECRET_HEX)

    stub = await _start_fake_tls_stub(
        secret=secret,
        expects_packet=True,
        reply=len(reply).to_bytes(_LENGTH_PREFIX_SIZE, "little", signed=True) + reply,
        reply_record_size=3,
    )
    transport = TCPPaddedIntermediate(
        proxy=_fake_tls_mtproxy(stub.port, secret=secret), dc_id=_DC_ID
    )

    try:
        await transport.connect(_UNREACHABLE_DC_ADDRESS)
        await transport.send(payload)
        received = await asyncio.wait_for(transport.recv(), timeout=TCP.TIMEOUT)
    finally:
        await transport.close()
        stub.server.close()
        await stub.server.wait_closed()

    assert received == reply


async def test_connect_via_mtproxy_rejects_a_fake_tls_reply_signed_with_another_secret() -> None:
    # Without this check a censor could answer with any plausible ServerHello and
    #  watch what the client does next.
    stub = await _start_fake_tls_stub(secret=bytes(16))
    transport = TCPPaddedIntermediate(
        proxy=_fake_tls_mtproxy(stub.port, secret=bytes.fromhex(PLAIN_SECRET_HEX)),
        dc_id=_DC_ID,
    )

    try:
        with pytest.raises(OSError, match="without knowing the proxy secret"):
            await transport.connect(_UNREACHABLE_DC_ADDRESS)
    finally:
        await transport.close()
        stub.server.close()
        await stub.server.wait_closed()


async def test_connect_via_mtproxy_rejects_a_fake_tls_reply_that_is_not_a_server_hello() -> None:
    # What a plain web server on the configured port answers with.
    async def serve(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            writer.write(b"HTTP/1.1 400 Bad Request\r\n\r\n")
            await writer.drain()

        finally:
            writer.close()

    server = await asyncio.start_server(serve, host="127.0.0.1", port=0)
    transport = TCPPaddedIntermediate(
        proxy=_fake_tls_mtproxy(
            server.sockets[0].getsockname()[1], secret=bytes.fromhex(PLAIN_SECRET_HEX)
        ),
        dc_id=_DC_ID,
    )

    try:
        with pytest.raises(OSError, match="not answered with a ServerHello"):
            await transport.connect(_UNREACHABLE_DC_ADDRESS)
    finally:
        await transport.close()
        server.close()
        await server.wait_closed()


@pytest.mark.parametrize(
    ("protocol_factory", "secret_hex", "expected_tag"),
    [
        pytest.param(TCPAbridged, PLAIN_SECRET_HEX, ABRIDGED_OBFUSCATE_TAG, id="plain-abridged"),
        pytest.param(
            TCPPaddedIntermediate,
            DD_SECRET_HEX,
            PADDED_INTERMEDIATE_OBFUSCATE_TAG,
            id="dd-padded",
        ),
    ],
)
async def test_connect_via_mtproxy_sends_a_header_the_proxy_can_read(
    protocol_factory: type[TCP],
    secret_hex: str,
    expected_tag: bytes,
) -> None:
    # Reads the handshake back exactly as stock MTProxy does: derive the keys from
    #  the cleartext half, then run the whole 64-byte buffer through CTR so the tag
    #  and dc id at 56-62 are decrypted at the keystream offset they were written at.
    stub = await _start_proxy_stub(read_bytes=_OBFUSCATED2_HEADER_SIZE)

    full_secret = bytes.fromhex(secret_hex)
    transport = protocol_factory(
        proxy=MTProxy(hostname="127.0.0.1", port=stub.port, secret=full_secret),
        dc_id=_DC_ID,
    )

    try:
        await transport.connect(_UNREACHABLE_DC_ADDRESS)
        header = await asyncio.wait_for(stub.received, timeout=TCP.TIMEOUT)
    finally:
        await transport.close()
        stub.server.close()
        await stub.server.wait_closed()

    bare_secret = full_secret[1:] if len(full_secret) == 17 else full_secret
    key = hashlib.sha256(header[8:40] + bare_secret).digest()
    decrypted = aes.ctr256_decrypt(header, key, bytearray(header[40:56]), bytearray(1))

    assert decrypted[56:60] == expected_tag
    assert int.from_bytes(decrypted[60:62], "little", signed=True) == _DC_ID


async def test_connect_via_mtproxy_leaves_no_bare_tag_before_the_first_packet() -> None:
    # TCPAbridged opens a plain connection with a bare 0xef byte. Over MTProxy the
    #  handshake already carries that tag, so sending it again would shift the whole
    #  stream by one byte and the proxy would read a 0xef-long packet.
    payload: Final[bytes] = b"\x01\x02\x03\x04"

    # The header, then the abridged length byte and the payload behind it.
    stub = await _start_proxy_stub(read_bytes=_OBFUSCATED2_HEADER_SIZE + 1 + len(payload))

    secret = bytes.fromhex(PLAIN_SECRET_HEX)
    transport = TCPAbridged(
        proxy=MTProxy(hostname="127.0.0.1", port=stub.port, secret=secret),
        dc_id=_DC_ID,
    )

    try:
        await transport.connect(_UNREACHABLE_DC_ADDRESS)
        await transport.send(payload)
        stream = await asyncio.wait_for(stub.received, timeout=TCP.TIMEOUT)
    finally:
        await transport.close()
        stub.server.close()
        await stub.server.wait_closed()

    key = hashlib.sha256(stream[8:40] + secret).digest()
    decrypted = aes.ctr256_decrypt(stream, key, bytearray(stream[40:56]), bytearray(1))

    # One 4-byte word, so the abridged length byte is 1 and the payload follows it.
    assert decrypted[_OBFUSCATED2_HEADER_SIZE:] == bytes([len(payload) // 4]) + payload


async def test_build_proxy_keeps_a_credential_a_url_would_mangle() -> None:
    # `SocksProxy.from_url` parses the credentials back out with `unquote()`,
    #  so a password holding `@`, `:` or `%` came out different from the one
    #  the caller passed, and the proxy rejected the login.
    socks_proxy = SOCKS5Proxy(
        hostname="1.2.3.4",
        port=1080,
        username="user@example.com",
        password="p:a%40ss",
    )
    transport = TCPAbridged(proxy=socks_proxy, dc_id=2)

    dialed = await transport._build_socks_proxy()

    assert dialed._username == socks_proxy.username
    assert dialed._password == socks_proxy.password


async def test_build_proxy_keeps_a_username_that_comes_without_a_password() -> None:
    # `parse_proxy_url` resets both credentials to `''` when either is missing,
    #  so a username-only proxy was dialed anonymously.
    socks_proxy = SOCKS5Proxy(hostname="1.2.3.4", port=1080, username="user")
    transport = TCPAbridged(proxy=socks_proxy, dc_id=2)

    dialed = await transport._build_socks_proxy()

    assert dialed._username == "user"


async def test_build_proxy_maps_each_scheme_to_its_dial_type() -> None:
    http_proxy = HTTPProxy(hostname="1.2.3.4", port=8080)
    transport = TCPAbridged(proxy=http_proxy, dc_id=2)

    dialed = await transport._build_socks_proxy()

    assert dialed._proxy_type is ProxyType.HTTP


async def test_build_proxy_rejects_a_scheme_it_cannot_dial() -> None:
    transport = TCPAbridged(proxy=_web_proxy(), dc_id=2)

    with pytest.raises(ValueError, match="WebProxy"):
        await transport._build_socks_proxy()


# The same seven values TDLib refuses, as the little-endian ints it compares.
#  https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/td/mtproto/TcpTransport.cpp#L99-L101
_TDLIB_RESERVED_FIRST_INTS: Final[tuple[int, ...]] = (
    0x44414548,
    0x54534F50,
    0x20544547,
    0x4954504F,
    0xDDDDDDDD,
    0xEEEEEEEE,
    0x02010316,
)


def test_obfuscated2_reserved_prefixes_are_the_ones_tdlib_refuses() -> None:
    # A dropped entry is invisible at runtime - it costs one connection in four
    #  billion - so the list is compared against its source rather than exercised.
    expected = {value.to_bytes(4, "little") for value in _TDLIB_RESERVED_FIRST_INTS}

    assert set(_OBFUSCATED2_RESERVED_PREFIXES) == expected


def test_generate_obfuscated2_nonce_avoids_every_fingerprintable_opening() -> None:
    for _ in range(256):
        nonce = generate_obfuscated2_nonce()

        assert len(nonce) == 64
        assert nonce[0] != ABRIDGED_OBFUSCATE_TAG[0]
        assert bytes(nonce[:4]) not in _OBFUSCATED2_RESERVED_PREFIXES
        assert nonce[4:8] != bytes(4)
_PLAIN_MTPROXY: Final[MTProxy] = MTProxy(
    hostname="11.22.33.44",
    port=443,
    secret=bytes.fromhex(PLAIN_SECRET_HEX),
)


_DD_MTPROXY: Final[MTProxy] = MTProxy(
    hostname="11.22.33.44",
    port=443,
    secret=bytes.fromhex(DD_SECRET_HEX),
)


# An ee secret keeps a bare 16-byte key: its marker and domain came off in
#  `normalize_proxy`, and `sni_hostname` is what records that it was one.
_EE_MTPROXY: Final[MTProxy] = MTProxy(
    hostname="11.22.33.44",
    port=443,
    secret=bytes.fromhex(PLAIN_SECRET_HEX),
    sni_hostname=SNI_DOMAIN,
)


def testprotocol_dc_id_plain() -> None:
    assert protocol_dc_id(2, test_mode=False, media=False) == 2


def testprotocol_dc_id_media_is_negated() -> None:
    assert protocol_dc_id(2, test_mode=False, media=True) == -2


def testprotocol_dc_id_test_mode_is_shifted() -> None:
    assert protocol_dc_id(2, test_mode=True, media=False) == 10002


def testprotocol_dc_id_test_mode_media_shifts_then_negates() -> None:
    assert protocol_dc_id(2, test_mode=True, media=True) == -10002


async def test_connection_computesprotocol_dc_id_from_media_and_test_mode() -> None:
    connection = Connection(dc_id=5, test_mode=True, ipv6=False, media=True, server_address="unused", port=443)
    assert connection.protocol_dc_id == -10005


@pytest.mark.parametrize(
    ("proxy", "expected"),
    [
        pytest.param(None, TCPAbridged, id="no-proxy"),
        pytest.param(SOCKS5Proxy(hostname="11.22.33.44", port=1234), TCPAbridged, id="socks5"),
        pytest.param(_PLAIN_MTPROXY, TCPAbridged, id="mtproxy-plain"),
        pytest.param(_DD_MTPROXY, TCPPaddedIntermediate, id="mtproxy-dd"),
        pytest.param(_EE_MTPROXY, TCPPaddedIntermediate, id="mtproxy-ee"),
        pytest.param(
            WebProxy(hostname="relay.example.com", secret=bytes.fromhex(PLAIN_SECRET_HEX)),
            TCPAbridged,
            id="web-plain",
        ),
        pytest.param(
            WebProxy(hostname="relay.example.com", secret=bytes.fromhex(DD_SECRET_HEX)),
            TCPPaddedIntermediate,
            id="web-dd",
        ),
    ],
)
def test_transport_class_for_reads_the_framing_off_the_secret(
    proxy: Proxy | None,
    expected: type[TCP],
) -> None:
    assert transport_class_for(proxy) is expected


def test_transport_class_for_keeps_the_default_when_the_secret_asks_for_nothing() -> None:
    # A plain secret pads nothing, so whatever the caller picked still stands.
    assert transport_class_for(_PLAIN_MTPROXY, default=TCPFull) is TCPFull


def test_transport_class_for_overrides_a_default_the_secret_contradicts() -> None:
    assert transport_class_for(_EE_MTPROXY, default=TCPFull) is TCPPaddedIntermediate


async def test_connection_takes_its_transport_from_the_proxy_secret() -> None:
    connection = Connection(
        dc_id=2,
        test_mode=False,
        ipv6=False,
        proxy=_EE_MTPROXY,
        server_address="unused",
        port=443,
    )

    assert connection.protocol_factory is TCPPaddedIntermediate


async def test_connection_keeps_the_requested_transport_without_a_padded_secret() -> None:
    connection = Connection(
        dc_id=2,
        test_mode=False,
        ipv6=False,
        proxy=_PLAIN_MTPROXY,
        protocol_factory=TCPFull,
        server_address="unused",
        port=443,
    )

    assert connection.protocol_factory is TCPFull


async def test_a_failure_names_the_proxy_that_was_dialed_not_only_the_dc() -> None:
    class _Refusing(TCPAbridged):
        async def connect(self, address) -> None:
            raise OSError("getaddrinfo failed")

    connection = Connection(
        dc_id=4,
        test_mode=False,
        ipv6=False,
        proxy=_DD_MTPROXY,
        protocol_factory=_Refusing,
        server_address="149.154.167.92",
        port=443,
    )

    with pytest.raises(ConnectionError) as exc:
        await connection.connect()

    assert _DD_MTPROXY.hostname in str(exc.value), (
        "an obfuscated2 proxy is dialed instead of the DC address, so naming only "
        "the DC sends the reader after an address the failure never touched"
    )


async def test_a_failure_without_a_proxy_names_only_the_dc() -> None:
    class _Refusing(TCPAbridged):
        async def connect(self, address) -> None:
            raise OSError("refused")

    connection = Connection(
        dc_id=4,
        test_mode=False,
        ipv6=False,
        protocol_factory=_Refusing,
        server_address="149.154.167.92",
        port=443,
    )

    with pytest.raises(ConnectionError) as exc:
        await connection.connect()

    assert "via" not in str(exc.value)


def test_serialize_parse_round_trip() -> None:
    payload = b"hello mtproxy"
    wire = serialize_frame(FrameType.DATA, stream_id=42, payload=payload)

    parsed = parse_frames(wire)

    assert parsed.consumed == len(wire)
    assert len(parsed.frames) == 1

    assert parsed.frames[0].type == FrameType.DATA
    assert parsed.frames[0].stream_id == 42
    assert parsed.frames[0].payload == payload


def test_parse_concatenated_frames() -> None:
    hello = serialize_frame(FrameType.HELLO, stream_id=0, payload=b"\x01")
    open_stream = serialize_frame(FrameType.OPEN, stream_id=7, payload=b"")
    data = serialize_frame(FrameType.DATA, stream_id=7, payload=b"payload")
    wire = hello + open_stream + data

    parsed = parse_frames(wire)

    assert parsed.consumed == len(wire)
    assert [one_frame.type for one_frame in parsed.frames] == [
        FrameType.HELLO,
        FrameType.OPEN,
        FrameType.DATA,
    ]


def test_parse_trailing_partial_frame_not_consumed() -> None:
    full = serialize_frame(FrameType.PING, stream_id=0, payload=b"")
    partial = bytes([FrameType.DATA, 0, 0, 1, 0, 0, 0])  # header alone, truncated
    wire = full + partial

    parsed = parse_frames(wire)

    assert len(parsed.frames) == 1
    assert parsed.frames[0].type == FrameType.PING
    assert parsed.consumed == len(full)


def test_parse_unknown_type_rejected() -> None:
    wire = bytes([0x7F, 0, 0, 0, 0, 0, 0, 0])  # unknown type, zero-length payload
    with pytest.raises(FrameParseError):
        parse_frames(wire)


def test_parse_oversized_payload_rejected() -> None:
    wire = bytearray(FRAME_HEADER_SIZE)
    wire[0] = FrameType.DATA
    oversized = FRAME_MAX_PAYLOAD + 1
    wire[4:8] = oversized.to_bytes(4, "big")

    with pytest.raises(FrameParseError):
        parse_frames(bytes(wire))


def test_parse_message_rejects_empty_and_partial() -> None:
    with pytest.raises(FrameParseError):
        parse_frame_message(b"")

    full = serialize_frame(FrameType.PONG, stream_id=0, payload=b"")
    trailing = full + b"\x01"
    with pytest.raises(FrameParseError):
        parse_frame_message(trailing)

    assert parse_frame_message(full)[0].type == FrameType.PONG


def test_serialize_stream_id_encoding() -> None:
    wire = serialize_frame(FrameType.WINDOW, stream_id=0x00ABCDEF, payload=b"\x00\x00\x00\x01")
    assert wire[1:4] == b"\xab\xcd\xef"


def test_serialize_rejects_out_of_range_stream_id() -> None:
    with pytest.raises(ValueError):
        serialize_frame(FrameType.DATA, stream_id=0x01000000, payload=b"")


def test_serialize_rejects_oversized_payload() -> None:
    with pytest.raises(ValueError):
        serialize_frame(FrameType.DATA, stream_id=1, payload=b"\x00" * (FRAME_MAX_PAYLOAD + 1))


def test_large_legal_batch_is_not_rejected() -> None:
    # §7.1: the relay may legally batch up to 2 MiB of small frames into one
    #  response. A frame-count cap would make that batch a parse error even
    #  though every frame in it is well-formed.
    wire = b"".join(
        serialize_frame(FrameType.PING, stream_id=0, payload=b"") for _ in range(20_000)
    )

    parsed = parse_frames(wire)

    assert parsed.consumed == len(wire)
    assert len(parsed.frames) == 20_000


def test_window_frame_round_trips_as_four_byte_big_endian_delta() -> None:
    wire = serialize_frame(FrameType.WINDOW, stream_id=1, payload=(256 * 1024).to_bytes(4, "big"))

    frame = parse_frame_message(wire)[0]

    assert frame.type == FrameType.WINDOW
    assert int.from_bytes(frame.payload, "big") == 256 * 1024


def test_derive_bridge_capability_normative_vectors() -> None:
    for host, secret_hex, expected in BRIDGE_CAPABILITY_VECTORS:
        secret = bytes.fromhex(secret_hex)
        assert derive_bridge_capability(host, secret=secret) == expected


def test_derive_bridge_capability_is_sensitive_to_host_and_secret() -> None:
    secret = bytes.fromhex("000102030405060708090a0b0c0d0e0f")
    capability = derive_bridge_capability("proxy.example.com", secret=secret)
    other_host_capability = derive_bridge_capability("other.example.com", secret=secret)
    assert capability != other_host_capability

    other_secret = bytes.fromhex("0f0e0d0c0b0a09080706050403020100")
    other_secret_capability = derive_bridge_capability("proxy.example.com", secret=other_secret)
    assert capability != other_secret_capability


# Proxy config parsing (dict form, string link forms, dd-marker handling,
#  secret validation) now lives in pyrogram.connection.proxy.normalize_proxy
#  and is covered in tests/test_proxy.py; TCP itself only
#  takes an already-normalized Proxy dataclass, covered in
#  tests/test_tcp_proxy.py.
def _connection_reading(raw: bytes) -> _HttpConnection:
    reader = asyncio.StreamReader()
    reader.feed_data(raw)
    reader.feed_eof()

    connection = _HttpConnection("relay.invalid", port=443, ssl_context=None)
    connection._reader = reader

    return connection


class _RecordingWriter:
    def __init__(self, close_error: Exception | None = None) -> None:
        self.closed = False
        self.close_error = close_error

    def close(self) -> None:
        if self.close_error is not None:
            raise self.close_error
        self.closed = True

    def is_closing(self) -> bool:
        return False


def test_drop_connection_closes_the_writer() -> None:
    connection = _HttpConnection("relay.invalid", port=443, ssl_context=None)

    writer = _RecordingWriter()
    connection._writer = writer
    connection._reader = object()

    connection._drop_connection()

    assert connection._writer is None
    assert connection._reader is None
    assert writer.closed is True


def test_drop_connection_is_a_noop_without_a_writer() -> None:
    connection = _HttpConnection("relay.invalid", port=443, ssl_context=None)

    connection._drop_connection()

    assert connection._writer is None
    assert connection._reader is None


def test_drop_connection_swallows_an_oserror_on_close() -> None:
    connection = _HttpConnection("relay.invalid", port=443, ssl_context=None)

    connection._writer = _RecordingWriter(close_error=OSError("close failed"))
    connection._reader = object()

    connection._drop_connection()

    assert connection._writer is None
    assert connection._reader is None


class _SendingWriter(_RecordingWriter):
    def write(self, data: bytes) -> None:
        pass

    async def drain(self) -> None:
        pass


async def test_cancelled_request_drops_the_pooled_connection() -> None:
    connection = _HttpConnection("relay.invalid", port=443, ssl_context=None)

    writer = _SendingWriter()
    connection._writer = writer
    connection._reader = asyncio.StreamReader()

    task = asyncio.create_task(connection.request("POST", path="/up"))
    await asyncio.sleep(0.01)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    assert connection._writer is None
    assert connection._reader is None
    assert writer.closed is True


async def test_read_body_content_length() -> None:
    connection = _connection_reading(b"downlink batch")

    body = await connection._read_body(HTTPStatus.OK, response_headers={"content-length": "14"})

    assert body == b"downlink batch"


async def test_read_body_chunked() -> None:
    # A reverse proxy re-frames larger downlink batches this way, and reading
    #  them as an empty body silently dropped every response above a few KiB.
    connection = _connection_reading(b"5\r\nhello\r\n6\r\n mtprx\r\n0\r\n\r\n")

    body = await connection._read_body(
        HTTPStatus.OK, response_headers={"transfer-encoding": "chunked"}
    )

    assert body == b"hello mtprx"


async def test_read_body_chunked_ignores_extensions_and_trailers() -> None:
    raw = b"5;name=value\r\nhello\r\n0\r\nx-checksum: 1\r\n\r\n"
    connection = _connection_reading(raw)

    body = await connection._read_body(
        HTTPStatus.OK, response_headers={"transfer-encoding": "CHUNKED"}
    )

    assert body == b"hello"


async def test_read_body_chunked_leaves_the_connection_at_the_next_response() -> None:
    raw = b"5\r\nhello\r\n0\r\n\r\nHTTP/1.1 204 No Content\r\n"
    connection = _connection_reading(raw)

    await connection._read_body(HTTPStatus.OK, response_headers={"transfer-encoding": "chunked"})

    assert await connection._reader.readline() == b"HTTP/1.1 204 No Content\r\n"


async def test_read_body_no_content_length_is_rejected() -> None:
    connection = _connection_reading(b"")

    with pytest.raises(ConnectionError, match="neither Content-Length nor chunked"):
        await connection._read_body(HTTPStatus.OK, response_headers={})


async def test_read_body_204_has_no_body() -> None:
    connection = _connection_reading(b"")

    assert await connection._read_body(HTTPStatus.NO_CONTENT, response_headers={}) == b""


async def test_read_body_rejects_a_malformed_chunk_size() -> None:
    connection = _connection_reading(b"zz\r\n")

    with pytest.raises(ConnectionError, match="malformed chunk size"):
        await connection._read_body(
            HTTPStatus.OK, response_headers={"transfer-encoding": "chunked"}
        )


async def test_read_body_rejects_a_truncated_chunked_body() -> None:
    connection = _connection_reading(b"5\r\nhello\r\n")

    with pytest.raises(ConnectionError, match="closed inside a chunked body"):
        await connection._read_body(
            HTTPStatus.OK, response_headers={"transfer-encoding": "chunked"}
        )


class _UplinkRecorder:
    """Stands in for the relay's uplink endpoint, recording every frame the
    carrier puts on the wire instead of POSTing it."""

    def __init__(self, carrier: WebProxyCarrier) -> None:
        self.frames: list[bytes] = []
        carrier._send_frames = self._record

    async def _record(self, frames: list[bytes]) -> None:
        self.frames.extend(frames)

    @property
    def payload_sizes(self) -> list[int]:
        return [len(one_frame) - FRAME_HEADER_SIZE for one_frame in self.frames]

    @property
    def bytes_sent(self) -> int:
        return sum(self.payload_sizes)


def _carrier() -> WebProxyCarrier:
    carrier = WebProxyCarrier("relay.invalid", secret=bytes(16))
    carrier._session_id = "test-session"

    return carrier


def _window_grant(amount: int) -> Frame:
    wire = serialize_frame(
        FrameType.WINDOW, stream_id=_STREAM_ID, payload=amount.to_bytes(4, "big")
    )
    parsed = parse_frames(wire)

    return parsed.frames[0]


async def _run_until_blocked(sending: asyncio.Task[None]) -> None:
    # `send()` suspends once it runs out of credit, and waking it after a WINDOW
    #  grant costs three loop iterations below 3.12, where `asyncio.wait_for` ran the
    #  wait in a task of its own, against one on 3.12+, which awaits the coroutine
    #  directly. A single yield left the grant unspent and failed
    #  `test_send_never_puts_more_on_the_wire_than_the_credit_granted` on 3.10-3.11.
    #  Ten is that measured three with room to spare.
    #  https://github.com/python/cpython/blob/0fb18b02c8ad56299d6a2910be0bab8ad601ef24/Lib/asyncio/tasks.py#L509
    for _ in range(10):
        await asyncio.sleep(0)

    assert not sending.done()


async def test_send_splits_the_payload_at_the_uplink_frame_size() -> None:
    carrier = _carrier()
    recorder = _UplinkRecorder(carrier)
    payload = b"\x00" * (2 * _UPLINK_FRAME_MAX + 100)

    await carrier.send(payload)

    assert recorder.payload_sizes == [_UPLINK_FRAME_MAX, _UPLINK_FRAME_MAX, 100]
    assert carrier._send_window == _INITIAL_STREAM_WINDOW - len(payload)


async def test_send_blocks_once_the_stream_window_is_exhausted() -> None:
    carrier = _carrier()
    recorder = _UplinkRecorder(carrier)
    payload = b"\x00" * (_INITIAL_STREAM_WINDOW + _UPLINK_FRAME_MAX)

    sending = asyncio.ensure_future(carrier.send(payload))
    await _run_until_blocked(sending)

    assert recorder.bytes_sent == _INITIAL_STREAM_WINDOW
    assert carrier._send_window == 0

    carrier._handle_frame(_window_grant(_UPLINK_FRAME_MAX))
    await asyncio.wait_for(sending, timeout=5)

    assert recorder.bytes_sent == len(payload)
    assert carrier._send_window == 0


async def test_send_never_puts_more_on_the_wire_than_the_credit_granted() -> None:
    carrier = _carrier()
    recorder = _UplinkRecorder(carrier)
    payload = b"\x00" * (_INITIAL_STREAM_WINDOW + 3 * _UPLINK_FRAME_MAX)

    sending = asyncio.ensure_future(carrier.send(payload))
    await _run_until_blocked(sending)

    carrier._handle_frame(_window_grant(_UPLINK_FRAME_MAX))
    await _run_until_blocked(sending)

    assert recorder.bytes_sent == _INITIAL_STREAM_WINDOW + _UPLINK_FRAME_MAX

    carrier._handle_frame(_window_grant(2 * _UPLINK_FRAME_MAX))
    await asyncio.wait_for(sending, timeout=5)

    assert recorder.bytes_sent == len(payload)


async def test_send_fails_the_carrier_when_credit_never_arrives(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(web_proxy_carrier, "_CREDIT_WAIT_TIMEOUT", 0.05)
    carrier = _carrier()
    _UplinkRecorder(carrier)
    payload = b"\x00" * (_INITIAL_STREAM_WINDOW + _UPLINK_FRAME_MAX)

    with pytest.raises(WebCarrierError, match="timed out waiting for uplink WINDOW credit"):
        await carrier.send(payload)

    assert carrier._fail_exc is not None


async def test_send_raises_when_the_carrier_fails_while_waiting_for_credit() -> None:
    carrier = _carrier()
    _UplinkRecorder(carrier)
    payload = b"\x00" * (_INITIAL_STREAM_WINDOW + _UPLINK_FRAME_MAX)

    sending = asyncio.ensure_future(carrier.send(payload))
    await _run_until_blocked(sending)

    await carrier._fail(WebCarrierError("relay closed the stream"))

    with pytest.raises(WebCarrierError, match="relay closed the stream"):
        await asyncio.wait_for(sending, timeout=5)


async def test_recv_joins_relay_frames_into_the_requested_length() -> None:
    carrier = _carrier()
    _UplinkRecorder(carrier)

    for chunk in (b"abc", b"defg", b"hi"):
        carrier._recv_queue.put_nowait(chunk)

    assert await carrier.recv(5) == b"abcde"
    assert await carrier.recv(4) == b"fghi"


async def test_recv_returns_none_when_the_stream_ends_mid_read() -> None:
    carrier = _carrier()
    _UplinkRecorder(carrier)

    carrier._recv_queue.put_nowait(b"abc")
    carrier._recv_queue.put_nowait(None)

    assert await carrier.recv(8) is None


async def test_recv_grants_downlink_credit_once_the_threshold_is_reached() -> None:
    carrier = _carrier()
    recorder = _UplinkRecorder(carrier)
    carrier._recv_queue.put_nowait(b"\x00" * _DOWNLINK_GRANT_THRESHOLD)

    await carrier.recv(_DOWNLINK_GRANT_THRESHOLD)

    assert recorder.payload_sizes == [_WINDOW_PAYLOAD_SIZE]
    assert carrier._pending_grant == 0
    assert carrier._recv_window_remaining == _INITIAL_STREAM_WINDOW + _DOWNLINK_GRANT_THRESHOLD


async def test_recv_holds_a_grant_below_the_threshold() -> None:
    carrier = _carrier()
    recorder = _UplinkRecorder(carrier)
    carrier._recv_queue.put_nowait(b"\x00" * 16)

    await carrier.recv(16)

    assert recorder.frames == []
    assert carrier._pending_grant == 16


async def _run_failing_tracked_task(carrier: WebProxyCarrier) -> None:
    """Track a task that raises, and let its done callbacks run.

    Nothing awaits a tracked task, so an unretrieved exception reaches asyncio's
    "Task exception was never retrieved" handler and prints a full traceback.
    `asyncio.wait` does not retrieve it the way `gather` would, so this leaves
    the task in exactly the state the callback has to handle.
    """

    async def _fails() -> None:
        raise WebCarrierError("uplink rejected: HTTP 409")

    carrier._track_task(_fails())
    task = next(iter(carrier._background_tasks))

    await asyncio.wait({task})
    # The done callbacks run a loop iteration after the task itself finishes.
    await asyncio.sleep(0)

    assert carrier._background_tasks == set(), "a finished task must not stay in the tracking set"


def _carrier_records(caplog: pytest.LogCaptureFixture) -> list[logging.LogRecord]:
    # `caplog` collects at the root, so `asyncio` logging "Task was destroyed but it is
    #  pending!" over a task an earlier test left behind is otherwise read below as a
    #  line this module emitted.
    return [record for record in caplog.records if record.name == web_proxy_carrier.log.name]


async def test_a_failed_background_task_the_carrier_recorded_is_reported_at_debug(
    caplog: pytest.LogCaptureFixture,
) -> None:
    carrier = _carrier()
    carrier._fail_exc = WebCarrierError("uplink rejected: HTTP 409")

    with caplog.at_level(logging.DEBUG, logger=web_proxy_carrier.log.name):
        await _run_failing_tracked_task(carrier)

    # The next `send()` raises `_fail_exc` at the caller, so this is the second
    #  report of an error that already has an owner.
    assert [(record.levelno, record.getMessage()) for record in _carrier_records(caplog)] == [
        (logging.DEBUG, "WEB proxy: background task failed: uplink rejected: HTTP 409"),
    ]


async def test_a_failed_background_task_nothing_recorded_is_reported_at_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    carrier = _carrier()

    with caplog.at_level(logging.DEBUG, logger=web_proxy_carrier.log.name):
        await _run_failing_tracked_task(carrier)

    # `_fail_exc` is unset, so no caller will ever be handed this failure and
    #  swallowing it at debug would lose it outright.
    assert [(record.levelno, record.getMessage()) for record in _carrier_records(caplog)] == [
        (
            logging.ERROR,
            "WEB proxy: background task failed with nothing to report it: uplink rejected: HTTP 409",
        ),
    ]


async def test_a_cancelled_background_task_is_not_reported(
    caplog: pytest.LogCaptureFixture,
) -> None:
    carrier = _carrier()

    async def _waits() -> None:
        await asyncio.sleep(60)

    with caplog.at_level(logging.DEBUG, logger=web_proxy_carrier.log.name):
        carrier._track_task(_waits())
        task = next(iter(carrier._background_tasks))

        await carrier._cancel_tracked(task)

    assert _carrier_records(caplog) == []


async def test_recv_after_close_does_not_start_a_grant_task() -> None:
    carrier = _carrier()
    # No session left to `DELETE`, so `close()` needs no network.
    carrier._session_id = None
    carrier._recv_buffer.extend(b"leftover")

    await carrier.close()

    assert await carrier.recv(len(b"leftover")) == b"leftover"

    # `close()` walks `_background_tasks` once and returns, so a grant task
    #  started after it is never cancelled: it sleeps on and then posts a WINDOW
    #  frame down an uplink that is already gone.
    assert carrier._pending_grant == 0
    assert carrier._grant_flush_task is None
    assert carrier._background_tasks == set()
_SECRET: Final[bytes] = bytes.fromhex("0123456789abcdef0123456789abcdef")
_UNIX_TIME: Final[int] = 1756000000
_X25519_GROUP: Final[int] = 0x001D
_ML_KEM_768_X25519_GROUP: Final[int] = 0x11EC
_CURVE25519_PRIME: Final[int] = 2**255 - 19
_CURVE25519_KEY_SIZE: Final[int] = 32
_ML_KEM_768_KEY_SIZE: Final[int] = 1184


# The prime order of the Curve25519 base point. The full curve is eight times it.
#  https://www.rfc-editor.org/rfc/rfc7748#section-4.1
_CURVE25519_ORDER: Final[int] = 2**252 + 27742317777372353535851937790883648493


# The extension types the ClientHello carries, whatever order the permutation
#  puts them in. Read off TDLib's op list, so a dropped extension shows up here.
#  https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/td/mtproto/TlsInit.cpp#L211-L230
_EXPECTED_EXTENSIONS: Final[frozenset[int]] = frozenset(
    {
        0x0000,
        0x0005,
        0x000A,
        0x000B,
        0x000D,
        0x0010,
        0x0012,
        0x0017,
        0x001B,
        0x0023,
        0x002B,
        0x002D,
        0x0033,
        0x44CD,
        0xFE0D,
        0xFF01,
    }
)


def _is_grease(extension_type: int) -> bool:
    high, low = extension_type >> 8, extension_type & 0xFF

    return high == low and low & 0x0F == 0x0A


def _parse_extensions(record: bytes) -> dict[int, bytes]:
    """Every length field in the greeting, checked on the way to the extensions."""
    assert record[:3] == b"\x16\x03\x01"

    record_length = int.from_bytes(record[3:5], "big")
    assert len(record) == 5 + record_length

    assert record[5] == 0x01
    handshake_length = int.from_bytes(record[6:9], "big")
    assert len(record) == 9 + handshake_length

    assert record[9:11] == b"\x03\x03"

    cursor = _RANDOM_OFFSET + _RANDOM_SIZE
    cursor += 1 + record[cursor]  # Legacy session id.
    cursor += 2 + int.from_bytes(record[cursor : cursor + 2], "big")  # Cipher suites.
    cursor += 1 + record[cursor]  # Compression methods.

    extensions_length = int.from_bytes(record[cursor : cursor + 2], "big")
    cursor += 2
    assert cursor + extensions_length == len(record)

    extensions: dict[int, bytes] = {}

    while cursor < len(record):
        extension_type = int.from_bytes(record[cursor : cursor + 2], "big")
        body_length = int.from_bytes(record[cursor + 2 : cursor + 4], "big")
        cursor += 4

        assert extension_type not in extensions, f"extension {extension_type:#06x} written twice"
        extensions[extension_type] = record[cursor : cursor + body_length]
        cursor += body_length

    assert cursor == len(record)

    return extensions


def _build_client_hello() -> faketls.FakeTlsHello:
    return faketls.build_client_hello(domain=SNI_DOMAIN, secret=_SECRET, unix_time=_UNIX_TIME)


def test_client_hello_length_fields_all_agree() -> None:
    _parse_extensions(_build_client_hello().record)


def test_client_hello_names_the_domain_in_its_sni_extension() -> None:
    server_name = _parse_extensions(_build_client_hello().record)[0x0000]

    # `ServerNameList` length, one `ServerName` of type 0, then the host length.
    assert server_name[5:] == SNI_DOMAIN.encode("ascii")


def test_client_hello_carries_every_extension_whatever_the_permutation_does() -> None:
    # The extension order is randomized per greeting, so this runs enough times
    #  that a dropped or duplicated block cannot hide behind one lucky ordering.
    for _ in range(16):
        present = _parse_extensions(_build_client_hello().record)
        grease = {extension for extension in present if _is_grease(extension)}

        # Two more extensions carry a GREASE value as their type, so their number
        #  is not fixed and they are counted rather than named.
        assert len(grease) == 2
        assert frozenset(present) - grease == _EXPECTED_EXTENSIONS


def test_client_hello_extension_order_actually_varies() -> None:
    # GREASE types are drawn fresh per greeting, so an order that kept them
    #  would vary on those values alone and still pass with the permutation
    #  removed entirely.
    orders = {
        tuple(
            extension
            for extension in _parse_extensions(_build_client_hello().record)
            if not _is_grease(extension)
        )
        for _ in range(16)
    }

    assert len(orders) > 1


def test_ech_payload_length_is_one_of_the_four_tdlib_draws_between() -> None:
    # The four lengths TDLib's `Op::ech_payload()` picks between, written out as
    #  `Random::fast(0, 3) * 32 + 144` gives them.
    #  https://github.com/tdlib/td/blob/d1085f9cebc5a62379991ae1652673954f229c1f/td/mtproto/TlsInit.cpp#L126-L131
    assert faketls._ECH_PAYLOAD_SIZE in {144, 176, 208, 240}


def test_client_hello_length_is_the_same_for_every_greeting() -> None:
    # The ECH payload is the only part of the hello whose length is drawn at all,
    #  and TDLib draws it once for the process - so a length that changed from one
    #  connection to the next would be a fingerprint no browser produces.
    lengths = {len(_build_client_hello().record) for _ in range(16)}

    assert len(lengths) == 1


def test_client_hello_random_is_the_secret_hmac_with_the_clock_folded_in() -> None:
    hello = _build_client_hello()

    zeroed = (
        hello.record[:_RANDOM_OFFSET]
        + bytes(_RANDOM_SIZE)
        + hello.record[_RANDOM_OFFSET + _RANDOM_SIZE :]
    )
    digest = bytearray(hmac.new(_SECRET, zeroed, hashlib.sha256).digest())
    timestamp = _UNIX_TIME.to_bytes(_TIMESTAMP_SIZE, "little")

    for index in range(_TIMESTAMP_SIZE):
        digest[_RANDOM_SIZE - _TIMESTAMP_SIZE + index] ^= timestamp[index]

    assert hello.random == bytes(digest)
    assert hello.record[_RANDOM_OFFSET : _RANDOM_OFFSET + _RANDOM_SIZE] == bytes(digest)


def _server_hello_for(client_random: bytes, *, secret: bytes) -> bytes:
    """What a proxy holding `secret` answers with, per TDLib's own check."""
    body = b"\x16\x03\x03\x00\x50\x02\x00\x00\x4c\x03\x03" + bytes(_RANDOM_SIZE) + b"\x00" * 25
    digest = hmac.new(secret, client_random + body, hashlib.sha256).digest()

    return body[:_RANDOM_OFFSET] + digest + body[_RANDOM_OFFSET + _RANDOM_SIZE :]


def test_server_hello_is_authentic_accepts_a_reply_built_with_the_secret() -> None:
    hello = _build_client_hello()
    response = _server_hello_for(hello.random, secret=_SECRET)

    assert faketls.server_hello_is_authentic(response, secret=_SECRET, client_random=hello.random)


def test_server_hello_is_authentic_rejects_a_reply_built_with_another_secret() -> None:
    hello = _build_client_hello()
    response = _server_hello_for(hello.random, secret=bytes(16))

    assert not faketls.server_hello_is_authentic(
        response, secret=_SECRET, client_random=hello.random
    )


def test_server_hello_is_authentic_rejects_a_tampered_reply() -> None:
    hello = _build_client_hello()
    response = bytearray(_server_hello_for(hello.random, secret=_SECRET))
    response[-1] ^= 0xFF

    assert not faketls.server_hello_is_authentic(
        bytes(response), secret=_SECRET, client_random=hello.random
    )


class _KeyShareEntry(NamedTuple):
    group: int
    key: bytes


def _key_share_entries(record: bytes) -> list[_KeyShareEntry]:
    body = _parse_extensions(record)[0x0033]  # the key_share extension
    assert int.from_bytes(body[:2], "big") == len(body) - 2

    entries: list[_KeyShareEntry] = []
    cursor = 2

    while cursor < len(body):
        group = int.from_bytes(body[cursor : cursor + 2], "big")
        key_length = int.from_bytes(body[cursor + 2 : cursor + 4], "big")
        cursor += 4

        entries.append(_KeyShareEntry(group=group, key=body[cursor : cursor + key_length]))
        cursor += key_length

    assert cursor == len(body)

    return entries


def _is_on_curve25519(key: bytes) -> bool:
    x = int.from_bytes(key, "little")
    # 486662 is the curve's `A`.
    y_squared = ((x + 486662) * x + 1) * x % _CURVE25519_PRIME

    return pow(y_squared, (_CURVE25519_PRIME - 1) // 2, _CURVE25519_PRIME) == 1


def _is_in_prime_order_subgroup(key: bytes) -> bool:
    """Whether the order of the point at `key` divides the base point's.

    The x-only Montgomery ladder of RFC 7748 section 5, run with the group order
    as the scalar: the identity is the only point whose projective z is zero.
    """
    prime = _CURVE25519_PRIME
    x_1 = int.from_bytes(key, "little")

    x_2, z_2, x_3, z_3 = 1, 0, x_1, 1
    swap = 0

    for bit_index in reversed(range(_CURVE25519_ORDER.bit_length())):
        bit = (_CURVE25519_ORDER >> bit_index) & 1

        if swap ^ bit:
            x_2, x_3, z_2, z_3 = x_3, x_2, z_3, z_2

        swap = bit

        a_sum = (x_2 + z_2) % prime
        b_difference = (x_2 - z_2) % prime
        a_squared = a_sum * a_sum % prime
        b_squared = b_difference * b_difference % prime
        e_difference = (a_squared - b_squared) % prime

        da = (x_3 - z_3) * a_sum % prime
        cb = (x_3 + z_3) * b_difference % prime

        x_3 = pow(da + cb, 2, prime)
        z_3 = x_1 * pow(da - cb, 2, prime) % prime
        x_2 = a_squared * b_squared % prime
        # 121665 is `a24`, the ladder constant of RFC 7748 section 4.1.
        z_2 = e_difference * (a_squared + 121665 * e_difference) % prime

    if swap:
        z_2 = z_3

    return z_2 == 0


def test_key_share_offers_x25519_alone_and_paired_with_ml_kem_768() -> None:
    entries = _key_share_entries(_build_client_hello().record)
    by_group = {entry.group: entry.key for entry in entries}

    assert len(by_group[_X25519_GROUP]) == _CURVE25519_KEY_SIZE
    assert len(by_group[_ML_KEM_768_X25519_GROUP]) == _ML_KEM_768_KEY_SIZE + _CURVE25519_KEY_SIZE


def test_key_share_keys_look_like_real_x25519_public_keys() -> None:
    # A real key is a point on the curve and in the base point's subgroup; a
    #  point on the twist, or one of full order, is exactly what a fingerprinter
    #  looks for. Both properties are per-key random, so this repeats.
    for _ in range(4):
        by_group = {
            entry.group: entry.key for entry in _key_share_entries(_build_client_hello().record)
        }
        keys = (by_group[_X25519_GROUP], by_group[_ML_KEM_768_X25519_GROUP][_ML_KEM_768_KEY_SIZE:])

        for key in keys:
            assert _is_on_curve25519(key)
            assert _is_in_prime_order_subgroup(key)


def test_client_hello_needs_no_padding_extension() -> None:
    # The padding op only fires below offset 513, and the ML-KEM key alone is 1184
    #  bytes - so the extension TDLib still lists is never reached in practice. The
    #  op stays because dropping it would make this file diverge from its source.
    assert 0x0015 not in _parse_extensions(_build_client_hello().record)


# Stands in for the obfuscated2 header, which is what the transport hands over as
#  the prologue.
_PROLOGUE: Final[bytes] = bytes(range(64))


class _Wire:
    """Serves `read_exactly` out of a fixed buffer, then reports the end as `None`."""

    def __init__(self, data: bytes) -> None:
        self._data = data
        self._offset = 0

    async def __call__(self, length: int) -> bytes | None:
        if self._offset + length > len(self._data):
            return None

        chunk = self._data[self._offset : self._offset + length]
        self._offset += length

        return chunk


def _records(payload: bytes) -> FakeTlsRecords:
    return FakeTlsRecords(_Wire(payload), prologue=b"")


def _record(payload: bytes) -> bytes:
    return APPLICATION_DATA_PREFIX + len(payload).to_bytes(RECORD_LENGTH_SIZE, "big") + payload


def _record_payloads(wire: bytes) -> list[bytes]:
    payloads: list[bytes] = []
    offset = 0

    while offset < len(wire):
        assert wire[offset : offset + len(APPLICATION_DATA_PREFIX)] == APPLICATION_DATA_PREFIX

        length = int.from_bytes(
            wire[offset + len(APPLICATION_DATA_PREFIX) : offset + RECORD_HEADER_SIZE], "big"
        )
        payloads.append(wire[offset + RECORD_HEADER_SIZE : offset + RECORD_HEADER_SIZE + length])
        offset += RECORD_HEADER_SIZE + length

    return payloads


def test_wrap_sends_one_change_cipher_spec_and_never_a_second() -> None:
    records = _records(b"")

    first = records.wrap(b"one")
    second = records.wrap(b"two")

    assert first.startswith(CHANGE_CIPHER_SPEC)
    assert CHANGE_CIPHER_SPEC not in second


def test_wrap_puts_the_prologue_in_front_of_the_first_payload_only() -> None:
    records = FakeTlsRecords(_Wire(b""), prologue=_PROLOGUE)

    first = records.wrap(b"one")
    second = records.wrap(b"two")

    assert _record_payloads(first[len(CHANGE_CIPHER_SPEC) :]) == [_PROLOGUE + b"one"]
    assert _record_payloads(second) == [b"two"]


def test_wrap_cuts_a_payload_no_single_record_can_hold() -> None:
    # One byte past what a single 2-byte length field can describe. The whole
    #  payload used to go into one record, so this size raised `struct.error`.
    payload = bytes(0x10000 + 1)
    records = _records(b"")

    payloads = _record_payloads(records.wrap(payload)[len(CHANGE_CIPHER_SPEC) :])

    assert len(payloads) > 1
    assert max(len(one) for one in payloads) <= _MAX_RECORD_PAYLOAD
    assert b"".join(payloads) == payload


async def test_recv_joins_records_into_the_requested_length() -> None:
    records = _records(_record(b"abc") + _record(b"de") + _record(b"f"))

    assert await records.recv(6) == b"abcdef"


async def test_recv_serves_a_short_read_out_of_the_buffered_record() -> None:
    records = _records(_record(b"abcdef"))

    assert await records.recv(2) == b"ab"
    assert await records.recv(4) == b"cdef"


async def test_recv_reports_a_stream_that_ended_as_none() -> None:
    records = _records(_record(b"abc"))

    assert await records.recv(4) is None


async def test_recv_rejects_a_record_that_is_not_application_data() -> None:
    handshake_record = b"\x16\x03\x03" + (3).to_bytes(RECORD_LENGTH_SIZE, "big") + b"abc"
    records = _records(handshake_record)

    with pytest.raises(OSError, match="application-data"):
        await records.recv(3)


@pytest.fixture(autouse=True)
def short_timeouts(monkeypatch):
    monkeypatch.setattr(TCP, "TIMEOUT", 2)
    monkeypatch.setattr(TCP, "CONNECT_TIMEOUT", 2)


async def _start_echo_server():
    async def serve(reader, writer):
        try:
            # The transport tag the framing class sends once, which a real server
            #  consumes and never echoes back.
            await reader.readexactly(1)

            while True:
                chunk = await reader.read(4096)

                if not chunk:
                    return

                writer.write(chunk)
                await writer.drain()
        except asyncio.IncompleteReadError:
            return
        finally:
            writer.close()

    server = await asyncio.start_server(serve, host="127.0.0.1", port=0)

    return server, server.sockets[0].getsockname()[1]


async def _pipe(reader, writer):
    try:
        while True:
            chunk = await reader.read(4096)

            if not chunk:
                return

            writer.write(chunk)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError):
        return


async def _relay(client_reader, client_writer, upstream_reader, upstream_writer):
    # Both halves stop as soon as either does: leaving one waiting would keep the
    #  upstream connection open, and its own server would then never close.
    tasks = [
        asyncio.create_task(_pipe(client_reader, upstream_writer)),
        asyncio.create_task(_pipe(upstream_reader, client_writer)),
    ]

    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()

        upstream_writer.close()


async def _start_socks5_server(target_port: int, *, username=None, password=None):
    seen = {}

    async def serve(reader, writer):
        try:
            version, count = await reader.readexactly(2)
            methods = await reader.readexactly(count)
            seen["methods"] = methods

            assert version == 5

            if username is not None:
                assert 0x02 in methods
                writer.write(b"\x05\x02")
                await writer.drain()

                assert await reader.readexactly(1) == b"\x01"
                user = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
                secret = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
                seen["credentials"] = (user, secret)

                writer.write(b"\x01\x00")
                await writer.drain()
            else:
                writer.write(b"\x05\x00")
                await writer.drain()

            head = await reader.readexactly(4)
            assert head[:2] == b"\x05\x01"

            address_type = head[3]

            if address_type == 0x01:
                host = socket.inet_ntoa(await reader.readexactly(4))
            elif address_type == 0x03:
                host = (await reader.readexactly((await reader.readexactly(1))[0])).decode()
            else:
                raise AssertionError(f"unexpected address type {address_type}")

            port = int.from_bytes(await reader.readexactly(2), "big")
            seen["destination"] = (host, port)

            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", target_port
            )

            writer.write(b"\x05\x00\x00\x01" + b"\x00" * 4 + (0).to_bytes(2, "big"))
            await writer.drain()

            await _relay(reader, writer, upstream_reader, upstream_writer)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, host="127.0.0.1", port=0)

    return server, server.sockets[0].getsockname()[1], seen


async def _start_http_connect_server(target_port: int):
    seen = {}

    async def serve(reader, writer):
        try:
            request = await reader.readuntil(b"\r\n\r\n")
            seen["request"] = request

            assert request.startswith(b"CONNECT ")

            upstream_reader, upstream_writer = await asyncio.open_connection(
                "127.0.0.1", target_port
            )

            writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await writer.drain()

            await _relay(reader, writer, upstream_reader, upstream_writer)
        finally:
            writer.close()

    server = await asyncio.start_server(serve, host="127.0.0.1", port=0)

    return server, server.sockets[0].getsockname()[1], seen


async def _round_trip(transport, destination):
    payload = b"A" * 64

    await transport.connect(destination)

    try:
        await transport.send(payload)

        return await transport.recv()
    finally:
        await transport.close()


async def _close(*servers):
    for server in servers:
        server.close()
        await server.wait_closed()


async def test_a_direct_connection_round_trips_an_abridged_frame():
    echo, echo_port = await _start_echo_server()
    transport = TCPAbridged()

    try:
        assert await _round_trip(transport, ("127.0.0.1", echo_port)) == b"A" * 64
    finally:
        await _close(echo)


async def test_a_socks5_proxy_carries_the_connection_to_the_destination():
    echo, echo_port = await _start_echo_server()
    proxy_server, proxy_port, seen = await _start_socks5_server(echo_port)

    transport = TCPAbridged(proxy=SOCKS5Proxy(hostname="127.0.0.1", port=proxy_port))

    try:
        assert await _round_trip(transport, ("127.0.0.1", echo_port)) == b"A" * 64
    finally:
        await _close(proxy_server, echo)

    assert seen["destination"] == ("127.0.0.1", echo_port), (
        "the proxy must be asked for the DC address, not for its own"
    )


async def test_a_socks5_proxy_is_given_credentials_that_a_url_would_mangle():
    echo, echo_port = await _start_echo_server()
    proxy_server, proxy_port, seen = await _start_socks5_server(
        echo_port, username="us:er", password="p@ss%1"
    )

    transport = TCPAbridged(
        proxy=SOCKS5Proxy(
            hostname="127.0.0.1",
            port=proxy_port,
            username="us:er",
            password="p@ss%1"
        )
    )

    try:
        assert await _round_trip(transport, ("127.0.0.1", echo_port)) == b"A" * 64
    finally:
        await _close(proxy_server, echo)

    assert seen["credentials"] == ("us:er", "p@ss%1")


async def test_an_http_proxy_tunnels_the_connection_with_connect():
    echo, echo_port = await _start_echo_server()
    proxy_server, proxy_port, seen = await _start_http_connect_server(echo_port)

    transport = TCPAbridged(proxy=HTTPProxy(hostname="127.0.0.1", port=proxy_port))

    try:
        assert await _round_trip(transport, ("127.0.0.1", echo_port)) == b"A" * 64
    finally:
        await _close(proxy_server, echo)

    assert f"127.0.0.1:{echo_port}".encode() in seen["request"]


async def test_a_refused_proxy_is_reported_as_an_os_error():
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        dead_port = probe.getsockname()[1]

    transport = TCPAbridged(proxy=SOCKS5Proxy(hostname="127.0.0.1", port=dead_port))

    with pytest.raises(OSError):
        await transport.connect(("127.0.0.1", 443))

    await transport.close()


def frame(payload: bytes) -> bytes:
    return bytes([len(payload) // 4]) + payload


class ScriptedReader:
    def __init__(self, *parts):
        self.parts = list(parts)
        self.buf = b""

    async def read(self, n):
        while not self.buf:
            if not self.parts:
                return b""
            part = self.parts.pop(0)
            if part == "STALL":
                await asyncio.sleep(3600)
            self.buf = part
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


@pytest.fixture
def quick_timeout(monkeypatch):
    monkeypatch.setattr(TCP, "TIMEOUT", 0.05)


def make_protocol(reader):
    protocol = TCPAbridged(False, None)
    protocol.reader = reader
    return protocol


async def test_a_stall_before_any_byte_stays_recoverable(quick_timeout):
    protocol = make_protocol(ScriptedReader("STALL", frame(b"A" * 8)))

    with pytest.raises(TimeoutError):
        await protocol.recv()


async def test_a_stall_mid_message_is_reported_as_a_broken_connection(quick_timeout):
    body = b"A" * 64
    head = frame(body)
    protocol = make_protocol(ScriptedReader(head[:33], "STALL", head[33:]))

    with pytest.raises(OSError) as exc:
        await protocol.recv()

    assert not isinstance(exc.value, TimeoutError)


async def test_a_stall_after_the_extended_length_marker_is_broken_too(quick_timeout):
    protocol = make_protocol(ScriptedReader(b"\x7f", "STALL", b"\x01\x00\x00"))

    with pytest.raises(OSError) as exc:
        await protocol.recv()

    assert not isinstance(exc.value, TimeoutError)


class FakeConn:
    def __init__(self, *packets):
        self.packets = list(packets)
        self.protocol = type("P", (), {"crypto_executor": None})()

    async def recv(self):
        if not self.packets:
            return None
        return self.packets.pop(0)

    async def close(self):
        pass


class DummyClient:
    name = "framing"
    app_version = "1.0"
    device_model = "T"
    system_version = "L"
    lang_code = "en"
    proxy = None
    ipv6 = False
    session = None
    disconnect_handler = None


def make_session(conn):
    session = Session(DummyClient(), 2, b"k" * 256, False)
    session.connection = conn
    session.loop = asyncio.get_running_loop()
    return session


async def test_an_empty_packet_ends_the_read_loop():
    session = make_session(FakeConn(b""))
    handled = []
    session._handle_packet_wrapper = lambda p: handled.append(p)

    await asyncio.wait_for(session.recv_worker(), 5)

    assert handled == []


async def test_an_undecryptable_packet_does_not_look_like_a_live_connection():
    session = make_session(FakeConn(b"garbage-that-cannot-decrypt", None))

    await asyncio.wait_for(session.recv_worker(), 5)
    await asyncio.sleep(0)

    assert session.last_packet_received == 0.0


class _ObfuscatedLike(TCP):
    """The framing every non-abridged transport shares: a fixed-size length
    prefix, then a body read that used to escape as a plain TimeoutError."""

    async def recv(self, length: int = 0):
        prefix = await super().recv(4)

        if prefix is None:
            return None

        return await super().recv(int.from_bytes(prefix, "little"))


def make_raw_protocol(reader):
    protocol = _ObfuscatedLike(False, None)
    protocol.reader = reader
    return protocol


async def test_every_transport_reports_a_mid_message_stall_as_broken(quick_timeout):
    protocol = make_raw_protocol(
        ScriptedReader((64).to_bytes(4, "little") + b"A" * 32, "STALL", b"A" * 32)
    )

    with pytest.raises(OSError) as exc:
        await protocol.recv()

    assert not isinstance(exc.value, TimeoutError), (
        "a stall after the length prefix leaves the stream desynchronised; "
        "reporting it as a timeout makes recv_worker resume mid-packet"
    )


async def test_a_stall_between_the_prefix_and_the_body_is_broken_too(quick_timeout):
    protocol = make_raw_protocol(
        ScriptedReader((64).to_bytes(4, "little"), "STALL", b"A" * 64)
    )

    with pytest.raises(OSError) as exc:
        await protocol.recv()

    assert not isinstance(exc.value, TimeoutError), (
        "the length prefix is already consumed, so the connection cannot be resumed"
    )


async def test_an_idle_socket_between_messages_stays_recoverable(quick_timeout):
    protocol = make_raw_protocol(
        ScriptedReader((4).to_bytes(4, "little") + b"AAAA", "STALL")
    )

    assert await protocol.recv() == b"AAAA"

    with pytest.raises(TimeoutError):
        await Connection.__dict__["recv"](
            SimpleNamespace(protocol=protocol)
        )


async def test_the_message_boundary_is_reset_between_messages(quick_timeout):
    protocol = make_raw_protocol(
        ScriptedReader((4).to_bytes(4, "little") + b"AAAA", "STALL")
    )
    conn = SimpleNamespace(protocol=protocol)

    assert await Connection.__dict__["recv"](conn) == b"AAAA"

    with pytest.raises(TimeoutError):
        await Connection.__dict__["recv"](conn)


def padded_intermediate_frame(payload: bytes, padding: int) -> bytes:
    body = payload + bytes(padding)
    return len(body).to_bytes(4, "little") + body


def make_padded_intermediate(reader):
    protocol = TCPPaddedIntermediate(False, None)
    protocol.reader = reader
    return protocol


@pytest.mark.parametrize("padding", range(16))
async def test_a_transport_error_survives_padded_intermediate_framing(padding, quick_timeout):
    error = (-404).to_bytes(4, "little", signed=True)
    protocol = make_padded_intermediate(
        ScriptedReader(padded_intermediate_frame(error, padding))
    )

    packet = await protocol.recv()

    assert packet == error
    assert transport_error(packet) == "Server sent transport error: 404 (auth key not found)"


@pytest.mark.parametrize("padding", range(16))
async def test_padded_intermediate_still_strips_padding_off_a_message(padding, quick_timeout):
    message = bytes(range(40))
    protocol = make_padded_intermediate(
        ScriptedReader(padded_intermediate_frame(message, padding))
    )

    assert await protocol.recv() == message


class FakeSession:
    def __init__(self, *args, **kwargs):
        self.auth_key = args[2]
        self.server_address = kwargs.get("server_address")
        self.port = kwargs.get("port")

    async def start(self, *args, **kwargs):
        pass

    async def stop(self):
        pass


class FakeStorage:
    def __init__(self, **values):
        self.values = {
            "dc_id": 2,
            "server_address": "10.0.0.2",
            "port": 443,
            "auth_key": b"k" * 256,
            "test_mode": False,
            "user_id": 7,
            **values,
        }

    def __getattr__(self, name):
        async def accessor(value=object):
            if value is object:
                return self.values[name]
            self.values[name] = value
            return value

        return accessor


class FakeClient(Connect, SendCode, SignInBot):
    def __init__(self, ipv6=False, migrate=None):
        self.is_connected = False
        self.ipv6 = ipv6
        self.crypto_executor = None
        self.storage = FakeStorage()
        self.session = FakeSession(self, 2, b"old", False)
        self.migrate = migrate
        self.api_id = 1
        self.api_hash = "h"
        self.requested_dc_options = []

    async def load_session(self):
        pass

    async def get_dc_option(self, dc_id, is_media=False, is_cdn=False, ipv6=False):
        self.requested_dc_options.append(dc_id)
        return SimpleNamespace(ip_address=f"10.0.0.{dc_id}", port=443)

    async def get_session(self, dc_id=None, server_address=None, port=None, **kwargs):
        return FakeSession(
            self, dc_id, b"migrated-key", False,
            server_address=server_address, port=port,
        )

    async def invoke(self, query, *args, **kwargs):
        await asyncio.sleep(0)
        if self.migrate is not None:
            exc, self.migrate = self.migrate, None
            raise exc
        return SimpleNamespace(
            user=SimpleNamespace(id=7),
            type=raw.types.auth.SentCodeTypeApp(length=5),
            phone_code_hash="hash",
            next_type=None,
            timeout=None,
        )


@pytest.fixture(autouse=True)
def _stub_session(monkeypatch):
    monkeypatch.setattr(pyrogram.methods.auth.connect, "Session", FakeSession)


async def test_connect_uses_the_stored_address():
    client = FakeClient()
    await client.connect()

    assert client.session.server_address == "10.0.0.2"
    assert client.session.port == 443


async def test_connect_ignores_an_address_of_the_wrong_family():
    client = FakeClient(ipv6=True)
    await client.connect()

    assert client.session.server_address is None
    assert client.session.port is None


async def test_connect_falls_back_when_nothing_is_stored():
    client = FakeClient()
    client.storage.values["server_address"] = None
    client.storage.values["port"] = None
    await client.connect()

    assert client.session.server_address is None


async def test_send_code_migration_keeps_dc_and_address_in_step():
    client = FakeClient(migrate=PhoneMigrate(value=4))
    await client.send_code("+10000000000")

    assert client.storage.values["dc_id"] == 4
    assert client.storage.values["server_address"] == "10.0.0.4"
    assert client.storage.values["auth_key"] == b"migrated-key"


async def test_sign_in_bot_migration_keeps_dc_and_address_in_step():
    client = FakeClient(migrate=UserMigrate(value=5))
    await client.sign_in_bot("token")

    assert client.storage.values["dc_id"] == 5
    assert client.storage.values["server_address"] == "10.0.0.5"
    assert client.storage.values["auth_key"] == b"migrated-key"

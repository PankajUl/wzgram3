import asyncio
import logging
import os
import struct
import threading
import time
import zlib
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from unittest import mock

import pytest

import pyrogram
import pyrogram.client
from pyrogram import raw
from pyrogram.storage import (
    Storage,
    SQLiteStorage,
    sqlite_storage,
    HybridStorage,
    RemoteStorage,
    MongoStorage,
    RedisStorage,
)
from pyrogram.storage.caching import PeerRowCache, SessionAttrCache
from pyrogram.storage.hybrid_storage import PEER_WRITE, SESSION_WRITE
from pyrogram.storage.memory_storage import MemoryStorage
from pyrogram.storage.sqlite_storage import PROD
from pyrogram.storage.storage import WZ_PREFIX
from pyrogram.utils import ainput


class TestStorageABC:
    def test_storage_is_abstract(self):
        with pytest.raises(TypeError):
            Storage()  # abstract – can't instantiate directly

    def test_storage_has_abstract_methods(self):
        methods = [
            "open", "save", "close", "delete",
            "update_peers", "update_usernames", "update_state",
            "get_peer_by_id", "get_peer_by_username",
            "get_peer_by_phone_number",
            "dc_id", "api_id", "server_address", "port",
            "test_mode", "auth_key", "date", "user_id", "is_bot",
        ]
        for m in methods:
            assert hasattr(Storage, m), f"Storage missing abstract method: {m}"

    def test_storage_constants(self):
        assert Storage.V2_PACKED_SIZE == 290
        assert Storage.V2_CRC_PACKED_SIZE == 294
        assert Storage.SESSION_STRING_FORMAT_V2 == ">BBI?256sQ?H16s"


class TestMemoryStorage:
    def test_create_minimal(self):
        storage = MemoryStorage(":memory:")
        assert storage.name == ":memory:"
        assert storage.session_string is None

    def test_create_with_session_string(self):
        storage = MemoryStorage(":memory:", session_string="dummy")
        assert storage.session_string == "dummy"

    def test_open_creates_db(self):
        storage = MemoryStorage(":memory:")
        # Before open, conn should be None
        assert storage.conn is None

    @pytest.mark.asyncio
    async def test_open_and_set_values(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        assert storage.conn is not None

        # Set values via the property-style setters
        await storage.dc_id(2)
        await storage.api_id(12345)
        await storage.test_mode(True)
        await storage.user_id(67890)
        await storage.is_bot(False)
        await storage.auth_key(b"\x00" * 256)
        await storage.date(1000)

        # Read them back (SQLite stores bools as 0/1)
        assert await storage.dc_id() == 2
        assert await storage.api_id() == 12345
        assert await storage.test_mode() == 1
        assert await storage.user_id() == 67890
        assert await storage.is_bot() == 0
        assert await storage.auth_key() == b"\x00" * 256
        assert await storage.date() == 1000

        await storage.save()
        await storage.close()

    @pytest.mark.asyncio
    async def test_open_twice(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        await storage.open()  # should not raise
        await storage.close()

    @pytest.mark.asyncio
    async def test_delete_noop(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        await storage.delete()  # should not raise
        await storage.close()

    @pytest.mark.asyncio
    async def test_update_peers(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        await storage.update_peers([(123, 456, "user", "+1234567890")])

        peer = await storage.get_peer_by_id(123)
        assert peer is not None
        assert peer.user_id == 123

        await storage.close()

    @pytest.mark.asyncio
    async def test_get_peer_by_username(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        await storage.update_peers([(789, 101112, "user", "")])
        await storage.update_usernames([(789, ["testuser"])])

        peer = await storage.get_peer_by_username("testuser")
        assert peer is not None
        assert peer.user_id == 789

        await storage.close()

    @pytest.mark.asyncio
    async def test_get_peer_by_phone_number(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        await storage.update_peers([(111, 222, "user", "+111222333")])

        peer = await storage.get_peer_by_phone_number("+111222333")
        assert peer is not None
        assert peer.user_id == 111

        await storage.close()

    @pytest.mark.asyncio
    async def test_peer_not_found_raises(self):
        storage = MemoryStorage(":memory:")
        await storage.open()

        with pytest.raises(KeyError):
            await storage.get_peer_by_id(999999)

        await storage.close()

    @pytest.mark.asyncio
    async def test_update_state(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        state = (0, 1, 2, 3, 4)
        await storage.update_state(state)

        # read back (returns list of tuples)
        state2 = await storage.update_state()
        assert state2 == [state]

        await storage.close()

    @pytest.mark.asyncio
    async def test_export_session_string(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        await storage.dc_id(2)
        await storage.api_id(12345)
        await storage.test_mode(False)
        await storage.auth_key(b"\x11" * 256)
        await storage.user_id(999)
        await storage.is_bot(False)
        await storage.date(0)
        await storage.server_address("149.154.167.50")
        await storage.port(443)

        s = await storage.export_session_string()
        assert isinstance(s, str)
        assert len(s) == 438
        assert s.startswith("WZ_")

        await storage.close()

    @pytest.mark.asyncio
    async def test_export_session_string_survives_an_ipv6_address(self):
        address = "2001:067c:04e8:f004:0000:0000:0000:000b"

        storage = MemoryStorage(":memory:")
        await storage.open()
        await storage.dc_id(4)
        await storage.api_id(12345)
        await storage.test_mode(False)
        await storage.auth_key(b"\x11" * 256)
        await storage.user_id(999)
        await storage.is_bot(False)
        await storage.date(0)
        await storage.server_address(address)
        await storage.port(443)

        s = await storage.export_session_string()
        await storage.close()

        imported = MemoryStorage(":memory:")
        await imported.open()
        await imported.load_session_string(s)

        assert await imported.server_address() == address

        await imported.close()

    @pytest.mark.asyncio
    async def test_server_address_and_port(self):
        storage = MemoryStorage(":memory:")
        await storage.open()
        await storage.server_address("149.154.167.50")
        await storage.port(443)
        assert await storage.server_address() == "149.154.167.50"
        assert await storage.port() == 443
        await storage.close()

    def test_is_subclass_of_sqlite_storage(self):
        assert issubclass(MemoryStorage, SQLiteStorage)


class TestSQLiteStorageMigration:
    @pytest.mark.asyncio
    async def test_stale_address_is_reset_to_match_dc_id(self, tmp_path):
        storage = SQLiteStorage("stale", workdir=tmp_path)
        await storage.open()
        await storage.test_mode(False)
        await storage.auth_key(b"k" * 256)
        await storage.server_address("149.154.167.51")
        await storage.port(443)
        await storage.dc_id(4)
        await storage.version(7)
        await storage.close()

        storage = SQLiteStorage("stale", workdir=tmp_path)
        await storage.open()
        try:
            assert await storage.dc_id() == 4
            assert await storage.server_address() == PROD[4]
            assert await storage.port() == 443
            assert await storage.version() == SQLiteStorage.VERSION
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_migration_skips_an_unknown_dc(self, tmp_path):
        storage = SQLiteStorage("unknown", workdir=tmp_path)
        await storage.open()
        await storage.test_mode(False)
        await storage.server_address("10.0.0.1")
        await storage.dc_id(99)
        await storage.version(7)
        await storage.close()

        storage = SQLiteStorage("unknown", workdir=tmp_path)
        await storage.open()
        try:
            assert await storage.server_address() == "10.0.0.1"
        finally:
            await storage.close()

    @pytest.mark.asyncio
    async def test_migration_from_v6_handles_a_dc_missing_from_the_address_table(self, tmp_path):
        storage = SQLiteStorage("v6", workdir=tmp_path)
        await storage.open()
        await storage.test_mode(True)
        await storage.dc_id(4)
        await storage.conn.execute("ALTER TABLE sessions DROP COLUMN server_address;")
        await storage.conn.execute("ALTER TABLE sessions DROP COLUMN port;")
        await storage.version(6)
        await storage.close()

        storage = SQLiteStorage("v6", workdir=tmp_path)
        await storage.open()
        try:
            assert await storage.version() == SQLiteStorage.VERSION
            assert await storage.server_address() is None
            assert await storage.port() is None
        finally:
            await storage.close()


class TestSQLiteStorageLocking:
    @pytest.mark.asyncio
    async def test_a_platform_without_flock_does_not_warn_about_other_clients(
        self, tmp_path, monkeypatch, caplog
    ):
        monkeypatch.setattr(sqlite_storage, "fcntl", None)

        storage = SQLiteStorage("nolock", workdir=tmp_path)
        await storage.open()
        await storage.close()

        with caplog.at_level(logging.WARNING, logger=sqlite_storage.log.name):
            storage = SQLiteStorage("nolock", workdir=tmp_path)
            await storage.open()
            await storage.close()

        assert caplog.records == []


class TestSQLiteStoragePersistence:
    @pytest.mark.asyncio
    async def test_writes_survive_close_without_save(self, tmp_path):
        storage = SQLiteStorage("persist", workdir=tmp_path)
        await storage.open()
        await storage.update_peers([(123, 456, "user", "+1234567890")])
        await storage.update_usernames([(123, ["bob"])])
        await storage.update_state((1, 2, 3, 4, 5))
        await storage.close()

        storage = SQLiteStorage("persist", workdir=tmp_path)
        await storage.open()
        try:
            assert (await storage.get_peer_by_id(123)).user_id == 123
            assert (await storage.get_peer_by_username("bob")).user_id == 123
            assert (await storage.get_peer_by_phone_number("+1234567890")).user_id == 123
            assert await storage.update_state() == [(1, 2, 3, 4, 5)]
        finally:
            await storage.close()


class TestSQLiteStorageClosedGuards:
    """A stopping client can leave update tasks in flight past ``close()``.

    ``Session.stop()`` cancels the receive and packet tasks but never awaits
    the ``_run_update`` tasks, so they can reach storage after
    ``Client.disconnect()`` has already set ``conn`` to ``None``.
    """

    @staticmethod
    async def _closed_storage(tmp_path):
        storage = SQLiteStorage("closed", workdir=tmp_path)
        await storage.open()
        await storage.update_peers([(123, 456, "user", "+1234567890")])
        await storage.close()
        return storage

    @pytest.mark.asyncio
    async def test_writes_after_close_are_a_no_op(self, tmp_path):
        storage = await self._closed_storage(tmp_path)

        await storage.update_state((1, 100, 0, 1000, 5))
        await storage.update_peers([(321, 654, "user", "+9876543210")])
        await storage.update_usernames([(321, ["bob"])])

    @pytest.mark.asyncio
    async def test_state_read_after_close_returns_no_states(self, tmp_path):
        storage = await self._closed_storage(tmp_path)

        assert await storage.update_state() == []

    @pytest.mark.asyncio
    async def test_uncached_peer_reads_after_close_raise(self, tmp_path):
        storage = await self._closed_storage(tmp_path)

        with pytest.raises(ConnectionError):
            await storage.get_peer_by_id(999)

        with pytest.raises(ConnectionError):
            await storage.get_peer_by_username("bob")

        with pytest.raises(ConnectionError):
            await storage.get_peer_by_phone_number("+1234567890")

    @pytest.mark.asyncio
    async def test_cached_peer_still_resolves_after_close(self, tmp_path):
        storage = await self._closed_storage(tmp_path)

        assert (await storage.get_peer_by_id(123)).user_id == 123
AUTH_KEY = b"K" * 256
USER_ID = 8305084482
NEWLINE = chr(10)


def packed_v2():
    return struct.pack(
        Storage.SESSION_STRING_FORMAT_V2,
        2, 2, 1234, False, AUTH_KEY, USER_ID, True, 0, bytes(16)
    )


def session_string(kind):
    """Every wire format wzgram has ever exported."""
    if kind == "v2_crc":
        body = packed_v2()
        return Storage._encode(body + struct.pack("<I", zlib.crc32(body)))

    if kind == "v2":
        return Storage._encode(packed_v2())

    if kind == "legacy_267":
        return Storage._encode(struct.pack(">B?256sQ?", 2, False, AUTH_KEY, USER_ID, True))

    if kind == "legacy_271":
        return Storage._encode(
            struct.pack(">BI?256sQ?", 2, 1234, False, AUTH_KEY, USER_ID, True)
        )

    raise AssertionError(kind)


class TestSessionStringDecoding:
    """A session string wzgram itself exported has to keep working.

    The prefixed branch used to try only the CRC format and then raise, so every
    string exported before the CRC was added - all of which carry the prefix -
    reported itself as corrupted. Stripping a stray character out of the body
    raised the same flag, so a legacy string that picked up a newline from a
    database column or an env var was unreadable too.
    """

    @pytest.mark.parametrize("kind", ["v2_crc", "v2", "legacy_267", "legacy_271"])
    @pytest.mark.parametrize("wrap", ["bare", "prefixed", "stray_character"])
    def test_every_exported_format_decodes(self, kind, wrap):
        body = session_string(kind)
        candidate = {
            "bare": body,
            "prefixed": WZ_PREFIX + body,
            "stray_character": body[:40] + NEWLINE + body[40:],
        }[wrap]

        assert Storage._decode_session_string(candidate)["user_id"] == USER_ID

    def test_a_truncated_string_is_still_refused(self):
        with pytest.raises(ValueError, match="corrupted"):
            Storage._decode_session_string(WZ_PREFIX + session_string("v2_crc")[:-8])

    def test_a_repair_is_only_trusted_when_a_checksum_confirms_it(self):
        """Repair guesses characters, so only the CRC can vouch for the result.

        Accepting a repaired string with no checksum would hand back an auth key
        assembled from a guess.
        """
        with pytest.raises(ValueError, match="corrupted"):
            Storage._decode_session_string(session_string("v2")[:-8])

    def test_a_dropped_character_is_repaired_when_the_checksum_agrees(self):
        body = session_string("v2_crc")

        assert Storage._decode_session_string(body[:-1])["user_id"] == USER_ID

    def test_an_empty_string_says_so(self):
        with pytest.raises(ValueError, match="empty"):
            Storage._decode_session_string("   ")


class TestSessionStringLoading:
    """A session string has to bring its datacenter address with it.

    Resolving the address was nested inside `if data["api_id"] is not None`, so
    a legacy string - which carries no api_id - kept whatever address create()
    had seeded, which is DC 2's. A DC 4 session then offered a DC 4 auth key to
    DC 2. A v2 string exported before the address was known packs sixteen NUL
    bytes, which is not None, so it wrote an empty address and port 0.
    """

    @pytest.mark.parametrize(
        "kind,expected_api_id",
        [("legacy_dc4", None), ("v2_dc4", 1234), ("v2_dc4_blank_address", 1234)],
    )
    async def test_the_address_always_matches_the_datacenter(self, kind, expected_api_id):
        if kind == "legacy_dc4":
            string = Storage._encode(
                struct.pack(">B?256sQ?", 4, False, AUTH_KEY, USER_ID, True)
            )
        else:
            address = (
                bytes(16) if kind == "v2_dc4_blank_address"
                else PROD[4].encode("ascii").ljust(16, bytes(1))[:16]
            )
            port = 0 if kind == "v2_dc4_blank_address" else 443
            body = struct.pack(
                Storage.SESSION_STRING_FORMAT_V2,
                2, 4, 1234, False, AUTH_KEY, USER_ID, True, port, address
            )
            string = Storage._encode(body + struct.pack("<I", zlib.crc32(body)))

        storage = MemoryStorage("dc4", session_string=string)
        await storage.open()

        assert await storage.dc_id() == 4
        assert await storage.server_address() == PROD[4], (
            "the stored address must belong to the session's own datacenter"
        )
        assert await storage.port() == 443
        assert await storage.api_id() == expected_api_id

    async def test_a_session_with_no_api_id_can_still_be_re_exported(self):
        """The legacy warning asks the user to re-export; that has to be possible.

        api_id was in the required-fields check, so the one format that cannot
        carry an api_id was also the one that could never be re-exported.
        """
        string = Storage._encode(
            struct.pack(">B?256sQ?", 2, False, AUTH_KEY, USER_ID, True)
        )
        storage = MemoryStorage("legacy", session_string=string)
        await storage.open()

        exported = await storage.export_session_string()
        again = Storage._decode_session_string(exported)

        assert again["user_id"] == USER_ID
        assert again["auth_key"] == AUTH_KEY
        assert again["dc_id"] == 2
        assert again["api_id"] == 0, (
            "an unknown api_id round trips as 0, which load_session treats as "
            "absent and backfills from the Client"
        )

    async def test_a_still_incomplete_session_is_refused(self):
        storage = MemoryStorage("empty")
        await storage.open()

        with pytest.raises(ValueError, match="required fields are missing"):
            await storage.export_session_string()


class TestApiIdMigrationPrompt:
    async def test_a_headless_host_is_told_what_to_do_instead_of_spinning(self, monkeypatch):
        """load_session prompts for a missing api_id in a bare `while True`.

        With no terminal, input() raises EOFError on every pass, the broad
        except printed it and looped again: 3123 passes in 1.5s, each spawning a
        thread and writing to stdout, forever.
        """
        string = Storage._encode(
            struct.pack(">B?256sQ?", 2, False, AUTH_KEY, USER_ID, True)
        )
        app = pyrogram.Client(
            "prompt", session_string=string, api_id=None, api_hash=None, in_memory=True
        )
        await app.storage.open()

        asked = []

        async def no_terminal(prompt="", **kwargs):
            asked.append(prompt)

            if len(asked) > 5:
                raise AssertionError("still spinning on a host with no terminal")

            raise EOFError("EOF when reading a line")

        monkeypatch.setattr(pyrogram.client, "ainput", no_terminal)

        with pytest.raises(AttributeError, match="Pass api_id"):
            await asyncio.wait_for(app.load_session(), 10)

        assert len(asked) == 1


class TestPrompts:
    async def test_a_cancelled_prompt_does_not_wedge_the_loop(self):
        """ainput used a `with ThreadPoolExecutor(1)`, whose exit joins the thread.

        A thread parked in input() never returns, so timing out or cancelling a
        prompt hung on executor shutdown instead of unwinding.
        """
        started = threading.Event()
        release = threading.Event()

        def blocking(_prompt):
            started.set()
            release.wait(30)
            return "late"

        with mock.patch("builtins.input", blocking):
            task = asyncio.ensure_future(ainput("prompt: "))

            await asyncio.get_running_loop().run_in_executor(None, started.wait, 10)

            task.cancel()
            start = time.monotonic()

            try:
                with pytest.raises(asyncio.CancelledError):
                    await task
            finally:
                release.set()

            assert time.monotonic() - start < 5, (
                "cancelling the prompt waited on the parked thread instead of "
                "unwinding, so a timed-out prompt wedges the event loop"
            )


# One behavioural suite, run against every storage engine.
#
# Three engines implement the same contract and they drift apart quietly: a peer
# lookup that raises the wrong error, a username TTL enforced in one place and not
# another, an update state that comes back in a different shape. Parametrizing the
# suite is what keeps them honest, and it is why a new engine only has to be added
# to ``ENGINES`` to be held to the same rules.
class FakeRemote(RemoteStorage):
    """A RemoteStorage backed by dicts, standing in for Mongo or Redis."""

    def __init__(self, name: str = "fake", session_string: Optional[str] = None):
        super().__init__(name, session_string=session_string)

        self.session: Optional[Dict[str, Any]] = None
        self.peers: Dict[int, Tuple[int, int, str, Optional[str], int]] = {}
        self.usernames: Dict[str, int] = {}
        self.states: Dict[int, Tuple[int, int, int, int, int]] = {}
        self.stored_version: Optional[int] = None

        self.connected = False
        self.reads = 0
        self.writes = 0
        self.fail_reads = False
        self.fail_writes = False

    async def _connect(self):
        self.connected = True

    async def _disconnect(self):
        self.connected = False

    async def _load_session(self):
        self.reads += 1

        if self.fail_reads:
            raise ConnectionError("backend is down")

        return dict(self.session) if self.session is not None else None

    async def _save_session(self, fields):
        self.writes += 1

        if self.fail_writes:
            raise ConnectionError("backend is down")

        if self.session is None:
            self.session = {}

        self.session.update(fields)

    async def _load_version(self):
        return self.stored_version

    async def _save_version(self, version):
        self.stored_version = version

    async def _upsert_peers(self, rows):
        self.writes += 1

        if self.fail_writes:
            raise ConnectionError("backend is down")

        now = int(time.time())

        for peer_id, access_hash, peer_type, phone_number in rows:
            self.peers[peer_id] = (peer_id, access_hash, peer_type, phone_number, now)

    async def _fetch_peer(self, peer_id):
        self.reads += 1

        if self.fail_reads:
            raise ConnectionError("backend is down")

        stored = self.peers.get(peer_id)

        return None if stored is None else (stored[0], stored[1], stored[2], stored[4])

    async def _fetch_peer_by_username(self, username):
        peer_id = self.usernames.get(username)

        return None if peer_id is None else await self._fetch_peer(peer_id)

    async def _fetch_peer_by_phone(self, phone_number):
        for stored in self.peers.values():
            if stored[3] == phone_number:
                return (stored[0], stored[1], stored[2], stored[4])

        return None

    async def _iter_peers(self, limit=None):
        rows = [(p[0], p[1], p[2], p[3]) for p in self.peers.values()]

        return rows if limit is None else rows[:limit]

    async def _replace_usernames(self, usernames):
        self.writes += 1

        for peer_id, _ in usernames:
            for name in [n for n, pid in self.usernames.items() if pid == peer_id]:
                del self.usernames[name]

        for peer_id, names in usernames:
            for name in names:
                self.usernames[name] = peer_id

    async def _load_states(self):
        return sorted(self.states.values(), key=lambda state: state[3])

    async def _save_state(self, state):
        self.writes += 1
        stored = self.states.get(state[0], (state[0], None, None, None, None))
        self.states[state[0]] = tuple(
            new if new is not None else old for new, old in zip(state, stored)
        )

    async def _delete_state(self, state_id):
        self.writes += 1
        self.states.pop(state_id, None)

    async def _purge(self, remove_peers):
        self.session = None
        self.states.clear()

        if remove_peers:
            self.peers.clear()
            self.usernames.clear()

    def touch_peer(self, peer_id: int, last_update_on: int) -> None:
        stored = self.peers[peer_id]
        self.peers[peer_id] = (*stored[:4], last_update_on)


def make_sqlite(tmp_path: Path):
    return SQLiteStorage("contract", workdir=tmp_path, in_memory=True)


def make_remote(tmp_path: Path):
    return FakeRemote("contract")


def make_hybrid(tmp_path: Path):
    return HybridStorage("contract", backend=FakeRemote("contract"), workdir=tmp_path)
ENGINES = {
    "sqlite": make_sqlite,
    "remote": make_remote,
    "hybrid": make_hybrid,
}


@pytest.fixture(params=list(ENGINES), ids=list(ENGINES))
async def storage(request, tmp_path):
    engine = ENGINES[request.param](tmp_path)

    await engine.open()

    try:
        yield engine
    finally:
        await engine.close()


class TestSessionAttributes:
    async def test_round_trip(self, storage):
        await storage.dc_id(4)
        await storage.api_id(12345)
        await storage.server_address("149.154.167.91")
        await storage.port(443)
        await storage.test_mode(False)
        await storage.auth_key(b"k" * 256)
        await storage.user_id(777000)
        await storage.is_bot(False)

        assert await storage.dc_id() == 4
        assert await storage.api_id() == 12345
        assert await storage.server_address() == "149.154.167.91"
        assert await storage.port() == 443
        assert await storage.test_mode() is False or await storage.test_mode() == 0
        assert await storage.auth_key() == b"k" * 256
        assert await storage.user_id() == 777000

    async def test_unset_attribute_is_none(self, storage):
        assert await storage.user_id() is None

    async def test_save_stamps_date(self, storage):
        await storage.date(0)
        await storage.save()

        assert await storage.date() > 0


class TestPeers:
    async def test_round_trip_user(self, storage):
        await storage.update_peers([(123, 456, "user", None)])

        peer = await storage.get_peer_by_id(123)

        assert isinstance(peer, raw.types.InputPeerUser)
        assert peer.user_id == 123
        assert peer.access_hash == 456

    async def test_group_and_channel(self, storage):
        await storage.update_peers(
            [(-100, 0, "group", None), (-1001234567890, 99, "channel", None)]
        )

        group = await storage.get_peer_by_id(-100)
        channel = await storage.get_peer_by_id(-1001234567890)

        assert isinstance(group, raw.types.InputPeerChat)
        assert isinstance(channel, raw.types.InputPeerChannel)

    async def test_unknown_peer_raises_key_error(self, storage):
        with pytest.raises(KeyError):
            await storage.get_peer_by_id(999)

    async def test_by_phone_number(self, storage):
        await storage.update_peers([(123, 456, "user", "15551234567")])

        peer = await storage.get_peer_by_phone_number("15551234567")

        assert peer.user_id == 123

        with pytest.raises(KeyError):
            await storage.get_peer_by_phone_number("15550000000")

    async def test_by_username(self, storage):
        await storage.update_peers([(123, 456, "user", None)])
        await storage.update_usernames([(123, ["alice"])])

        peer = await storage.get_peer_by_username("alice")

        assert peer.user_id == 123

        with pytest.raises(KeyError):
            await storage.get_peer_by_username("nobody")

    async def test_username_reassignment(self, storage):
        await storage.update_peers([(1, 11, "user", None), (2, 22, "user", None)])
        await storage.update_usernames([(1, ["shared"])])
        await storage.update_usernames([(1, []), (2, ["shared"])])

        peer = await storage.get_peer_by_username("shared")

        assert peer.user_id == 2

    async def test_changed_access_hash_is_written(self, storage):
        await storage.update_peers([(123, 456, "user", None)])
        await storage.update_peers([(123, 789, "user", None)])

        peer = await storage.get_peer_by_id(123)

        assert peer.access_hash == 789

    async def test_empty_batch_is_a_no_op(self, storage):
        await storage.update_peers([])
        await storage.update_usernames([])


class TestUpdateState:
    async def test_upsert_and_read(self, storage):
        await storage.update_state((1, 100, 0, 1600000000, 5))

        states = await storage.update_state()

        assert [tuple(s) for s in states] == [(1, 100, 0, 1600000000, 5)]

    async def test_overwrite_same_id(self, storage):
        await storage.update_state((1, 100, 0, 1600000000, 5))
        await storage.update_state((1, 200, 0, 1600000001, 6))

        states = await storage.update_state()

        assert len(states) == 1
        assert tuple(states[0])[1] == 200

    async def test_a_field_left_out_is_kept(self, storage):
        await storage.update_state((0, 100, 50, 1600000000, 5))
        await storage.update_state((0, 120, None, 1600000001, None))
        await storage.update_state((0, None, 60, None, None))

        assert [tuple(s) for s in await storage.update_state()] == [(0, 120, 60, 1600000001, 5)]

    async def test_delete_by_id(self, storage):
        await storage.update_state((1, 100, 0, 1600000000, 5))
        await storage.update_state(1)

        assert list(await storage.update_state()) == []


class TestSessionString:
    async def test_export_then_load_round_trips(self, storage, tmp_path):
        await storage.dc_id(2)
        await storage.api_id(12345)
        await storage.test_mode(False)
        await storage.auth_key(b"a" * 256)
        await storage.user_id(777000)
        await storage.is_bot(False)
        await storage.server_address("149.154.167.51")
        await storage.port(443)

        exported = await storage.export_session_string()

        target = FakeRemote("target")
        await target.open()

        try:
            await target.load_session_string(exported)

            assert await target.dc_id() == 2
            assert await target.api_id() == 12345
            assert await target.auth_key() == b"a" * 256
            assert await target.user_id() == 777000
            assert await target.server_address() == "149.154.167.51"
        finally:
            await target.close()


class TestClientSelection:
    """An explicit storage_engine used to be silently replaced by SQLite whenever
    a session string was also given, because session_string was checked first."""

    def test_explicit_engine_wins_over_session_string(self, tmp_path):
        import pyrogram

        engine = FakeRemote("explicit")

        client = pyrogram.Client(
            "explicit",
            api_id=12345,
            api_hash="0123456789abcdef0123456789abcdef",
            session_string="WZ_whatever",
            storage_engine=engine,
            workdir=tmp_path,
        )

        assert client.storage is engine
        assert engine.session_string == "WZ_whatever"

    def test_explicit_engine_wins_over_in_memory(self, tmp_path):
        import pyrogram

        engine = FakeRemote("explicit")

        client = pyrogram.Client(
            "explicit",
            api_id=12345,
            api_hash="0123456789abcdef0123456789abcdef",
            in_memory=True,
            storage_engine=engine,
            workdir=tmp_path,
        )

        assert client.storage is engine

    def test_default_is_still_sqlite(self, tmp_path):
        import pyrogram

        client = pyrogram.Client("plain", workdir=tmp_path)

        assert isinstance(client.storage, SQLiteStorage)


# MongoStorage and RedisStorage against fake drivers.
#
# The mapping between wzgram's storage contract and a document or a key layout is
# where these engines break, and it needs no server to check. Integration against
# a real server runs only when WZGRAM_TEST_MONGO_URI / WZGRAM_TEST_REDIS_URI are
# set.
class FakeCollection:
    def __init__(self):
        self.documents = {}
        self.indexes = []

    async def create_index(self, key):
        self.indexes.append(key)

    async def find_one(self, query):
        if "_id" in query:
            return self.documents.get(query["_id"])

        for document in self.documents.values():
            if all(document.get(k) == v for k, v in query.items()):
                return document

        return None

    async def update_one(self, query, update, upsert=False):
        if not update.get("$set"):
            raise ValueError("'$set' is empty. You must specify a field like so: {$set: {<field>: ...}}")

        key = query["_id"]
        document = self.documents.get(key)

        if document is None:
            if not upsert:
                return
            document = {"_id": key}
            self.documents[key] = document

        document.update(update["$set"])

    async def delete_one(self, query):
        self.documents.pop(query.get("_id"), None)

    async def delete_many(self, query):
        if not query:
            self.documents.clear()
            return

        if "peer_id" in query and "$in" in query["peer_id"]:
            wanted = set(query["peer_id"]["$in"])
            for key in [k for k, d in self.documents.items() if d.get("peer_id") in wanted]:
                del self.documents[key]

    def find(self, query):
        documents = list(self.documents.values())

        class Cursor:
            def __aiter__(self):
                self._it = iter(documents)
                return self

            async def __anext__(self):
                try:
                    return next(self._it)
                except StopIteration:
                    raise StopAsyncIteration

        return Cursor()


class FakeDatabase(dict):
    def __missing__(self, key):
        self[key] = FakeCollection()
        return self[key]


class FakeMongoClient(dict):
    def __missing__(self, key):
        self[key] = FakeDatabase()
        return self[key]


@pytest.fixture
async def mongo():
    storage = MongoStorage("driver", FakeMongoClient())

    await storage.open()

    try:
        yield storage
    finally:
        await storage.close()


class TestMongoMapping:
    async def test_session_is_one_document(self, mongo):
        await mongo.dc_id(4)
        await mongo.auth_key(b"k" * 256)

        document = mongo._session.documents[0]

        assert document["_id"] == 0
        assert document["dc_id"] == 4
        assert document["auth_key"] == b"k" * 256

    async def test_peers_are_keyed_by_id(self, mongo):
        await mongo.update_peers([(123, 456, "user", "15551234567")])

        document = mongo._peers.documents[123]

        assert document["access_hash"] == 456
        assert document["type"] == "user"
        assert document["last_update_on"] > 0

        peer = await mongo.get_peer_by_phone_number("15551234567")
        assert peer.user_id == 123

    async def test_usernames_are_keyed_by_name(self, mongo):
        await mongo.update_peers([(123, 456, "user", None)])
        await mongo.update_usernames([(123, ["alice", "alice2"])])

        assert mongo._usernames.documents["alice"]["peer_id"] == 123
        assert (await mongo.get_peer_by_username("alice2")).user_id == 123

    async def test_indexes_created_on_open(self, mongo):
        assert "phone_number" in mongo._peers.indexes
        assert "peer_id" in mongo._usernames.indexes

    async def test_a_state_field_left_out_is_kept(self, mongo):
        await mongo.update_state((0, 10, 50, 7, 1))
        await mongo.update_state((0, 11, None, 8, None))

        assert [tuple(s) for s in await mongo.update_state()] == [(0, 11, 50, 8, 1)]

    async def test_a_state_with_nothing_to_store_is_skipped(self, mongo):
        await mongo.update_state((0, None, None, None, None))

        assert list(await mongo.update_state()) == []

    async def test_version_is_recorded(self, mongo):
        assert await mongo.version() == MongoStorage.VERSION

    async def test_pyrofork_import_resolves_the_address(self):
        client = FakeMongoClient()
        session = client["legacy"]["session"]
        session.documents[0] = {"_id": 0, "dc_id": 4, "test_mode": False, "auth_key": b"k" * 256}

        storage = MongoStorage("legacy", client)
        await storage.open()

        try:
            await storage.import_pyrofork()

            assert session.documents[0]["server_address"] == "149.154.167.91"
            assert session.documents[0]["port"] == 443
        finally:
            await storage.close()


class FakePipeline:
    def __init__(self, redis):
        self.redis = redis
        self.calls = []

    def __getattr__(self, name):
        def record(*args, **kwargs):
            self.calls.append((name, args, kwargs))
            return self

        return record

    async def execute(self):
        for name, args, kwargs in self.calls:
            await getattr(self.redis, name)(*args, **kwargs)

        self.calls.clear()


class FakeRedis:
    def __init__(self):
        self.hashes = {}
        self.strings = {}
        self.sets = {}

    def pipeline(self):
        return FakePipeline(self)

    async def config_get(self, key):
        return {"maxmemory-policy": "noeviction"}

    async def hset(self, key, mapping=None, **kwargs):
        if not mapping:
            raise ValueError("'hset' with no key value pairs")

        for value in mapping.values():
            if value is None:
                raise TypeError("Invalid input of type: 'NoneType'. Convert to a bytes, string, int or float first.")

        self.hashes.setdefault(key, {}).update(mapping)

    async def hgetall(self, key):
        return dict(self.hashes.get(key, {}))

    async def set(self, key, value):
        self.strings[key] = value

    async def get(self, key):
        return self.strings.get(key)

    async def delete(self, key):
        self.hashes.pop(key, None)
        self.strings.pop(key, None)
        self.sets.pop(key, None)

    async def sadd(self, key, member):
        self.sets.setdefault(key, set()).add(member)

    async def srem(self, key, member):
        self.sets.get(key, set()).discard(member)

    async def smembers(self, key):
        return set(self.sets.get(key, set()))

    async def aclose(self):
        pass


@pytest.fixture
async def redis():
    storage = RedisStorage("driver", FakeRedis())

    await storage.open()

    try:
        yield storage
    finally:
        await storage.close()


class TestRedisMapping:
    async def test_session_is_one_hash(self, redis):
        await redis.dc_id(4)
        await redis.auth_key(b"k" * 256)
        await redis.is_bot(True)

        stored = redis._redis.hashes["wzgram:driver:session"]

        assert stored["dc_id"] == 4
        assert stored["auth_key"] == b"k" * 256

        assert await redis.dc_id() == 4
        assert await redis.auth_key() == b"k" * 256
        assert await redis.is_bot() is True

    async def test_none_round_trips_as_none(self, redis):
        await redis.api_id(None)

        assert await redis.api_id() is None

    async def test_peer_keys_and_indexes(self, redis):
        await redis.update_peers([(123, 456, "user", "15551234567")])

        assert "wzgram:driver:peer:123" in redis._redis.hashes
        assert 123 in redis._redis.sets["wzgram:driver:peers"]
        assert (await redis.get_peer_by_phone_number("15551234567")).user_id == 123

    async def test_username_reassignment_clears_the_old_key(self, redis):
        await redis.update_peers([(1, 11, "user", None), (2, 22, "user", None)])
        await redis.update_usernames([(1, ["shared"])])
        await redis.update_usernames([(1, []), (2, ["shared"])])

        assert (await redis.get_peer_by_username("shared")).user_id == 2

    async def test_state_round_trip(self, redis):
        await redis.update_state((7, 100, 0, 1600000000, 3))

        assert [tuple(s) for s in await redis.update_state()] == [(7, 100, 0, 1600000000, 3)]

        await redis.update_state(7)

        assert list(await redis.update_state()) == []

    async def test_a_state_field_left_out_is_kept(self, redis):
        await redis.update_state((0, 10, 50, 7, 1))
        await redis.update_state((0, 11, None, 8, None))

        assert [tuple(s) for s in await redis.update_state()] == [(0, 11, 50, 8, 1)]

    async def test_a_state_never_given_a_field_reads_it_as_none(self, redis):
        await redis.update_state((0, None, 50, 7, None))

        assert [tuple(s) for s in await redis.update_state()] == [(0, None, 50, 7, None)]

    async def test_a_state_with_nothing_to_store_is_skipped(self, redis):
        await redis.update_state((0, None, None, None, None))

        assert list(await redis.update_state()) == []

    async def test_purge_removes_everything(self, redis):
        await redis.update_peers([(1, 11, "user", None)])
        await redis.update_usernames([(1, ["alice"])])
        await redis.update_state((7, 1, 0, 1, 1))

        await redis.delete()

        assert redis._redis.hashes.get("wzgram:driver:session") in (None, {})
        assert not redis._redis.sets.get("wzgram:driver:peers")

    async def test_delete_works_after_close(self):
        server = FakeRedis()
        storage = RedisStorage("logout", server)

        await storage.open()
        await storage.auth_key(b"k" * 256)
        await storage.close()

        await storage.delete()

        assert not server.hashes.get("wzgram:logout:session"), "the login survived log_out"
        assert storage._redis is None, "delete must not leave the connection open"

    async def test_eviction_policy_warning(self, caplog):
        class Evicting(FakeRedis):
            async def config_get(self, key):
                return {"maxmemory-policy": "allkeys-lru"}

        storage = RedisStorage("warn", Evicting())

        with caplog.at_level("WARNING"):
            await storage.open()

        await storage.close()

        assert any("lost login" in record.message for record in caplog.records)


@pytest.mark.skipif(
    not os.environ.get("WZGRAM_TEST_MONGO_URI"), reason="WZGRAM_TEST_MONGO_URI not set"
)
class TestMongoIntegration:
    async def test_round_trip_against_a_real_server(self):
        storage = MongoStorage("wzgram_test", os.environ["WZGRAM_TEST_MONGO_URI"])

        await storage.open()

        try:
            await storage.auth_key(b"k" * 256)
            await storage.update_peers([(123, 456, "user", None)])

            assert (await storage.get_peer_by_id(123)).access_hash == 456
        finally:
            await storage.delete()
            await storage.close()


@pytest.mark.skipif(
    not os.environ.get("WZGRAM_TEST_REDIS_URI"), reason="WZGRAM_TEST_REDIS_URI not set"
)
class TestRedisIntegration:
    async def test_round_trip_against_a_real_server(self):
        storage = RedisStorage("wzgram_test", os.environ["WZGRAM_TEST_REDIS_URI"])

        await storage.open()

        try:
            await storage.auth_key(b"k" * 256)
            await storage.update_peers([(123, 456, "user", None)])

            assert (await storage.get_peer_by_id(123)).access_hash == 456
        finally:
            await storage.delete()
            await storage.close()


# HybridStorage: reads stay local, writes reach the backend, and neither a slow
# backend nor a dead one is allowed to become the client's problem.
#
# Each case here fails with its fix reverted - the drop-priority test in
# particular, which is the difference between losing a peer lookup and losing the
# login.
async def drained(storage: HybridStorage) -> None:
    await asyncio.wait_for(storage._queue.join(), timeout=5)


@pytest.fixture
async def hybrid(tmp_path):
    backend = FakeRemote("hybrid")
    storage = HybridStorage("hybrid", backend=backend, workdir=tmp_path, flush_timeout=1)

    await storage.open()

    try:
        yield storage, backend
    finally:
        await storage.close()


class TestReadsStayLocal:
    async def test_peer_read_never_touches_a_broken_backend(self, hybrid):
        storage, backend = hybrid

        await storage.update_peers([(123, 456, "user", None)])
        await drained(storage)

        backend.fail_reads = True

        peer = await storage.get_peer_by_id(123)

        assert peer.user_id == 123

    async def test_session_read_never_touches_a_broken_backend(self, hybrid):
        storage, backend = hybrid

        await storage.dc_id(4)
        backend.fail_reads = True

        assert await storage.dc_id() == 4

    async def test_close_is_bounded_when_the_backend_is_gone(self, tmp_path):
        """A dead backend must not hold shutdown for twice the timeout."""
        backend = FakeRemote("gone")
        storage = HybridStorage("gone", backend=backend, workdir=tmp_path, flush_timeout=0.5)
        storage.RETRY_DELAY = 0.01

        await storage.open()
        backend.fail_writes = True
        await storage.update_peers([(1, 11, "user", None)])

        started = asyncio.get_running_loop().time()
        await storage.close()
        elapsed = asyncio.get_running_loop().time() - started

        assert elapsed < 1.5, f"close() took {elapsed:.1f}s for a 0.5s budget"

    async def test_warm_copies_the_backend_into_the_cache(self, tmp_path):
        backend = FakeRemote("warm")
        await backend.open()
        await backend.dc_id(5)
        await backend.auth_key(b"k" * 256)
        await backend.update_state((7, 100, 0, 1600000000, 3))
        await backend.update_peers([(123, 456, "user", None)])
        await backend.close()

        storage = HybridStorage("warm", backend=backend, workdir=tmp_path)
        await storage.open()

        try:
            backend.fail_reads = True

            assert await storage.dc_id() == 5
            assert await storage.auth_key() == b"k" * 256
            assert [tuple(s) for s in await storage.update_state()] == [
                (7, 100, 0, 1600000000, 3)
            ]
            assert (await storage.get_peer_by_id(123)).access_hash == 456
        finally:
            await storage.close()

    async def test_peers_are_warmed_on_open(self, tmp_path):
        """A client coming up on a new host must not pay an RPC per peer to
        rebuild a cache the backend already holds."""
        backend = FakeRemote("warmpeers")
        await backend.open()
        await backend.update_peers([(i, i * 10, "user", None) for i in range(1, 21)])
        await backend.close()

        storage = HybridStorage("warmpeers", backend=backend, workdir=tmp_path, flush_timeout=1)
        await storage.open()

        try:
            backend.fail_reads = True

            for peer_id in range(1, 21):
                assert (await storage.get_peer_by_id(peer_id)).access_hash == peer_id * 10
        finally:
            await storage.close()

    async def test_warm_limit_is_respected(self, tmp_path):
        backend = FakeRemote("limited")
        await backend.open()
        await backend.update_peers([(i, i * 10, "user", None) for i in range(1, 21)])
        await backend.close()

        storage = HybridStorage(
            "limited", backend=backend, workdir=tmp_path, warm_peers=5, flush_timeout=1
        )
        await storage.open()

        try:
            backend.fail_reads = True

            with pytest.raises(ConnectionError):
                for peer_id in range(1, 21):
                    await storage.get_peer_by_id(peer_id)
        finally:
            await storage.close()

    async def test_a_backend_that_cannot_enumerate_still_opens(self, tmp_path):
        class NoExport(FakeRemote):
            async def _iter_peers(self, limit=None):
                return []

        backend = NoExport("noexport")
        storage = HybridStorage("noexport", backend=backend, workdir=tmp_path, flush_timeout=1)

        await storage.open()

        try:
            await storage.update_peers([(1, 11, "user", None)])
            assert (await storage.get_peer_by_id(1)).access_hash == 11
        finally:
            await storage.close()


class TestWritesReachTheBackend:
    async def test_peers_are_mirrored(self, hybrid):
        storage, backend = hybrid

        await storage.update_peers([(123, 456, "user", None)])
        await drained(storage)

        assert backend.peers[123][:3] == (123, 456, "user")

    async def test_session_attributes_are_mirrored(self, hybrid):
        storage, backend = hybrid

        await storage.auth_key(b"z" * 256)
        await storage.user_id(777000)
        await drained(storage)

        assert backend.session["auth_key"] == b"z" * 256
        assert backend.session["user_id"] == 777000

    async def test_update_state_is_mirrored_and_deletable(self, hybrid):
        storage, backend = hybrid

        await storage.update_state((1, 100, 0, 1600000000, 5))
        await drained(storage)
        assert 1 in backend.states

        await storage.update_state(1)
        await drained(storage)
        assert 1 not in backend.states

    async def test_close_flushes_what_is_queued(self, tmp_path):
        backend = FakeRemote("flush")
        storage = HybridStorage("flush", backend=backend, workdir=tmp_path, flush_timeout=2)

        await storage.open()
        await storage.update_peers([(1, 11, "user", None)])
        await storage.close()

        assert 1 in backend.peers


class TestDelete:
    async def test_a_queued_write_is_not_replayed_after_a_purge(self, tmp_path):
        class Slow(FakeRemote):
            async def _save_session(self, fields):
                await asyncio.sleep(0.2)
                return await super()._save_session(fields)

        backend = Slow("purge")
        storage = HybridStorage("purge", backend=backend, workdir=tmp_path, flush_timeout=1)

        await storage.open()
        await storage.auth_key(b"k" * 256)
        await asyncio.sleep(0)

        await storage.delete()

        assert backend.session is None

        await asyncio.sleep(0.5)

        assert backend.session is None, "a queued write put the session back after the purge"

        await storage.close()


class TestBackendFailures:
    async def test_write_failure_does_not_reach_the_caller(self, hybrid):
        storage, backend = hybrid

        storage.RETRY_DELAY = 0.01
        backend.fail_writes = True

        await storage.update_peers([(1, 11, "user", None)])
        await storage.auth_key(b"k" * 256)

        assert (await storage.get_peer_by_id(1)).user_id == 1

    async def test_writes_resume_after_the_backend_returns(self, tmp_path):
        backend = FakeRemote("retry")
        storage = HybridStorage("retry", backend=backend, workdir=tmp_path, flush_timeout=1)
        storage.RETRY_DELAY = 0.01

        await storage.open()

        try:
            backend.fail_writes = True
            await storage.update_peers([(1, 11, "user", None)])

            await asyncio.sleep(0.05)
            backend.fail_writes = False

            await asyncio.wait_for(storage._queue.join(), timeout=5)

            assert 1 in backend.peers
        finally:
            await storage.close()


class TestBackpressure:
    async def test_queue_is_bounded(self, tmp_path):
        backend = FakeRemote("bounded")
        storage = HybridStorage("bounded", backend=backend, workdir=tmp_path, queue_size=4)

        await storage.open()

        try:
            storage._writer.cancel()

            for peer_id in range(50):
                await storage.update_peers([(peer_id + 1, peer_id, "user", None)])

            assert storage._queue.qsize() <= 4
            assert storage.dropped_writes > 0
        finally:
            storage._closing = True
            await storage.local.close()
            await backend.close()

    async def test_session_writes_are_never_the_ones_dropped(self, tmp_path):
        """Losing a peer costs a lookup; losing an auth key costs the login."""
        backend = FakeRemote("priority")
        storage = HybridStorage("priority", backend=backend, workdir=tmp_path, queue_size=3)

        await storage.open()

        try:
            storage._writer.cancel()

            await storage.auth_key(b"k" * 256)

            for peer_id in range(20):
                await storage.update_peers([(peer_id + 1, peer_id, "user", None)])

            queued = list(storage._queue._queue)
            kinds = [kind for kind, _ in queued]

            assert SESSION_WRITE in kinds, "the session write was dropped under pressure"
            assert kinds.count(PEER_WRITE) < 20
        finally:
            storage._closing = True
            await storage.local.close()
            await backend.close()


class TestWriterTask:
    async def test_writer_task_is_strongly_referenced(self, hybrid):
        """The loop keeps only a weak reference to a task, so fire-and-forget work
        with no other referent can be collected mid-await and simply stop."""
        storage, _ = hybrid

        from pyrogram.utils import _background_tasks

        assert storage._writer in _background_tasks

    async def test_a_write_lost_on_close_is_counted_and_logged(self, tmp_path, caplog):
        class Slow(FakeRemote):
            async def _save_session(self, fields):
                await asyncio.sleep(5)
                return await super()._save_session(fields)

        backend = Slow("lost")
        storage = HybridStorage("lost", backend=backend, workdir=tmp_path, flush_timeout=0.2)

        await storage.open()
        await storage.auth_key(b"k" * 256)
        await asyncio.sleep(0)

        with caplog.at_level("WARNING"):
            await storage.close()

        assert backend.session.get("auth_key") is None, "the write did not land"
        assert storage.dropped_writes == 1
        assert "still had 1 writes queued" in caplog.text
        assert "lost them" in caplog.text

    async def test_a_clean_close_reports_no_loss(self, tmp_path):
        backend = FakeRemote("clean")
        storage = HybridStorage("clean", backend=backend, workdir=tmp_path, flush_timeout=2)

        await storage.open()
        await storage.auth_key(b"k" * 256)
        await storage.close()

        assert backend.session["auth_key"] == b"k" * 256
        assert storage.dropped_writes == 0

    async def test_writer_stops_on_close(self, tmp_path):
        backend = FakeRemote("stop")
        storage = HybridStorage("stop", backend=backend, workdir=tmp_path, flush_timeout=2)

        await storage.open()
        writer = storage._writer
        await storage.close()

        assert writer.done()
        assert storage._writer is None


# RemoteStorage's own rules: the caches, and what they are allowed to skip.
#
# These are the invariants a hand-written engine gets wrong, so they are asserted
# against the base class rather than against any particular backend.
class TestPeerCache:
    async def test_hit_never_reaches_the_backend(self):
        storage = FakeRemote()
        await storage.open()

        await storage.update_peers([(123, 456, "user", None)])
        reads = storage.reads

        await storage.get_peer_by_id(123)
        await storage.get_peer_by_id(123)

        assert storage.reads == reads, "a cached peer must not be fetched again"

        await storage.close()

    async def test_miss_falls_through_and_is_then_cached(self):
        storage = FakeRemote()
        await storage.open()

        storage.peers[999] = (999, 111, "user", None, int(time.time()))
        reads = storage.reads

        await storage.get_peer_by_id(999)
        assert storage.reads == reads + 1

        await storage.get_peer_by_id(999)
        assert storage.reads == reads + 1

        await storage.close()

    async def test_unchanged_peers_are_not_rewritten(self):
        """Every invoke feeds r.users and r.chats back through fetch_peers, so
        without this filter the same peers are rewritten on every single RPC."""
        storage = FakeRemote()
        await storage.open()

        batch = [(1, 11, "user", None), (2, 22, "user", None)]

        await storage.update_peers(batch)
        writes = storage.writes

        await storage.update_peers(batch)

        assert storage.writes == writes, "unchanged peers must not be written again"

        await storage.close()

    async def test_changed_access_hash_is_still_written(self):
        storage = FakeRemote()
        await storage.open()

        await storage.update_peers([(1, 11, "user", None)])
        writes = storage.writes

        await storage.update_peers([(1, 99, "user", None)])

        assert storage.writes == writes + 1
        assert storage.peers[1][1] == 99

        await storage.close()

    async def test_cache_is_bounded(self):
        cache = PeerRowCache(size=3)

        for peer_id in range(10):
            cache.remember((peer_id, peer_id, "user"))

        assert len(cache) == 3
        assert cache.get(9) is not None
        assert cache.get(0) is None

    async def test_reads_keep_a_peer_recent(self):
        cache = PeerRowCache(size=3)

        for peer_id in (1, 2, 3):
            cache.remember((peer_id, 0, "user"), written=True)

        cache.get(1)
        cache.remember((4, 0, "user"), written=True)

        assert cache.get(1) is not None
        assert cache.get(2) is None

        cache.matches(1, 0, "user")
        cache.remember((5, 0, "user"), written=True)

        assert cache.get(1) is not None
        assert cache.get(3) is None

    async def test_cache_holds_rows_not_input_peers(self):
        """Callers hand InputPeers to the API and are free to mutate them, so a
        shared instance would be shared mutable state."""
        storage = FakeRemote()
        await storage.open()

        await storage.update_peers([(123, 456, "user", None)])

        first = await storage.get_peer_by_id(123)
        second = await storage.get_peer_by_id(123)

        assert first is not second
        first.access_hash = 0
        assert second.access_hash == 456

        await storage.close()


class TestUsernameTTL:
    async def test_expired_username_raises(self):
        storage = FakeRemote()
        await storage.open()

        await storage.update_peers([(123, 456, "user", None)])
        await storage.update_usernames([(123, ["alice"])])

        storage.touch_peer(123, int(time.time()) - RemoteStorage.USERNAME_TTL - 60)

        with pytest.raises(KeyError, match="expired"):
            await storage.get_peer_by_username("alice")

        await storage.close()

    async def test_fresh_username_resolves(self):
        storage = FakeRemote()
        await storage.open()

        await storage.update_peers([(123, 456, "user", None)])
        await storage.update_usernames([(123, ["alice"])])

        storage.touch_peer(123, int(time.time()) - 60)

        assert (await storage.get_peer_by_username("alice")).user_id == 123

        await storage.close()


class TestSessionAttrCache:
    async def test_attribute_read_once(self):
        storage = FakeRemote()
        await storage.open()

        reads = storage.reads

        await storage.dc_id()
        await storage.dc_id()
        await storage.dc_id()

        assert storage.reads == reads, "session attributes must be served from memory"

        await storage.close()

    def test_missing_and_none_are_different_states(self):
        cache = SessionAttrCache()

        assert "user_id" not in cache

        cache.remember("user_id", None)

        assert "user_id" in cache
        assert cache.get("user_id") is None

    async def test_write_updates_the_cache(self):
        storage = FakeRemote()
        await storage.open()

        await storage.auth_key(b"k" * 256)
        reads = storage.reads

        assert await storage.auth_key() == b"k" * 256
        assert storage.reads == reads

        await storage.close()


class TestLifecycle:
    async def test_open_seeds_a_default_session(self):
        storage = FakeRemote()
        await storage.open()

        assert storage.session is not None
        assert await storage.dc_id() == 2
        assert storage.stored_version == RemoteStorage.VERSION

        await storage.close()

    async def test_open_is_idempotent(self):
        storage = FakeRemote()
        await storage.open()
        await storage.open()

        assert storage.connected

        await storage.close()

    async def test_reads_after_close_are_refused(self):
        storage = FakeRemote()
        await storage.open()
        await storage.close()

        with pytest.raises(ConnectionError):
            await storage.dc_id()

    async def test_delete_after_close_purges_on_a_live_connection(self):
        seen = []

        class Recording(FakeRemote):
            async def _purge(self, remove_peers):
                seen.append(self.connected)
                await super()._purge(remove_peers)

        storage = Recording()
        await storage.open()
        await storage.auth_key(b"k" * 256)
        await storage.close()

        await storage.delete()

        assert seen == [True], "a purge must run against a live connection"
        assert storage.session is None
        assert not storage.connected, "delete must leave a closed storage closed"

    async def test_delete_keeps_peers_when_asked(self):
        storage = FakeRemote()
        await storage.open()

        await storage.update_peers([(1, 11, "user", None)])
        await storage.delete(remove_peers=False)

        assert storage.peers
        assert storage.session is None

        await storage.close()

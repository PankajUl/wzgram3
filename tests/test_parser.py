import ast
import base64
import inspect
import struct
import sys
from collections import Counter
from pathlib import Path
from unittest.mock import Mock

import pytest

from pyrogram import enums, raw, types, utils
from pyrogram.methods.messages import edit_inline_text as module
from pyrogram.parser import Parser
from pyrogram.parser.markdown import Markdown
from pyrogram.types import MessageEntity


async def parse(text, mode):
    return await Parser(None).parse(text, mode)


def shape(parsed):
    return [
        (type(e).__name__.replace("MessageEntity", "").lower(), e.offset, e.length)
        for e in (parsed["entities"] or [])
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize("source,message,entities", [
    (">a\n>b", "a\nb", [("blockquote", 0, 3)]),
    ("> a\n> b", " a\n b", [("blockquote", 0, 5)]),
    (">  a", "  a", [("blockquote", 0, 3)]),
    ("x\n> a\n> b", "x\n a\n b", [("blockquote", 2, 5)]),
    ("**> a\n> b||", " a\n b", [("blockquote", 0, 5)]),
])
async def test_markdown_keeps_the_space_after_the_quote_marker(source, message, entities):
    parsed = await parse(source, enums.ParseMode.MARKDOWN)

    assert parsed["message"] == message
    assert shape(parsed) == entities


@pytest.mark.asyncio
@pytest.mark.parametrize("source,message,entities", [
    ("<blockquote> a\n b</blockquote>", " a\n b", [("blockquote", 0, 5)]),
    ("<b> x</b>", " x", [("bold", 0, 2)]),
    ("<pre> x</pre>", " x", [("pre", 0, 2)]),
    ("  <b>x</b>  ", "x", [("bold", 0, 1)]),
    ("<b>x </b>", "x", [("bold", 0, 1)]),
    ("<b>x</b>\n\n", "x", [("bold", 0, 1)]),
    ("<b>x </b><i>y</i>", "x y", [("bold", 0, 2), ("italic", 2, 1)]),
    ("<b>  </b>", "", []),
])
async def test_html_whitespace_matches_what_telegram_stores(source, message, entities):
    parsed = await parse(source, enums.ParseMode.HTML)

    assert parsed["message"] == message
    assert shape(parsed) == entities


async def parse_markdown_blockquote(text):
    return await Markdown(None).parse(text)


def shape_markdown_blockquote(parsed):
    return [
        (type(e).__name__, e.offset, e.length) for e in (parsed["entities"] or [])
    ]


def quote_of(parsed):
    return parsed["entities"][0]


async def test_a_quoted_line_becomes_a_blockquote():
    parsed = await parse_markdown_blockquote(">quoted line")

    assert parsed["message"] == "quoted line"
    assert shape_markdown_blockquote(parsed) == [("MessageEntityBlockquote", 0, len("quoted line"))]


async def test_neighbouring_quoted_lines_are_one_blockquote():
    parsed = await parse_markdown_blockquote(">line one\n>line two")

    assert parsed["message"] == "line one\nline two"
    assert shape_markdown_blockquote(parsed) == [("MessageEntityBlockquote", 0, len("line one\nline two"))]


async def test_a_quote_ends_at_the_first_unquoted_line():
    parsed = await parse_markdown_blockquote(">quoted\nplain")

    assert parsed["message"] == "quoted\nplain"
    assert shape_markdown_blockquote(parsed) == [("MessageEntityBlockquote", 0, len("quoted"))]


async def test_an_expandable_quote_is_marked_collapsed():
    parsed = await parse_markdown_blockquote("**>expandable line||")

    assert parsed["message"] == "expandable line"
    assert quote_of(parsed).collapsed is True


async def test_an_expandable_quote_spans_its_quoted_lines():
    parsed = await parse_markdown_blockquote("**>line one\n>line two||")

    assert parsed["message"] == "line one\nline two"
    quote = quote_of(parsed)
    assert quote.collapsed is True
    assert quote.length == len("line one\nline two")


async def test_formatting_inside_a_quote_still_applies():
    parsed = await parse_markdown_blockquote(">quoted **bold** here")

    assert parsed["message"] == "quoted bold here"
    assert shape_markdown_blockquote(parsed) == [
        ("MessageEntityBlockquote", 0, len("quoted bold here")),
        ("MessageEntityBold", 7, 4),
    ]


async def test_a_marker_that_does_not_start_a_line_is_text():
    parsed = await parse_markdown_blockquote("a > b")

    assert parsed["message"] == "a > b"
    assert not parsed["entities"]


async def test_a_marker_inside_a_code_block_is_text():
    parsed = await parse_markdown_blockquote("```\n>not a quote\n```")

    assert ">not a quote" in parsed["message"]
    assert [name for name, _, _ in shape_markdown_blockquote(parsed)] == ["MessageEntityPre"]


@pytest.mark.parametrize(
    "source",
    [
        ">quoted line",
        ">line one\n>line two",
        "**>expandable line||",
        "**>line one\n>line two||",
        ">quoted **bold** here",
    ],
)
async def test_a_quote_survives_being_rendered_and_read_again(source):
    parsed = await parse_markdown_blockquote(source)
    entities = [MessageEntity._parse(None, e, {}) for e in parsed["entities"]]
    rendered = Markdown.unparse(parsed["message"], entities)

    assert rendered == source, (
        "unparse writes what parse must be able to read back, or a quote is "
        "silently lost every time a message is rendered and re-sent"
    )

    again = await parse_markdown_blockquote(rendered)
    assert again["message"] == parsed["message"]
    assert shape_markdown_blockquote(again) == shape_markdown_blockquote(parsed)


# what an ordinary text message in a channel legitimately needs: itself, the
# sender, the chat, their verification badges and its two entities
CALL_BUDGET = 12
NONE_BUDGET = 3


def _user(uid=1):
    return raw.types.User(
        id=uid, first_name="U", usernames=[], restriction_reason=[], access_hash=1
    )


def _channel(cid=100):
    return raw.types.Channel(
        id=cid,
        title="C",
        photo=raw.types.ChatPhotoEmpty(),
        date=0,
        access_hash=1,
        usernames=[],
        restriction_reason=[],
    )


def _message():
    return raw.types.Message(
        id=1,
        peer_id=raw.types.PeerChannel(channel_id=100),
        from_id=raw.types.PeerUser(user_id=1),
        date=1700000000,
        restriction_reason=[],
        message="hello",
        entities=[
            raw.types.MessageEntityBold(offset=0, length=5),
        ],
    )


def _client():
    client = Mock()
    client.me = Mock(id=999, is_bot=True, is_premium=False)
    client.message_cache = {}
    client.parse_mode = None

    return client


@pytest.fixture
def counted(monkeypatch):
    """Every types.*._parse* wrapped, so the calls one parse makes can be counted."""

    calls, nones = Counter(), Counter()

    for name in dir(types):
        cls = getattr(types, name)

        if not isinstance(cls, type):
            continue

        for attr in dir(cls):
            if not attr.startswith("_parse"):
                continue

            static = inspect.getattr_static(cls, attr, None)

            if not isinstance(static, staticmethod):
                continue

            inner = static.__func__
            key = f"{name}.{attr}"

            if inspect.iscoroutinefunction(inner):
                def wrap(inner=inner, key=key):
                    async def wrapper(*args, **kwargs):
                        calls[key] += 1
                        result = await inner(*args, **kwargs)

                        if result is None:
                            nones[key] += 1

                        return result

                    return wrapper
            else:
                def wrap(inner=inner, key=key):
                    def wrapper(*args, **kwargs):
                        calls[key] += 1
                        result = inner(*args, **kwargs)

                        if result is None:
                            nones[key] += 1

                        return result

                    return wrapper

            monkeypatch.setattr(cls, attr, staticmethod(wrap()), raising=False)

    return calls, nones


async def test_an_ordinary_message_makes_few_sub_parser_calls(counted):
    calls, _ = counted

    await types.Message._parse(_client(), _message(), {1: _user()}, {100: _channel()})

    total = sum(calls.values())

    assert total <= CALL_BUDGET, (
        f"parsing one message made {total} sub-parser calls: "
        f"{dict(calls.most_common())}"
    )


async def test_almost_none_of_them_answer_none(counted):
    """A parser handed a field that is already None costs a call to say so."""

    calls, nones = counted

    await types.Message._parse(_client(), _message(), {1: _user()}, {100: _channel()})

    wasted = sum(nones.values())

    assert wasted <= NONE_BUDGET, (
        f"{wasted} of {sum(calls.values())} sub-parser calls answered None: "
        f"{dict(nones.most_common())}"
    )


async def test_each_peer_is_parsed_once(counted):
    """from_user, sender_chat, via_bot and the rest used to re-parse the same peers."""

    calls, _ = counted

    await types.Message._parse(_client(), _message(), {1: _user()}, {100: _channel()})

    assert calls["User._parse"] == 1
    assert calls["Chat._parse"] == 1
    assert calls["Chat._parse_channel_chat"] == 1


async def test_the_message_still_parses_correctly(counted):
    """A budget met by parsing nothing would be no use."""

    parsed = await types.Message._parse(
        _client(), _message(), {1: _user()}, {100: _channel()}
    )

    assert parsed.id == 1
    assert parsed.from_user.id == 1
    assert parsed.chat.id == -1000000000100
    assert parsed.text == "hello"
    assert parsed.entities[0].type is not None
INLINE_MESSAGE_ID = base64.urlsafe_b64encode(struct.pack("<iqq", 2, 123, 456)).decode().rstrip("=")


class FakeClient:
    link_preview_options = None
    parse_mode = None
    parser = Parser(None)


@pytest.fixture
def sent(monkeypatch):
    calls = {}

    async def invoke_inline(client, dc_id, query, business_connection_id):
        calls["dc_id"] = dc_id
        calls["query"] = query
        return True

    monkeypatch.setattr(module, "invoke_inline", invoke_inline)

    return calls


async def edit(**kwargs):
    return await module.EditInlineText.edit_inline_text(
        FakeClient(), INLINE_MESSAGE_ID, **kwargs
    )


@pytest.mark.asyncio
async def test_markdown_rich_text_becomes_a_rich_message(sent):
    await edit(rich_text="# Title\n\n**bold**")

    query = sent["query"]

    assert isinstance(query.rich_message, raw.types.InputRichMessageMarkdown)
    assert query.rich_message.markdown == "# Title\n\n**bold**"
    assert query.message == ""
    assert sent["dc_id"] == 2


@pytest.mark.asyncio
async def test_html_rich_text_becomes_a_rich_message(sent):
    await edit(rich_text="<h2>Title</h2>", rich_text_parse_mode=enums.ParseMode.HTML)

    assert isinstance(sent["query"].rich_message, raw.types.InputRichMessageHTML)


@pytest.mark.asyncio
async def test_an_input_rich_message_is_written_as_given(sent):
    await edit(rich_text=types.InputRichMessage(html="<p>x</p>"))

    assert isinstance(sent["query"].rich_message, raw.types.InputRichMessageHTML)


@pytest.mark.asyncio
async def test_plain_text_is_untouched(sent):
    await edit(text="plain **bold**")

    query = sent["query"]

    assert query.rich_message is None
    assert query.message == "plain bold"
    assert query.entities


@pytest.mark.asyncio
async def test_neither_text_nor_rich_text_raises(sent):
    with pytest.raises(ValueError):
        await edit()


class StubClient:
    business_connection_id = None
    parse_mode = None

    def __init__(self):
        self.calls = {}

    def __getattr__(self, name):
        async def call(**kwargs):
            self.calls[name] = kwargs
            return "edited"

        return call


def a_message(client):
    from pyrogram import enums as _enums
    from pyrogram.types import Chat, Message

    return Message(
        id=7,
        chat=Chat(id=11, type=_enums.ChatType.PRIVATE, client=client),
        client=client,
    )


@pytest.mark.asyncio
async def test_message_edit_text_forwards_rich_text():
    client = StubClient()

    await a_message(client).edit_text(rich_text="# hi", rich_text_parse_mode=enums.ParseMode.HTML)

    sent = client.calls["edit_message_text"]

    assert sent["rich_text"] == "# hi"
    assert sent["rich_text_parse_mode"] is enums.ParseMode.HTML
    assert sent["text"] is None


@pytest.mark.asyncio
async def test_callback_query_edits_a_chat_message_with_rich_text():
    from pyrogram.types import CallbackQuery, User

    client = StubClient()
    query = CallbackQuery(
        id="1",
        from_user=User(id=1, client=client),
        chat_instance="x",
        message=a_message(client),
        client=client,
    )

    await query.edit_message_text(rich_text="# hi")

    assert client.calls["edit_message_text"]["rich_text"] == "# hi"


@pytest.mark.asyncio
async def test_callback_query_edits_an_inline_message_with_rich_text():
    from pyrogram.types import CallbackQuery, User

    client = StubClient()
    query = CallbackQuery(
        id="1",
        from_user=User(id=1, client=client),
        chat_instance="x",
        inline_message_id=INLINE_MESSAGE_ID,
        client=client,
    )

    await query.edit_message_text(rich_text="# hi")

    assert client.calls["edit_inline_text"]["rich_text"] == "# hi"


@pytest.mark.asyncio
async def test_message_edit_ephemeral_text_keeps_the_old_name_working():
    from pyrogram.types import User

    client = StubClient()
    message = a_message(client)
    message.ephemeral_message_id = 3
    message.receiver_user = User(id=5, client=client)
    rich = types.InputRichMessage(markdown="# hi")

    await message.edit_ephemeral_text(rich_message=rich)

    assert client.calls["edit_ephemeral_message_text"]["rich_message"] is rich


@pytest.mark.asyncio
async def test_the_deprecated_name_still_reaches_the_wire(monkeypatch):
    import pyrogram
    from pyrogram.methods.ephemeral import edit_ephemeral_message_text as ephemeral

    seen = {}

    async def edit_ephemeral(client, chat_id, receiver_id, message_id, **kwargs):
        seen.update(kwargs)
        return None

    monkeypatch.setattr(ephemeral, "edit_ephemeral", edit_ephemeral)

    await pyrogram.Client.edit_ephemeral_message_text(
        FakeClient(), 1, 2, 3, rich_message=types.InputRichMessage(markdown="# hi")
    )

    assert isinstance(seen["rich_message"], raw.types.InputRichMessageMarkdown)
    assert seen["rich_message"].markdown == "# hi"


@pytest.mark.asyncio
async def test_build_input_rich_message_picks_the_constructor_by_parse_mode():
    from pyrogram import utils

    client = FakeClient()

    assert isinstance(
        await utils.build_input_rich_message(client, "# hi"),
        raw.types.InputRichMessageMarkdown,
    )
    assert isinstance(
        await utils.build_input_rich_message(client, "<h1>hi</h1>", enums.ParseMode.HTML),
        raw.types.InputRichMessageHTML,
    )
    assert isinstance(
        await utils.build_input_rich_message(client, types.InputRichMessage(html="<p>x</p>")),
        raw.types.InputRichMessageHTML,
    )
PYROGRAM = Path(__file__).resolve().parents[1] / "pyrogram"


def photo(n: int = 1) -> "raw.types.InputPhoto":
    return raw.types.InputPhoto(id=n, access_hash=n, file_reference=b"")


def document(n: int = 1) -> "raw.types.InputDocument":
    return raw.types.InputDocument(id=n, access_hash=n, file_reference=b"")


def test_block_message_has_no_trailing_vectors():
    for media in (None, types.InputRichMessageMedia(photos=[])):
        b = types.InputRichMessage(
            blocks=[types.InputRichBlockDivider()], media=media
        ).write().write()
        assert len(b) == 20, b.hex()


@pytest.mark.parametrize("kwargs,expected", [
    ({"html": '<img src="tg://photo?id=pic">'}, raw.types.InputRichMessageHTML),
    ({"markdown": "![](tg://photo?id=pic)"}, raw.types.InputRichMessageMarkdown),
])
def test_media_reaches_html_and_markdown(kwargs, expected):
    written = types.InputRichMessage(
        media=[types.InputRichMessageMedia(id="pic", media=photo())],
        **kwargs,
    ).write()

    assert isinstance(written, expected)
    assert written.files == [raw.types.InputRichFilePhoto(id="pic", photo=photo())], (
        "tg://photo?id= resolves against the files vector, so a message whose "
        "files are dropped can never show its media"
    )


def test_a_document_becomes_a_rich_file_document():
    written = types.InputRichMessage(
        html="x", media=types.InputRichMessageMedia(id="vid", media=document())
    ).write()

    assert written.files == [raw.types.InputRichFileDocument(id="vid", document=document())]


def test_no_media_leaves_the_files_vector_absent():
    written = types.InputRichMessage(html="x").write()

    assert written.files is None, "an empty vector is not the same as an absent flag"


def test_an_id_the_server_will_not_accept_is_refused():
    with pytest.raises(ValueError, match="Invalid media id"):
        types.InputRichMessageMedia(id="not a valid id!", media=photo()).write_file()


def test_media_that_still_needs_uploading_says_so():
    with pytest.raises(ValueError, match="already exists on Telegram"):
        types.InputRichMessageMedia(id="pic", media=object()).write_file()


def test_block_vectors_merge_across_a_media_list():
    written = types.InputRichMessage(
        blocks=[types.InputRichBlockDivider()],
        media=[
            types.InputRichMessageMedia(photos=[photo(1)]),
            types.InputRichMessageMedia(photos=[photo(2)], documents=[document(3)]),
        ],
    ).write()

    assert written.photos == [photo(1), photo(2)]
    assert written.documents == [document(3)]


def _rich_message_constructions():
    paths = sorted((PYROGRAM / "methods").rglob("*.py")) + [PYROGRAM / "utils.py"]

    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))

        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue

            name = getattr(node.func, "attr", None)

            if name in ("InputRichMessageHTML", "InputRichMessageMarkdown"):
                yield path, node


def test_every_send_path_threads_the_files_vector():
    calls = list(_rich_message_constructions())

    assert calls, "this guard is pointless if nothing builds a rich message"

    for path, node in calls:
        keywords = {kw.arg for kw in node.keywords}

        assert "files" in keywords, (
            f"{path.name}:{node.lineno} builds a rich message without files=, so "
            "media referenced from the text can never resolve"
        )


def test_an_ordered_list_uses_the_ordered_item_constructors():
    """pageBlockOrderedList takes Vector<PageListOrderedItem>, a different base
    type from the Vector<PageListItem> an unordered list takes. Feeding it plain
    page list items put the wrong constructor on the wire and Telegram answered
    RICH_MESSAGE_BLOCK_UNEXPECTED, so an ordered list could never be sent."""
    block = types.InputRichBlockList(
        items=[
            types.InputRichBlockListItem(text="first"),
            types.InputRichBlockListItem(text="done", has_checkbox=True, is_checked=True),
            types.InputRichBlockListItem(
                blocks=[types.InputRichBlockParagraph(text="nested")]
            ),
        ],
        ordered=True,
    ).write()

    assert isinstance(block, raw.types.PageBlockOrderedList)
    assert [type(item) for item in block.items] == [
        raw.types.PageListOrderedItemText,
        raw.types.PageListOrderedItemText,
        raw.types.PageListOrderedItemBlocks,
    ]
    assert block.items[1].checkbox and block.items[1].checked
    block.write()


def test_an_unordered_list_keeps_the_plain_item_constructors():
    block = types.InputRichBlockList(
        items=[
            types.InputRichBlockListItem(text="first"),
            types.InputRichBlockListItem(
                blocks=[types.InputRichBlockParagraph(text="nested")]
            ),
        ]
    ).write()

    assert isinstance(block, raw.types.PageBlockList)
    assert [type(item) for item in block.items] == [
        raw.types.PageListItemText,
        raw.types.PageListItemBlocks,
    ]
    block.write()


def test_a_thinking_block_writes_the_page_block_the_tag_maps_to():
    written = types.InputRichMessage(
        blocks=[types.InputRichBlockThinking(text="Reading files")]
    ).write()

    assert written.blocks == [
        raw.types.PageBlockThinking(
            text=raw.types.TextConcat(texts=[raw.types.TextPlain(text="Reading files")])
        )
    ]


@pytest.mark.parametrize("kwargs,expected,field", [
    ({"html": "<tg-thinking>Thinking...</tg-thinking>"},
     raw.types.InputRichMessageHTML, "html"),
    ({"markdown": "<tg-thinking>Thinking...</tg-thinking>"},
     raw.types.InputRichMessageMarkdown, "markdown"),
])
def test_the_thinking_tag_reaches_the_wire_untouched(kwargs, expected, field):
    written = types.InputRichMessage(**kwargs).write()

    assert isinstance(written, expected)
    assert getattr(written, field) == "<tg-thinking>Thinking...</tg-thinking>", (
        "the html and markdown forms are parsed by the server, so a tag the "
        "library rewrote or dropped could never render"
    )


class UploadingClient:
    def __init__(self):
        self.uploaded = []

    async def invoke(self, query, *args, **kwargs):
        self.uploaded.append(query)

        return raw.types.MessageMediaPhoto(
            photo=raw.types.Photo(
                id=111, access_hash=222, file_reference=b"fr", date=0, sizes=[], dc_id=2
            )
        )

    async def resolve_peer(self, peer_id):
        return raw.types.InputPeerUser(user_id=peer_id, access_hash=9)

    async def save_file(self, *args, **kwargs):
        return raw.types.InputFile(id=1, parts=1, name="x.png", md5_checksum="")


@pytest.mark.asyncio
async def test_a_block_uploads_the_photo_it_was_given():
    client = UploadingClient()

    message = types.InputRichMessage(
        blocks=[types.InputRichBlockPhoto(photo=types.InputMediaPhoto("README.md"))]
    )

    written = await utils.build_input_rich_message(client, message, chat_id=5)

    assert written.blocks[0].photo_id == 111
    assert [p.id for p in written.photos] == [111]
    assert written.documents is None


@pytest.mark.asyncio
async def test_a_block_mention_reaches_the_users_vector():
    client = UploadingClient()

    message = types.InputRichMessage(
        blocks=[
            types.InputRichBlockParagraph(
                text=raw.types.TextMentionName(
                    user_id=777, text=raw.types.TextPlain(text="hi")
                )
            )
        ]
    )

    written = await utils.build_input_rich_message(client, message)

    assert [u.user_id for u in written.users] == [777]


@pytest.mark.asyncio
async def test_a_media_id_uploads_for_html_too():
    client = UploadingClient()

    message = types.InputRichMessage(
        html='<img src="tg://photo?id=a">',
        media=types.InputRichMessageMedia(id="a", media=types.InputMediaPhoto("README.md")),
    )

    written = await utils.build_input_rich_message(client, message)

    assert isinstance(written.files[0], raw.types.InputRichFilePhoto)
    assert written.files[0].photo.id == 111


@pytest.mark.asyncio
async def test_an_already_uploaded_block_photo_needs_no_round_trip():
    client = UploadingClient()

    message = types.InputRichMessage(
        blocks=[types.InputRichBlockPhoto(photo_id=42)],
        media=types.InputRichMessageMedia(photos=[photo(42)]),
    )

    written = await utils.build_input_rich_message(client, message)

    assert client.uploaded == []
    assert written.blocks[0].photo_id == 42


def test_a_media_block_without_media_is_refused():
    for block, kwargs in (
        (types.InputRichBlockPhoto, {}),
        (types.InputRichBlockVideo, {}),
        (types.InputRichBlockAnimation, {}),
        (types.InputRichBlockAudio, {}),
        (types.InputRichBlockVoiceNote, {}),
        (types.InputRichBlockDocument, {}),
    ):
        with pytest.raises(ValueError):
            block(**kwargs)

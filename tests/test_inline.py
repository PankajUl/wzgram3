import pytest

import pyrogram
from pyrogram import raw, types, enums, utils
from pyrogram.parser import Parser


ALLOWED = {"article", "photo", "gif", "video", "audio", "voice", "file",
           "geo", "venue", "contact", "sticker", "game"}
CONTENT = types.InputTextMessageContent("body")


@pytest.mark.parametrize(
    "result",
    [
        types.InlineQueryResultArticle(title="t", input_message_content=CONTENT),
        types.InlineQueryResultPhoto(photo_url="https://example.org/a.jpg"),
        types.InlineQueryResultAnimation(animation_url="https://example.org/a.gif"),
        types.InlineQueryResultVideo(
            video_url="https://example.org/a.mp4", thumb_url="https://example.org/a.jpg",
            title="t"),
        types.InlineQueryResultAudio(audio_url="https://example.org/a.mp3", title="t"),
        types.InlineQueryResultVoice(voice_url="https://example.org/a.ogg", title="t"),
        types.InlineQueryResultDocument(document_url="https://example.org/a.pdf", title="t"),
        types.InlineQueryResultContact(phone_number="+15550001111", first_name="A"),
        types.InlineQueryResultLocation(title="t", latitude=1.0, longitude=2.0),
        types.InlineQueryResultVenue(title="t", address="a", latitude=1.0, longitude=2.0),
        types.InlineQueryResultCachedSticker(sticker_file_id="x"),
        types.InlineQueryResultCachedPhoto(photo_file_id="x"),
        types.InlineQueryResultCachedVideo(video_file_id="x", title="t"),
        types.InlineQueryResultCachedAnimation(animation_file_id="x"),
        types.InlineQueryResultCachedAudio(audio_file_id="x"),
        types.InlineQueryResultCachedVoice(voice_file_id="x"),
        types.InlineQueryResultCachedDocument(document_file_id="x", title="t"),
    ],
)
def test_every_result_uses_a_type_telegram_knows(result):
    assert result.type in ALLOWED


async def test_a_location_result_is_sent_as_geo():
    result = types.InlineQueryResultLocation(title="t", latitude=1.0, longitude=2.0)

    written = await result.write(None)

    assert isinstance(written, raw.types.InputBotInlineResult)
    assert written.type == "geo"


async def test_venue_content_without_place_ids():
    content = types.InputVenueMessageContent(51.5, -0.12, "title", "address")

    raw_content = await content.write(None, None)

    assert raw_content.provider == ""
    assert raw_content.venue_id == ""
    assert raw_content.venue_type == ""


async def test_venue_content_keeps_google_place():
    content = types.InputVenueMessageContent(
        51.5, -0.12, "title", "address", google_place_id="g", google_place_type="t"
    )

    raw_content = await content.write(None, None)

    assert raw_content.provider == "google"
    assert raw_content.venue_id == "g"
    assert raw_content.venue_type == "t"


async def test_contact_content_without_last_name_or_vcard():
    content = types.InputContactMessageContent("+15550001111", "First")

    raw_content = await content.write(None, None)

    assert raw_content.last_name == ""
    assert raw_content.vcard == ""


async def test_invoice_content_without_provider_token():
    content = types.InputInvoiceMessageContent(
        title="t",
        description="d",
        payload="p",
        currency="XTR",
        prices=[types.LabeledPrice("label", 1)],
    )

    class FakeClient:
        test_mode = False

    raw_content = await content.write(FakeClient(), None)

    assert raw_content.provider == ""
    assert raw_content.payload == b"p"


async def test_every_content_serialises():
    class FakeClient:
        test_mode = False

    for content in (
        types.InputVenueMessageContent(51.5, -0.12, "title", "address"),
        types.InputContactMessageContent("+15550001111", "First"),
        types.InputInvoiceMessageContent(
            title="t", description="d", payload="p", currency="XTR",
            prices=[types.LabeledPrice("label", 1)],
        ),
    ):
        assert bytes((await content.write(FakeClient(), None)).write())


class FakeStorage:
    async def dc_id(self):
        return 2


class FakeClient:
    sleep_threshold = 10
    link_preview_options = None
    parse_mode = enums.ParseMode.DEFAULT

    def __init__(self):
        self.storage = FakeStorage()
        self.parser = Parser(self)
        self.sent = None

    async def invoke(self, query, sleep_threshold=None, business_connection_id=None):
        self.sent = query
        return True

    edit_inline_text = pyrogram.Client.edit_inline_text
    edit_inline_caption = pyrogram.Client.edit_inline_caption


@pytest.fixture
def inline_message_id():
    return utils.pack_inline_message_id(
        raw.types.InputBotInlineMessageID(dc_id=2, id=1, access_hash=1)
    )


@pytest.fixture
def client():
    return FakeClient()


async def test_entities_reach_the_request(client, inline_message_id):
    entities = [
        types.MessageEntity(type=enums.MessageEntityType.BOLD, offset=0, length=6),
        types.MessageEntity(type=enums.MessageEntityType.SPOILER, offset=7, length=6),
    ]

    await client.edit_inline_text(inline_message_id, "entity hidden", entities=entities)

    assert client.sent.message == "entity hidden"
    assert [type(e).__name__ for e in client.sent.entities] == [
        "MessageEntityBold", "MessageEntitySpoiler"
    ]


async def test_a_disabled_preview_sets_no_webpage(client, inline_message_id):
    await client.edit_inline_text(
        inline_message_id, "see https://wzgram.com",
        entities=[],
        link_preview_options=types.LinkPreviewOptions(is_disabled=True),
    )

    assert client.sent.no_webpage is True
    assert client.sent.media is None


async def test_a_preview_url_becomes_a_web_page_media(client, inline_message_id):
    await client.edit_inline_text(
        inline_message_id, "text",
        entities=[],
        link_preview_options=types.LinkPreviewOptions(
            url="https://wzgram.com", show_above_text=True, prefer_large_media=True),
    )

    assert isinstance(client.sent.media, raw.types.InputMediaWebPage)
    assert client.sent.media.url == "https://wzgram.com"
    assert client.sent.media.force_large_media is True
    assert client.sent.invert_media is True


async def test_disable_web_page_preview_still_wins(client, inline_message_id):
    await client.edit_inline_text(
        inline_message_id, "text", entities=[], disable_web_page_preview=True)

    assert client.sent.no_webpage is True


async def test_caption_entities_and_position_reach_the_request(client, inline_message_id):
    await client.edit_inline_caption(
        inline_message_id, "listed entities",
        caption_entities=[
            types.MessageEntity(type=enums.MessageEntityType.ITALIC, offset=0, length=6)
        ],
        show_caption_above_media=True,
    )

    assert client.sent.message == "listed entities"
    assert [type(e).__name__ for e in client.sent.entities] == ["MessageEntityItalic"]
    assert client.sent.invert_media is True


async def test_a_string_copy_text_becomes_a_button():
    button = types.InlineKeyboardButton("copy", copy_text="plain string")

    assert isinstance(button.copy_text, types.CopyTextButton)
    assert button.copy_text.text == "plain string"

    written = await button.write(None)

    assert written.type.copy_text == "plain string"


async def test_a_copy_text_button_is_left_alone():
    button = types.InlineKeyboardButton("copy", copy_text=types.CopyTextButton("kept"))

    assert button.copy_text.text == "kept"

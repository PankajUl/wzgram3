Rich Messages
=============

*Bot API 10.1 — June 2026, media added in 10.2*

A rich message is a document sent as a message. Where an ordinary message is a line of text
with entities on top, a rich message has structure: headings, lists, tables, pull quotes,
code blocks, collapsible sections, collages, maps and captions — the vocabulary of an
Instant View article, composed and sent from your own code.

Rich messages are sent by **bots**, and by **users with Premium** — a user account
without it gets ``[400 RICH_MESSAGE_UNSUPPORTED]``. Streaming a draft with
:meth:`~pyrogram.Client.send_rich_message_draft` stays a bot-only method
(``[400 USER_BOT_REQUIRED]``).


-----

Three ways to write one
-----------------------

:obj:`~pyrogram.types.InputRichMessage` takes exactly one of ``html``, ``markdown`` or
``blocks``. They map to three different constructors on the wire and they carry media
differently, which is the one thing worth understanding before you start.

.. code-block:: python

    from wzgram.types import InputRichMessage

    await app.send_rich_message(
        chat_id="me",
        rich_text=InputRichMessage(
            html="<h2>Release notes</h2><p>Layer <b>228</b> is live.</p>",
        ),
    )

``markdown`` is the same thing in the other syntax. ``blocks`` is the structured form:

.. code-block:: python

    from wzgram.types import (
        InputRichMessage,
        InputRichBlockSectionHeading,
        InputRichBlockParagraph,
        InputRichBlockList,
        InputRichBlockListItem,
        InputRichBlockPreformatted,
    )

    await app.send_rich_message(
        chat_id="me",
        rich_text=InputRichMessage(blocks=[
            InputRichBlockSectionHeading(text="Release notes", size=2),
            InputRichBlockParagraph(text="Layer 228 is live."),
            InputRichBlockList(
                items=[
                    InputRichBlockListItem(text="Ephemeral messages"),
                    InputRichBlockListItem(text="Communities"),
                    InputRichBlockListItem(text="Rich message media", has_checkbox=True, is_checked=True),
                ],
                ordered=False,
            ),
            InputRichBlockPreformatted(text="pip install -U wzgram", language="bash"),
        ]),
    )

Passing more than one of the three is not an error: the first set one in the order
``html``, ``markdown``, ``blocks`` wins and the rest are ignored. Setting none raises
``ValueError`` when the message is sent.

The block vocabulary
--------------------

Every block is a class under :obj:`~pyrogram.types.InputRichBlock`:

============================================  ===========================================
Block                                          What it is
============================================  ===========================================
``InputRichBlockParagraph``                    a paragraph of text
``InputRichBlockSectionHeading``               a heading, ``size`` 1-6
``InputRichBlockPreformatted``                 a code block with a ``language``
``InputRichBlockList``                         ordered or bulleted, items may have checkboxes
``InputRichBlockBlockQuotation``               a quote wrapping other blocks
``InputRichBlockExpandableBlockQuotation``     a quote that starts collapsed
``InputRichBlockPullQuotation``                a pull quote with a ``credit``
``InputRichBlockTable``                        rows of ``InputRichBlockTableCell``
``InputRichBlockDetails``                      a collapsible section
``InputRichBlockCollage`` / ``…Slideshow``     grouped media
``InputRichBlockPhoto`` / ``…Video``           a single photo or video, with spoiler and autoplay flags
``InputRichBlockAudio`` / ``…VoiceNote``       an audio file or a voice note
``InputRichBlockAnimation``                    a looping video
``InputRichBlockMap``                          a map at a geo point and zoom
``InputRichBlockMathematicalExpression``       a formula
``InputRichBlockAnchor``                       a named target to link to
``InputRichBlockDivider``                      a horizontal rule
``InputRichBlockFooter``                       trailing small print
``InputRichBlockThinking``                     a "Thinking..." placeholder, drafts only
``InputRichBlockButtons``                      a row of 1-8 ``RichMessageButton``
``InputRichBlockDocument``                     a general file, by ``document_id``
============================================  ===========================================

Attaching media
---------------

A rich message refers to media that lives on Telegram. You can hand it one that is already
there — a file identifier, an ``InputPhoto`` or an ``InputDocument`` — or an
:obj:`~pyrogram.types.InputMedia` object holding a local path or an HTTP URL, which is
uploaded for you when the message is sent.

How you attach it depends on which of the three forms you used, and
:obj:`~pyrogram.types.InputRichMessageMedia` covers both shapes:

**html and markdown** — each media entry needs an ``id`` of your choosing, and the text
refers to it with a ``tg://`` link:

.. code-block:: python

    from wzgram.types import InputMediaPhoto, InputRichMessage, InputRichMessageMedia

    await app.send_rich_message(
        chat_id="me",
        rich_text=InputRichMessage(
            html='<p>Here it is:</p><img src="tg://photo?id=cover">',
            media=[InputRichMessageMedia(id="cover", media=InputMediaPhoto("cover.jpg"))],
        ),
    )

A file identifier works in the same place: ``InputRichMessageMedia(id="cover",
media=photo_file_id)``. The scheme says what kind of media it is: ``tg://photo?id=``,
``tg://video?id=`` or ``tg://audio?id=``.

**blocks** — a media block takes the file directly, and the vectors the wire format needs
are built while the message is sent:

.. code-block:: python

    InputRichMessage(
        blocks=[InputRichBlockPhoto(photo=InputMediaPhoto("cover.jpg"), caption="The cover")],
    )

The parameter is named after the block: ``photo`` for
:obj:`~pyrogram.types.InputRichBlockPhoto`, ``video``, ``animation``, ``audio``, ``voice``
and ``document`` for the others. Each takes a file identifier or an ``InputMedia`` object.

If you have already uploaded the file and hold its raw type, pass the identifier instead
and carry the vectors yourself:

.. code-block:: python

    InputRichMessage(
        blocks=[InputRichBlockPhoto(photo_id=input_photo.id, caption="The cover")],
        media=[InputRichMessageMedia(photos=[input_photo])],
    )

A block's ``photo_id`` / ``video_id`` / ``audio_id`` must equal the ``id`` attribute of the
corresponding ``InputPhoto`` or ``InputDocument`` in those vectors. MTProto carries no
string identifiers on this side, which is why the two shapes differ at all.

Users mentioned by a block travel in a vector of their own; they are collected from the
blocks and resolved for you.

Buttons
-------

*Bot API 10.3 — August 2026*

A rich message can carry buttons of its own, in a row of its own
(:obj:`~pyrogram.types.InputRichBlockButtons`) or inline in the text
(:obj:`~pyrogram.types.RichTextButton`). Both hold a
:obj:`~pyrogram.types.RichMessageButton`, which takes the same actions an inline keyboard
button does — ``url``, ``callback_data``, ``web_app``, ``login_url``, the
``switch_inline_query`` family, ``copy_text`` — plus a ``style``:

.. code-block:: python

    from wzgram import enums
    from wzgram.types import InputRichBlockButtons, RichMessageButton

    InputRichBlockButtons(
        buttons=[
            RichMessageButton(
                text="Buy",
                callback_data="buy",
                style=enums.RichButtonStyle.PRIMARY,
            ),
            RichMessageButton(
                text="Cancel",
                callback_data="cancel",
                style=enums.RichButtonStyle.DANGER,
            ),
        ],
        align=enums.BlockAlignment.CENTER,
    )

:obj:`~pyrogram.enums.RichButtonStyle` adds ``LINK`` to the styles a keyboard button has —
the button is then drawn as a plain link with no border. A button with nothing set is a
:obj:`~pyrogram.types.DisabledButton`, which renders and does nothing; ``disabled`` says so
explicitly.

Rich buttons never resolve a peer, because a block is written synchronously. A ``login_url``
therefore leaves the bot for the server to fill in, which is what layer 229 made optional.

Drafts and diffs
----------------

:meth:`~pyrogram.Client.send_rich_message_draft` streams a *partial* rich message as a
typing action, which is how a bot shows its output while it is still being generated. The
draft is ephemeral — clients drop it after about 30 seconds or as soon as a real message
arrives — so call :meth:`~pyrogram.Client.send_rich_message` to persist the result:

.. code-block:: python

    await app.send_rich_message_draft(chat_id, draft_id=1, rich_message=rich)

:obj:`~pyrogram.types.InputRichBlockThinking` is a "Thinking..." placeholder for content
that has not been generated yet, and ``<tg-thinking>Thinking...</tg-thinking>`` is the same
block in the ``html`` and ``markdown`` forms. Both are accepted only by this method:

.. code-block:: python

    await app.send_rich_message_draft(
        chat_id, draft_id=1,
        rich_message=InputRichMessage(html="<tg-thinking>Reading files</tg-thinking>"),
    )

Pass ``can_stop=True`` to offer the user a button that stops the generation. Pressing it
delivers a :obj:`~pyrogram.types.MessageGenerationStopped` update, which
:meth:`~pyrogram.Client.on_message_generation_stopped` handles; add ``keep_on_stop=True`` to
leave the partial draft in the chat rather than clearing it.

.. code-block:: python

    @app.on_message_generation_stopped()
    async def stopped(client, update):
        cancel_generation(update.draft_id)

:obj:`~pyrogram.types.RichTextDiff` marks a rich text as a *change* against an older one,
pairing ``text`` with ``old_text``. Clients render the difference.

A message that arrives truncated
--------------------------------

A rich message too large to travel inline is delivered with only its first blocks and
``is_partial`` set on its :obj:`~pyrogram.types.RichMessage`.
:meth:`~pyrogram.Client.get_rich_message` fetches the whole of it:

.. code-block:: python

    @app.on_message()
    async def handler(client, message):
        if message.rich_message and message.rich_message.is_partial:
            message = await client.get_rich_message(message.chat.id, message.id)

        for block in message.rich_message.blocks:
            print(block)

The fetch is a **user** method: a bot calling it gets ``[400 BOT_METHOD_INVALID]``, so a bot
that sends a long rich message cannot read the rest of its own back.

Rich text elsewhere
-------------------

``rich_text`` is not confined to :meth:`~pyrogram.Client.send_rich_message`. Every method
that can carry rich content takes the same three arguments: ``rich_text``, which is a
Markdown or HTML string or a whole :obj:`~pyrogram.types.InputRichMessage`,
``rich_text_parse_mode`` (Markdown by default) for when it is a string, and
``rich_text_media`` for the media it refers to. That is
:meth:`~pyrogram.Client.send_message`, :meth:`~pyrogram.Client.edit_message_text`,
:meth:`~pyrogram.Client.edit_message_caption`, :meth:`~pyrogram.Client.edit_inline_text`,
:meth:`~pyrogram.Client.send_ephemeral_message`,
:meth:`~pyrogram.Client.edit_ephemeral_message_text`,
:meth:`~pyrogram.Client.send_rich_message_draft`, and the bound
:meth:`~pyrogram.types.Message.edit_text`,
:meth:`~pyrogram.types.Message.edit_ephemeral_text` and
:meth:`~pyrogram.types.CallbackQuery.edit_message_text`. The rich-first methods —
:meth:`~pyrogram.Client.send_rich_message`, :meth:`~pyrogram.types.Message.reply_rich`
and :meth:`~pyrogram.types.Message.answer_rich` — spell the last two ``parse_mode`` and
``media``, since they have no text of their own to disambiguate from.

``rich_message`` still works on the two methods that used to take it, with a deprecation
warning; ``message.rich_message`` remains the name of the rich content on a *received*
message.

.. code-block:: python

    await app.send_message(
        chat_id="me",
        text="",
        rich_text="# Heading

A paragraph.",
    )

When ``rich_text`` is set, ``text`` is ignored.

Gotchas
-------

- A bare local path is still refused: a string is read as a file identifier. Wrap the path
  in an ``InputMedia`` object — ``InputMediaPhoto("cover.jpg")`` — to have it uploaded.
- With ``html`` and ``markdown``, the ``id`` in the media entry and the ``id=`` in the
  ``tg://`` link must match exactly. A typo means the media is dropped rather than an error.
- With ``blocks``, a block's ``photo_id`` is the *file's own* id, not a position in the
  vector. Copying an id from one message's media to another's will not resolve.
- The three constructors are not interchangeable at the protocol level even though one
  wzgram type covers them. Media attached in the block shape is ignored by the html shape
  and the other way round.

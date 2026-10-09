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

import logging
from typing import List, Optional, Union

import pyrogram
from pyrogram import enums, raw, types, utils

log = logging.getLogger(__name__)


class SendRichMessageDraft:
    async def send_rich_message_draft(
        self: "pyrogram.Client",
        chat_id: Union[int, str],
        draft_id: int,
        rich_text: Optional[Union[str, "types.InputRichMessage"]] = None,
        rich_text_parse_mode: "enums.ParseMode" = enums.ParseMode.MARKDOWN,
        rich_text_media: Optional[List["types.InputRichMessageMedia"]] = None,
        message_thread_id: Optional[int] = None,
        can_stop: Optional[bool] = None,
        keep_on_stop: Optional[bool] = None,
        rich_message: Optional["types.InputRichMessage"] = None,
    ) -> bool:
        """Send a rich message draft action, allowing bots to stream partial rich messages.

        When generating a rich message progressively (e.g. during AI response streaming),
        this method shows the user a typing indicator with the partial rich message content.
        The block :obj:`~pyrogram.types.InputRichBlockThinking` — or, in the *html* and
        *markdown* forms, the custom tag ``<tg-thinking>Thinking...</tg-thinking>`` it
        corresponds to — may be used as a placeholder while waiting for content to be
        generated. Both are accepted only here, so they can't be received in messages.

        .. include:: /_includes/usable-by/bots.rst

        Parameters:
            chat_id (``int`` | ``str``):
                Unique identifier (int) or username (str) of the target **private** chat.
                Drafts are streamed as a typing action and a group or channel refuses one
                with ``TEXTDRAFT_PEER_INVALID``.

            draft_id (``int``):
                Unique identifier of the draft; must be non-zero.
                Keep it constant for the whole generation, updates sharing an identifier are animated by clients.
                A different identifier does not restart the draft, it adds a second concurrent draft,
                and some clients collapse them into one.

            rich_text (``str`` | :obj:`~pyrogram.types.InputRichMessage`, *optional*):
                The partial rich message to stream, as Markdown or HTML text or as a
                whole :obj:`~pyrogram.types.InputRichMessage`.
                Use :obj:`~pyrogram.types.InputRichBlockThinking`, or the
                ``<tg-thinking>Thinking...</tg-thinking>`` tag in *html* and *markdown*,
                as a placeholder for content still being generated.

            rich_text_parse_mode (:obj:`~pyrogram.enums.ParseMode`, *optional*):
                Parse mode for *rich_text*. Defaults to Markdown.
                Ignored when *rich_text* is an :obj:`~pyrogram.types.InputRichMessage`.

            rich_text_media (List of :obj:`~pyrogram.types.InputRichMessageMedia`, *optional*):
                Media *rich_text* refers to through ``tg://photo?id=``, ``tg://video?id=``
                or ``tg://audio?id=`` links.
                Ignored when *rich_text* is an :obj:`~pyrogram.types.InputRichMessage`.

            rich_message (:obj:`~pyrogram.types.InputRichMessage`, *optional*):
                Deprecated alias of *rich_text*.

            message_thread_id (``int``, *optional*):
                Unique identifier for a forum topic thread.

            can_stop (``bool``, *optional*):
                Pass True to show the user a button to stop further drafts. The bot then
                receives a :obj:`~pyrogram.types.MessageGenerationStopped` update when the
                user presses it.

            keep_on_stop (``bool``, *optional*):
                Pass True to keep the draft in the chat when the button is pressed. The draft
                still disappears after a short time or as soon as the bot sends a message, so
                call :meth:`~pyrogram.Client.send_rich_message` to preserve it.

        Returns:
            ``bool``: On success, True is returned.

        .. note::

            The draft is ephemeral: clients drop it after ``message_typing_draft_ttl`` seconds
            (30 by default, server-configured) or as soon as a real message arrives, so call
            :meth:`~pyrogram.Client.send_rich_message` to persist the result. Throttle the stream:
            setTyping is rate-limited to 20 calls per 5s and 40 per 30s per peer.

        Example:
            .. code-block:: python

                draft_id = app.rnd_id()

                for i, word in enumerate(words):
                    await app.send_rich_message_draft(
                        chat_id, draft_id,
                        types.InputRichMessage(html=" ".join(words[:i + 1])),
                    )
                    await asyncio.sleep(0.33)

                await app.send_rich_message(chat_id, text)
        """
        if rich_message is not None:
            log.warning(
                "`rich_message` is deprecated and will be removed in future updates. "
                "Use `rich_text` instead."
            )

            if rich_text is None:
                rich_text = rich_message

        if rich_text is None:
            raise ValueError("rich_text must be given")

        return await self.invoke(
            raw.functions.messages.SetTyping(
                peer=await self.resolve_peer(chat_id),
                action=raw.types.InputSendMessageRichMessageDraftAction(
                    random_id=draft_id,
                    rich_message=await utils.build_input_rich_message(
                        self, rich_text, rich_text_parse_mode, rich_text_media, chat_id
                    ),
                    can_stop=can_stop,
                    keep_on_stop=keep_on_stop,
                ),
                top_msg_id=message_thread_id,
            )
        )

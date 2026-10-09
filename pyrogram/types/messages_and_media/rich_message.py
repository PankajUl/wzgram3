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

from typing import Dict, List, Optional

import pyrogram
from pyrogram import raw, types

from ..object import Object


class RichMessage(Object):
    """Rich formatted message.

    Parameters:
        blocks (List of :obj:`~pyrogram.types.RichBlock`):
            Content of the message.

        is_rtl (``bool``, *optional*):
            True, if the rich message must be shown right-to-left.

        is_partial (``bool``, *optional*):
            True, if these blocks are only the beginning of the message: a rich message too
            large to travel inline arrives truncated, and
            :meth:`~pyrogram.Client.get_rich_message` fetches the whole of it.
    """

    def __init__(
        self,
        *,
        blocks: List["types.RichBlock"],
        is_rtl: Optional[bool] = None,
        is_partial: Optional[bool] = None,
    ):
        super().__init__()

        self.blocks = blocks
        self.is_rtl = is_rtl
        self.is_partial = is_partial

    @staticmethod
    async def _parse(
        client: "pyrogram.Client",
        rich_message: "raw.types.RichMessage",
        users: Dict[int, "raw.base.User"] = {},
        chats: Dict[int, "raw.base.Chat"] = {},
    ) -> "RichMessage":
        if isinstance(rich_message, raw.types.RichMessage):
            photos = {photo.id: photo for photo in rich_message.photos}
            documents = {document.id: document for document in rich_message.documents}

            parsed = RichMessage(
                blocks=types.List(
                    [
                        await types.RichBlock._parse(
                            client,
                            block,
                            photos,
                            documents,
                            users,
                            chats,
                        )
                        for block in rich_message.blocks
                    ]
                ),
                is_rtl=rich_message.rtl,
                is_partial=rich_message.part,
            )
            parsed._raw = rich_message
            parsed._users = [
                raw.types.InputUser(user_id=user.id, access_hash=user.access_hash)
                for user in (users.get(i) for i in _mentioned_user_ids(rich_message.blocks))
                if isinstance(user, raw.types.User) and user.access_hash is not None
            ]
            return parsed

    def _write(self) -> "raw.types.InputRichMessage":
        if getattr(self, "_raw", None) is None:
            raise ValueError("Only a received rich message can be sent again")

        return raw.types.InputRichMessage(
            blocks=self._raw.blocks,
            rtl=self._raw.rtl,
            photos=[
                raw.types.InputPhoto(id=p.id, access_hash=p.access_hash, file_reference=p.file_reference)
                for p in self._raw.photos
            ] or None,
            documents=[
                raw.types.InputDocument(id=d.id, access_hash=d.access_hash, file_reference=d.file_reference)
                for d in self._raw.documents
            ] or None,
            users=self._users or None,
        )


def _mentioned_user_ids(obj, found=None) -> set:
    found = set() if found is None else found

    if isinstance(obj, raw.types.TextMentionName):
        found.add(obj.user_id)

    if isinstance(obj, list):
        for item in obj:
            _mentioned_user_ids(item, found)
    elif isinstance(obj, raw.core.TLObject):
        for name in obj.__slots__:
            _mentioned_user_ids(getattr(obj, name), found)

    return found


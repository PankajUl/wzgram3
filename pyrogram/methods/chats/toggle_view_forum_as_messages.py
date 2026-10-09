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

# ***************************
# GENERATED FILE - DO NOT EDIT
# Source: tl:channels.toggleViewForumAsMessages
# ***************************

from typing import Union, Optional

import pyrogram
from pyrogram import raw
from pyrogram import types


class ToggleViewForumAsMessages:
    async def toggle_view_forum_as_messages(
        self: "pyrogram.Client",
        chat_id: Union[int, str],
        enabled: Optional[bool] = None,
    ) -> bool:
        """Toggle whether forum topics are displayed as messages.

        .. include:: /_includes/usable-by/users.rst

        Parameters:

            enabled (bool, *optional*): Whether to view forum as messages

            chat_id (``int`` | ``str``):
                Unique identifier (int) or username (str) of the target chat.



        Returns:
            ``bool``: True on success.

        Example:
            .. code-block:: python

                await app.toggle_view_forum_as_messages(chat_id, ...)
        """

        await self.invoke(
            raw.functions.channels.ToggleViewForumAsMessages(
                
                channel=await self.resolve_peer(chat_id),
                enabled=enabled,
            )
        )

        return True

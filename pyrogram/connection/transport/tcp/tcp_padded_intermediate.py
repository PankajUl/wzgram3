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

import os
import random
import logging
import asyncio
from struct import pack, unpack
from typing import Optional

from .tcp import PADDED_INTERMEDIATE_OBFUSCATE_TAG, TCP

log = logging.getLogger(__name__)


def strip_padding(payload: bytes) -> bytes:
    if len(payload) < 24:
        return payload[:4]

    if len(payload) >= 20 and int.from_bytes(payload[:8], "little") == 0:
        return payload[:20 + int.from_bytes(payload[16:20], "little")]

    return payload[:len(payload) - (len(payload) - 8) % 16]


class TCPPaddedIntermediate(TCP):
    # The tag Telegram's protocol requires for a dd-prefixed (random-padding)
    #  secret. TCP._obfuscated2_secret refuses to build a header for such a
    #  secret with any other tag, so this is what makes the class usable over
    #  both obfuscated2 schemes - classic MTProxy and WEB.
    OBFUSCATE_TAG = PADDED_INTERMEDIATE_OBFUSCATE_TAG

    def __init__(self, ipv6: bool = False, proxy=None, crypto_executor=None, loop: Optional[asyncio.AbstractEventLoop] = None, dc_id: Optional[int] = None):
        super().__init__(ipv6, proxy, crypto_executor, loop, dc_id)

    async def connect(self, address: tuple):
        await super().connect(address)

        if not self.opens_with_obfuscated2_header:
            # The header already carries this tag where one was sent; see
            #  TCP.opens_with_obfuscated2_header.
            await super().send(PADDED_INTERMEDIATE_OBFUSCATE_TAG)

    async def send(self, data: bytes, *args):
        padding = os.urandom(random.randint(0, 15))
        await super().send(pack("<i", len(data) + len(padding)) + data + padding)

    async def recv(self, length: int = 0) -> Optional[bytes]:
        length = await super().recv(4)

        if length is None:
            return None

        total_len = unpack("<i", length)[0]
        payload_plus_padding = await super().recv(total_len)

        if payload_plus_padding is None:
            return None

        return strip_padding(payload_plus_padding)

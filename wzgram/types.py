"""Static type-checker alias for :mod:`pyrogram.types`.

wzgram.types is :mod:`pyrogram.types` at runtime, served by the meta-path
finder in `wzgram/__init__.py`. This file exists only so static analyzers can
resolve `from wzgram.types import ...`; it is never executed.
"""

from pyrogram.types import *

"""Static type-checker alias for :mod:`pyrogram.errors`.

wzgram.errors is :mod:`pyrogram.errors` at runtime, served by the meta-path
finder in `wzgram/__init__.py`. This file exists only so static analyzers can
resolve `from wzgram.errors import ...`; it is never executed.
"""

from pyrogram.errors import *

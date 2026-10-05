"""Static type-checker alias for :mod:`pyrogram.handlers`.

wzgram.handlers is :mod:`pyrogram.handlers` at runtime, served by the meta-path
finder in `wzgram/__init__.py`. This file exists only so static analyzers can
resolve `from wzgram.handlers import ...`; it is never executed.
"""

from pyrogram.handlers import *

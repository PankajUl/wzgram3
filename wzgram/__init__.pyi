"""Static type-checker alias for :mod:`pyrogram`.

``import wzgram`` resolves to :mod:`pyrogram` at runtime via the meta-path
finder in ``wzgram/__init__.py``. This stub exists only so static analyzers
(Pyright, Pylance, mypy) can resolve ``from wzgram import ...``; it is never
executed.
"""

from pyrogram import *

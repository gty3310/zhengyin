"""zhengyin — 古文正音 CLI.

Given a classical Chinese text (optionally with per-character pinyin),
produce a TTS-safe version in which every character, read at its dictionary
default reading, sounds the intended reading — with meaning preserved
wherever the dictionary carries a sense-keyed mapping （通假/破读）.
Unresolvable characters are never guessed: they become structured
questions for a human, routed through the calling AI agent.
"""

__version__ = "0.4.0"

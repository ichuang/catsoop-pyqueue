"""catsoop-pyqueue: a lightweight pure-Python port of the CAT-SOOP Queue.

No external dependencies: the web/WebSocket server is built on asyncio,
and the RethinkDB database is replaced by an in-process store that
persists to a JSON file and emits change events (standing in for
RethinkDB changefeeds).
"""

__version__ = '0.1.0'

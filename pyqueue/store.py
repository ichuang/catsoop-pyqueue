"""In-process queue storage: the RethinkDB replacement.

Documents live in a dict keyed by ``username`` (the primary key the
original ``queue`` table used).  Every mutation:

  * optionally persists the whole table to a JSON file (atomic
    write-then-rename), and
  * notifies change listeners with ``(old_doc, new_doc)`` pairs —
    the stand-in for RethinkDB changefeeds that drove the real-time
    'edit' broadcasts in the Node.js server.

The server runs in a single asyncio event loop and all mutations are
synchronous, so no locking is needed.
"""

import copy
import json
import os
import tempfile

from . import util


class Store:
    def __init__(self, path=None):
        self.path = path
        self.docs = {}
        self.listeners = []
        if path and os.path.exists(path):
            with open(path) as f:
                self.docs = json.load(f)

    def on_change(self, listener):
        """Register a callback invoked as listener(old_doc, new_doc)."""
        self.listeners.append(listener)

    def _persist(self):
        if not self.path:
            return
        os.makedirs(os.path.dirname(self.path) or '.', exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(self.path) or '.')
        try:
            with os.fdopen(fd, 'w') as f:
                json.dump(self.docs, f, indent=1)
            os.replace(tmp, self.path)
        except BaseException:
            os.unlink(tmp)
            raise

    def _emit(self, old, new):
        self._persist()
        for listener in self.listeners:
            listener(copy.deepcopy(old), copy.deepcopy(new))

    def get(self, username):
        doc = self.docs.get(username)
        return copy.deepcopy(doc) if doc is not None else None

    def all(self):
        return [copy.deepcopy(doc) for doc in self.docs.values()]

    def filter(self, pattern):
        return [copy.deepcopy(doc) for doc in self.docs.values()
                if util.matches(doc, pattern)]

    def insert(self, doc, conflict=None):
        """Insert ``doc``; on primary-key conflict call
        ``conflict(old_doc, new_doc)`` to produce the merged document
        (mirroring RethinkDB's insert with a conflict resolver)."""
        username = doc['username']
        old = self.docs.get(username)
        if old is not None and conflict is not None:
            new = conflict(copy.deepcopy(old), copy.deepcopy(doc))
        else:
            new = copy.deepcopy(doc)
        if new == old:
            return
        self.docs[username] = new
        self._emit(old, new)

    def replace(self, username, fn):
        """Replace the doc via ``fn(doc) -> doc | None``.

        Returning the document unchanged is a no-op (no change event);
        returning None deletes it — the same semantics the original
        code got from ``db.branch(...)`` inside ``replace``.
        """
        old = self.docs.get(username)
        if old is None:
            return
        new = fn(copy.deepcopy(old))
        if new == old:
            return
        if new is None:
            del self.docs[username]
        else:
            self.docs[username] = new
        self._emit(old, new)

    def delete_where(self, pattern):
        """Delete every doc matching ``pattern``, emitting each change."""
        doomed = [u for u, doc in self.docs.items()
                  if util.matches(doc, pattern)]
        for username in doomed:
            old = self.docs.pop(username)
            self._emit(old, None)

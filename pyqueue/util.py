"""Small helpers shared across the queue server (port of server/util.js)."""

import copy
import hashlib
from datetime import datetime, timezone


def hash_username(username):
    """Anonymize a username the same way the Node.js server did."""
    return hashlib.sha512(username.encode('utf-8')).hexdigest()


def now_iso():
    """Current UTC time as an ISO-8601 string (sorts lexicographically)."""
    return datetime.now(timezone.utc).isoformat()


def deep_merge(base, extras):
    """Recursively merge ``extras`` into a copy of ``base``.

    Mirrors RethinkDB's ``merge``: dicts merge recursively, any other
    value in ``extras`` replaces the one in ``base``.
    """
    result = copy.deepcopy(base)
    for key, value in extras.items():
        if isinstance(result.get(key), dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def without(doc, *paths):
    """Return a copy of ``doc`` with the given key paths removed.

    A path is either a top-level key ('date_added') or a tuple naming a
    nested key (('data', 'claimant')).  Mirrors RethinkDB's ``without``.
    """
    result = copy.deepcopy(doc)
    for path in paths:
        if isinstance(path, str):
            path = (path,)
        target = result
        for key in path[:-1]:
            target = target.get(key)
            if not isinstance(target, dict):
                target = None
                break
        if isinstance(target, dict):
            target.pop(path[-1], None)
    return result


def matches(doc, pattern):
    """RethinkDB-style filter match: nested dicts match partially."""
    for key, value in pattern.items():
        if isinstance(value, dict):
            sub = doc.get(key)
            if not isinstance(sub, dict) or not matches(sub, value):
                return False
        elif doc.get(key) != value:
            return False
    return True

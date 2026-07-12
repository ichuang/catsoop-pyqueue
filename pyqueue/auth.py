"""Authentication and queue permissions (port of server/authentication.js).

Two auth schemes are supported:

* Signed (current CAT-SOOP): the queue plugin's post_load.py signs
  cs_user_info with HMAC-SHA256 keyed by sha256(api_token).hexdigest()
  for each API token of the queue user, and puts the blob in
  catsoop.plugins.queue.auth.  We verify it locally against
  CATSOOP.TOKEN — no API call needed.

* Legacy: the client sends its own api_token and we ask the CAT-SOOP
  API (get_user_information) who it is.
"""

import base64
import hashlib
import hmac
import json
import time

# The original used a fall-through switch: each role gets its own
# permissions plus everything the roles below it get.
_ROLE_LADDER = [
    ({'Admin', 'Instructor', 'TA'}, {'clear'}),
    ({'UTA'}, {'lock', 'show_claimed', 'check_in', 'auto_check_in'}),
    ({'LA'}, {'notifications'}),
    ({'SLA'}, {'queue_view_all', 'claim', 'checkoff'}),
]

STAFF_ROLES = {'Admin', 'Instructor', 'TA', 'UTA'}


def queue_permissions(user):
    permissions = set()
    role = (user or {}).get('role')
    matched = False
    for roles, perms in _ROLE_LADDER:
        matched = matched or role in roles
        if matched:
            permissions |= perms
    return permissions


def is_staff(user):
    return bool(user) and user.get('role') in STAFF_ROLES


def verify_signed_auth(blob, tokens, max_age=None):
    """Verify a signed auth blob from the queue plugin's post_load.py.

    ``blob`` is base64(json({payload, verifiers})) where payload is
    base64(json(cs_user_info + queue_timestamp)) and each verifier is
    hmac_sha256(key=sha256(token).hexdigest(), msg=payload).  Returns
    the decoded cs_user_info dict; raises ValueError if no configured
    token verifies (or the timestamp is older than ``max_age`` seconds).
    """
    outer = json.loads(base64.b64decode(blob))
    payload = outer['payload'].encode('utf-8')
    verifiers = [str(v) for v in outer.get('verifiers', [])]

    for token in tokens:
        if not token:
            continue
        key = hashlib.sha256(token.encode('ascii')).hexdigest().encode('ascii')
        expected = hmac.new(key, payload, hashlib.sha256).hexdigest()
        if any(hmac.compare_digest(expected, v) for v in verifiers):
            break
    else:
        raise ValueError('auth blob not signed with a known token')

    user = json.loads(base64.b64decode(payload))
    if max_age is not None:
        age = time.time() - float(user.get('queue_timestamp', 0))
        if age > max_age:
            raise ValueError('auth blob expired')
    if not user.get('username'):
        raise ValueError('auth blob has no username')
    return user


async def validate_auth(catsoop, auth, max_age=None):
    """Validate an auth message.

    A message with an 'auth' field carries the plugin's signed blob and
    is verified locally; otherwise the legacy api_token flow asks the
    CAT-SOOP API.  If the app has no CATSOOP.API_ROOT configured (dev
    mode), signed blobs are decoded without verification and plain
    {username, role, name} messages are trusted directly.

    Returns the user dict with a 'permissions' set attached; raises on
    invalid auth.
    """
    if auth.get('auth'):
        if catsoop.token:
            user = verify_signed_auth(auth['auth'], [catsoop.token], max_age)
        elif not catsoop.api_root:
            # dev mode without a token: decode, but can't verify
            outer = json.loads(base64.b64decode(auth['auth']))
            user = json.loads(base64.b64decode(outer['payload']))
        else:
            raise ValueError('no CATSOOP.TOKEN configured to verify auth')
    elif catsoop.api_root:
        res = await catsoop.post('get_user_information', auth)
        user = res['user_info']
    else:
        if not auth.get('username'):
            raise ValueError('username required')
        user = {
            'username': auth['username'],
            'name': auth.get('name', auth['username']),
            'role': auth.get('role', 'Student'),
        }
    user['permissions'] = set(user.get('permissions') or []) | queue_permissions(user)
    return user

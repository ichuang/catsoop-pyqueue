"""Queue configuration (port of config/params.js).

Edit the PARAMS dict to fit your deployment.  Optional local overrides
go in config/dev_params.py (a module defining a PARAMS dict with the
same shape); it is merged over these defaults, exactly as
dev_params.js was in the Node.js version.
"""

import copy
import os

try:
    from . import passwords
except ImportError:
    passwords = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

PARAMS = {
    'SERVER': {
        # The interface and port the queue server binds to.
        'HOST': '127.0.0.1',
        'PORT': 3100,
    },

    'STORE': {
        # Where the queue state is persisted (replaces RethinkDB).
        # Set to None to keep the queue in memory only.
        'PATH': os.path.join(BASE_DIR, 'data', 'queue.json'),
    },

    'CATSOOP': {
        # The API token for the queue user; set it in config/passwords.py.
        'TOKEN': getattr(passwords, 'catsoop', None),

        # The publicly accessible URL for the API root of your CAT-SOOP
        # instance.  Set to None for dev mode: authentication then
        # trusts the client-supplied {username, role, name} directly.
        # Only used for the legacy api_token auth flow and group
        # lookups; the current signed-auth flow (catsoop.plugins
        # .queue.auth from the plugin's post_load.py) is verified
        # locally against TOKEN with no API call.
        # 'API_ROOT': 'https://introml.odl.mit.edu/cat-soop/_util/api',
        'API_ROOT': 'http://localhost:7667/_util/api',

        # Maximum accepted age (seconds) of a signed auth blob's
        # queue_timestamp, or None to accept any age.  Note that
        # long-lived pages re-send the same blob when reconnecting, so
        # a small value will log users out on reconnect.
        'AUTH_MAX_AGE': None,
    },

    # Set this to True to require staff to check in before claiming.
    'STAFF_CHECK_IN_REQUIRED': False,

    # The publicly accessible URL of the queue (the reverse-proxy route).
    'URL_ROOT': '/queue/fall18/',

    # The rooms this queue covers; at least one name.  Every room a
    # CAT-SOOP page may pass as queue_room must be listed here, or
    # authentication for that page fails with "bad room".  'default' is
    # where the course's root preload.py (queue_room = 'default') sends
    # student lab/checkoff entries; the others are the course's
    # queue/<room> staff pages.
    'ROOMS': ['default', '34-501', '32-044', 'Virtual', 'Office_Hours',
              'Backup'],

    # Print logs to the console in addition to the log files.
    'PRINT_LOGS': True,

    # Where log files (info.log / warn.log / error.log) are written.
    'LOG_DIR': os.path.join(BASE_DIR, 'logs'),

    # The static files served at '/' (the demo frontend + JS client).
    'WWW_ROOT': os.path.join(BASE_DIR, 'www'),
}


def _merge(base, extras):
    for key, value in extras.items():
        if isinstance(base.get(key), dict) and isinstance(value, dict):
            _merge(base[key], value)
        else:
            base[key] = value


def load(overrides=None):
    """Return a fresh params dict: defaults + dev_params + overrides."""
    params = copy.deepcopy(PARAMS)
    try:
        from . import dev_params
        _merge(params, dev_params.PARAMS)
    except ImportError:
        pass
    if overrides:
        _merge(params, overrides)
    return params

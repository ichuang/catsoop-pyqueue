catsoop-pyqueue
===============

A lightweight, pure-Python port of the [CAT-SOOP Queue](../catsoop-queue)
(the Node.js/Express/socket.io/RethinkDB web application that lets
students in a lab request help or checkoffs in an orderly fashion).

**No dependencies at all** — Python 3.9+ standard library only:

| Node.js version                | Python version                                    |
|--------------------------------|---------------------------------------------------|
| Express + socket.io            | asyncio server with a hand-rolled RFC 6455 WebSocket + static-file layer ([pyqueue/websocket.py](pyqueue/websocket.py)) |
| RethinkDB + changefeeds        | in-process store persisted to a JSON file; mutations emit change events that drive the same real-time broadcasts ([pyqueue/store.py](pyqueue/store.py)) |
| `server/queue.js` / `index.js` | [pyqueue/queue_server.py](pyqueue/queue_server.py) |
| `server/entry_types.js`        | [pyqueue/entries.py](pyqueue/entries.py)          |
| `server/authentication.js`     | [pyqueue/auth.py](pyqueue/auth.py)                |
| `server/catsoop.js`            | [pyqueue/catsoop.py](pyqueue/catsoop.py)          |
| `imports/client.js`            | [pyqueue/client.py](pyqueue/client.py) (Python) and [www/js/client.js](www/js/client.js) (browser) |
| `www/js/queue.js` + `view.js` + Ractive templates | [www/js/queue.js](www/js/queue.js) — vanilla-JS frontend, no jQuery/Ractive/moment/Mousetrap |
| `www/scss/queue.scss` (+ Bootstrap) | [www/css/queue.css](www/css/queue.css) — plain CSS, no build step |
| `config/params.js`             | [config/params.py](config/params.py)             |

Quick start (no CAT-SOOP needed)
--------------------------------

```
cd catsoop-pyqueue
python3 -m pyqueue --dev
```

then open <http://127.0.0.1:3100/> for a small demo frontend.  In
`--dev` mode authentication trusts the username/role you type in, so
you can open two browser windows (one Student, one TA) and watch
entries appear, get claimed, etc. in real time.

Running against CAT-SOOP
------------------------

1. Create an API token for a queue user exactly as described in the
   original [catsoop-queue README](../catsoop-queue/README.md) step 4,
   and put it in `config/passwords.py`:

   ```python
   catsoop = 'YOUR_QUEUE_USER_CATSOOP_API_TOKEN'
   ```

2. Edit `config/params.py` (`CATSOOP.API_ROOT`, `ROOMS`, `SERVER.PORT`,
   ...).  Local overrides can go in `config/dev_params.py` as a
   `PARAMS` dict, which is merged over the defaults.

3. Run `python3 -m pyqueue` and reverse-proxy it as before (the
   WebSocket endpoint is `/ws`; make sure your proxy passes `Upgrade`
   headers).

Wire protocol
-------------

socket.io is replaced with plain JSON text frames over one WebSocket,
mirroring socket.io's emit-with-acknowledgement semantics:

```
client -> server   {"event": "add", "data": {...}, "id": 7}
server -> client   {"ack": 7, "data": {"success": true}}      (reply)
server -> client   {"event": "edit", "data": {...}}           (push)
```

The events and their payloads are unchanged from the Node.js version:
`authenticate`, `get_all`, `add`, `action` (claim / disclaim / remove /
single_checkoff / group_checkoff), `lock`, `unlock`, `get_locked`,
`clear`, `get_staff_list`, `check_in`, `check_out`; server pushes are
`edit`, `locked`, and `staff_list`.  `www/js/client.js` exposes the same
`Client` API the old frontend imported (`login`, `add`, `get_entries`,
`action`, ...) and handles reconnect/re-auth.

The frontend (queue.js)
-----------------------

`www/js/queue.js` (served at `<url_root>/js/queue.js`) is a
dependency-free port of the original frontend bundle: the staff table
with claim/disclaim/checkoff buttons and keyboard shortcuts (`c` to
claim first, `d`/`r`/`g`/`s` on a claim), the claim view with the
running timer and student cards, the student static and popup views,
the staff check-in list, sound + toast notifications, and the
locked/disconnected banners.

Like the original, it bootstraps itself from the globals a CAT-SOOP
page defines (`catsoop.plugins.queue = {url_root, is_staff, container,
view, room, auth}`, where `url_root` may be a path or an absolute URL),
exposes `window.queue` with the same `get`/`set`/`observe`/`add` API
the plugin-generated buttons call, and works with the plugin's single
`<script src=".../js/queue.js">` tag (it loads `client.js` by itself,
and links `<url_root>/css/queue.css` unless the page — e.g. the
CAT-SOOP plugin's `<link>` tag — already did).  The stylesheet ports
the original `queue.scss` (entry-type row colors, claimed highlight,
popup positioning) plus the component styles the original got from
Bootstrap; edit it to restyle the queue.
Optional `window.queue_www_params = {SHOW_STAFF_LIST, get_photo_url,
get_audio_url}` replaces the old `config/www_params.js`.

To try it without CAT-SOOP, run the server with `--dev` and open
`www/test_queue.html`, which stubs the `catsoop` globals:

```
http://127.0.0.1:3100/test_queue.html?username=ta1&role=TA&view=staff_view
http://127.0.0.1:3100/test_queue.html?username=alice&role=Student&view=student_static
```

(views: `staff_view`, `student_static`, `student_popup`; add
`&signed=1&token=...` to exercise the signed-auth flow described below)

Authentication
--------------

Current CAT-SOOP no longer exposes `catsoop.api_token` to pages.
Instead the queue plugin's `post_load.py` signs `cs_user_info` (plus a
`queue_timestamp`) with HMAC-SHA256, keyed by
`sha256(api_token).hexdigest()` for each API token of the queue user,
and puts the resulting blob in `catsoop.plugins.queue.auth`.  The
frontend passes that string through unchanged, and the server verifies
it **locally** against `CATSOOP.TOKEN` (pyqueue/auth.py:
`verify_signed_auth`) — no CAT-SOOP API call is needed to log in.
Set `CATSOOP.AUTH_MAX_AGE` (seconds) to reject stale blobs; the default
`None` accepts any age, since long-lived pages re-send the same blob on
reconnect.

Two other auth forms still work: the legacy flow (client sends its own
`api_token`; the server asks the CAT-SOOP API `get_user_information`),
and dev mode (`--dev` with no token configured trusts a client-supplied
`{username, role}`).

Storage
-------

Queue state lives in memory and is written atomically to
`data/queue.json` on every change (configurable via `STORE.PATH`;
set to `None` for memory-only).  Restarting the server picks the queue
back up from that file.  At lab-queue scale (tens of entries) this is
plenty, and there is no database process to babysit.

Intentional differences from the Node.js version
------------------------------------------------

* Checkoff group members are only included in an entry rendered for
  users allowed to see that entry (the original attached the group's
  real usernames even to the anonymized rendering).
* `action` messages only dispatch to the entry's declared action
  methods, rather than any attribute name.
* Reconnection/re-auth is handled inside `www/js/client.js` (socket.io
  used to provide it).

Tests
-----

```
cd catsoop-pyqueue
python3 -m unittest discover -s test -v
```

The tests start a real server on an ephemeral port, connect real
WebSocket clients, and authenticate against a mock CAT-SOOP API (a port
of `test/catsoop.js`), covering authentication/permissions, per-role
visibility and anonymization, claim/disclaim/remove semantics,
locking, clearing, the staff list, group and single checkoffs, re-add
merging, and persistence across restarts.

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

1. Set up the queue user (`__queue_user__`) and its API token — see
   "Setting up the queue user and API token" below — and put the token
   in `config/passwords.py`:

   ```python
   catsoop = 'YOUR_QUEUE_USER_CATSOOP_API_TOKEN'
   ```

2. Edit `config/params.py` (`CATSOOP.API_ROOT`, `ROOMS`, `SERVER.PORT`,
   ...).  Local overrides can go in `config/dev_params.py` as a
   `PARAMS` dict, which is merged over the defaults.

3. Run `python3 -m pyqueue` and reverse-proxy it — see "Proxying
   behind nginx" below.

Proxying behind nginx
---------------------

In production the queue server sits behind nginx on the same host as
CAT-SOOP, reachable under a path prefix such as `/queue`.  Add a
location block to your nginx config (alongside the one that proxies
CAT-SOOP itself):

```nginx
location /queue/ {
    proxy_http_version 1.1;
    proxy_set_header Upgrade $http_upgrade;
    proxy_set_header Connection "upgrade";
    proxy_set_header Host $host;
    proxy_set_header X-Real-IP $remote_addr;
    proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
    proxy_cache_bypass $http_upgrade;
    proxy_pass http://localhost:3100;
}
```

Notes:

* The three `Upgrade`/`Connection`/`proxy_http_version` lines are
  required — the queue's real-time updates run over a WebSocket at
  `<url_root>/ws`, and without them nginx silently downgrades the
  upgrade request and every client shows the "Disconnected!" banner.

* With `proxy_pass http://localhost:3100;` (no trailing slash or path),
  nginx forwards the `/queue` prefix through to the queue server.
  That works out of the box: the server strips prefixes listed in
  `URL_PREFIXES` in `config/params.py` (default `['/queue']`) before
  resolving static files, and accepts the WebSocket upgrade on any
  path.  If your prefix is something else (say `/help-queue`), either
  add it to `URL_PREFIXES`, or make nginx strip it by using a trailing
  slash on both directives (`location /help-queue/ { ...
  proxy_pass http://localhost:3100/; }`).

* Set `queue_url_root` in the CAT-SOOP plugin's `post_load.py` (and
  `URL_ROOT` in `config/params.py`) to the public prefix — e.g.
  `/queue` — so pages load `<prefix>/js/queue.js`,
  `<prefix>/css/queue.css`, and connect to `ws(s)://<host><prefix>/ws`.
  With a path prefix like this, the frontend automatically picks `ws:`
  or `wss:` to match the page, so the queue works unchanged when the
  site is served over HTTPS.

* Keep the queue server bound to localhost (`SERVER.HOST =
  '127.0.0.1'`, the default) so it is only reachable through nginx.

* To sanity-check the proxy: `curl -i https://your-host/queue/js/queue.js`
  should return 200, and a request for a missing file (e.g.
  `/queue/js/nope.js`) shows up in the queue server's `logs/warn.log`
  with the exact URL received — if the logged path still contains an
  unexpected prefix, add it to `URL_PREFIXES`.

Setting up the queue user and API token
---------------------------------------

The queue authenticates users and submits checkoffs on behalf of a
service account in your course, conventionally named `__queue_user__`.
Two pieces of setup are needed, and missing either one produces a
confusing half-working state:

**1. An API token registered to `__queue_user__`.**  This token is the
shared secret between CAT-SOOP and the queue server: the queue plugin's
`post_load.py` signs each user's identity with it (via
`csm_api.get_api_tokens(globals(), "__queue_user__")`), the queue
server verifies those signatures with the copy in
`config/passwords.py`, and the checkoff question type resolves the
token back to `__queue_user__` when a checkoff is submitted.  The token
must therefore actually be *registered in CAT-SOOP's token store* —
just inventing a string and putting the same value in both configs lets
logins work but breaks checkoffs, because CAT-SOOP's
`userinfo_from_token` won't know who owns it.

The cleanest way to register one is an admin-only CAT-SOOP page.  Drop
something like this in your course (e.g. as a `<python>` block on a
staff-only page, or a `queue/get_token` page), load it once as an
Admin, and copy the printed token into `config/passwords.py`:

```python
# Generate and register an API token for the queue user (__queue_user__).
# Admin-only.  Prints the token that goes in catsoop-pyqueue's
# config/passwords.py.

import secrets
import string

if cs_user_info.get("role") != "Admin":
    print("Sorry, only Admins may generate queue user API tokens.")
else:
    api_tokens = csm_api.get_api_tokens(globals(), "__queue_user__")
    if api_tokens:
        print("queue user api_tokens =", api_tokens)
    else:
        alphabet = string.ascii_letters + string.digits
        tok = "".join(secrets.choice(alphabet) for _ in range(40))

        # The same two store writes as catsoop.api.new_api_token, but
        # with our own token value: register the token -> username
        # mapping and add the token to the user's token list.
        csm_cslog.overwrite_log("_api_tokens", [], tok, "__queue_user__")
        csm_cslog.update_log("_api_users", [], "__queue_user__", tok)

        print("new token for __queue_user__:", tok)
```

(Alternatively, `csm_api.initialize_api_token(globals(), {'username':
'__queue_user__', 'name': 'Queue User', 'email': 'x@x'})` registers and
returns a CAT-SOOP-generated random token.)

**2. Appropriate privileges for `__queue_user__`, usually via a
role.**  The built-in `checkoff` question type only accepts a checkoff
when the submitting token's owner has the `impersonate` permission (and
the staff member named in the submission has `checkoff`).  Give the
queue user a role that carries those permissions in your course's
`cs_permissions` — TA is typical — by creating
`$COURSE/__USERS__/__queue_user__.py`:

```python
role = 'TA'
full_name = 'Queue User'
```

Without this, staff can claim entries and click "checkoff", but the
CAT-SOOP page rejects the submission with "You must receive this
checkoff from a staff member."

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

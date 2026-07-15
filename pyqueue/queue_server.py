"""The queue application (port of server/queue.js and server/index.js).

One asyncio server handles both static files (the ``www/`` directory)
and WebSocket connections (any request with an ``Upgrade: websocket``
header).  Each socket must first send an ``authenticate`` event; after
that the same events the Node.js version supported are available:

    get_all, add, action, lock, unlock, get_locked, clear,
    get_staff_list, check_in, check_out

Store change events (the changefeed replacement) drive per-user
rendered 'edit' broadcasts to every socket in the affected room.
"""

import asyncio

from . import auth as authentication
from . import util
from .catsoop import Catsoop
from .entries import ENTRY_TYPES, Entry
from .log import make_logger
from .store import Store
from .websocket import (WebSocket, ConnectionClosed, read_http_head,
                        websocket_handshake, serve_static)


class QueueApp:
    def __init__(self, params):
        self.params = params
        self.log = make_logger(params)
        self.store = Store(params.get('STORE', {}).get('PATH'))
        self.catsoop = Catsoop(params['CATSOOP'].get('API_ROOT'),
                               params['CATSOOP'].get('TOKEN'))
        self.server = None
        self.port = None
        self._connections = set()  # live WebSockets, closed on stop()

        rooms = params['ROOMS']
        self.SOCKETS = {room: {} for room in rooms}
        self.USERS = {}
        self.LOCKS = {room: False for room in rooms}
        self.STAFF_SETS = {room: {'confirmed': set(), 'unconfirmed': set()}
                           for room in rooms}
        self.ENTRIES = {room: {} for room in rooms}

        # Load the current state of the queue from the store.
        for doc in self.store.all():
            try:
                self.ENTRIES[doc['room']][doc['username']] = self.make_entry(doc)
            except Exception as err:
                self.log.info('[queue init] err %r doc=%r', err, doc)
                continue
            claimant = doc['data'].get('claimant')
            if claimant:
                self.USERS[claimant] = {'claims': {doc['username']}}

        self.store.on_change(self._on_change)

    def make_entry(self, doc):
        return ENTRY_TYPES[doc['type']](doc, self)

    ## Broadcasting

    async def broadcast(self, room, name, content):
        for sockets in list(self.SOCKETS.get(room, {}).values()):
            for ws in list(sockets):
                try:
                    await ws.send_json({'event': name, 'data': content})
                except Exception:
                    pass

    ## Staff list functions

    async def remove_from_all_rooms(self, username):
        for room, staff in self.STAFF_SETS.items():
            staff['unconfirmed'].discard(username)
            staff['confirmed'].discard(username)
            await self.broadcast(room, 'staff_list', {
                'checked_in': [], 'logged_in': [], 'removed': [username],
            })

    async def log_in(self, username, room):
        await self.remove_from_all_rooms(username)
        self.STAFF_SETS[room]['unconfirmed'].add(username)
        await self.broadcast(room, 'staff_list', {
            'checked_in': [], 'logged_in': [username], 'removed': [],
        })

    async def check_in(self, username, room):
        await self.remove_from_all_rooms(username)
        self.STAFF_SETS[room]['unconfirmed'].discard(username)
        self.STAFF_SETS[room]['confirmed'].add(username)
        if username in self.USERS:
            self.USERS[username]['confirmed'] = True
        await self.broadcast(room, 'staff_list', {
            'checked_in': [username], 'logged_in': [], 'removed': [],
        })

    async def check_out(self, username, room):
        await self.remove_from_all_rooms(username)
        self.STAFF_SETS[room]['confirmed'].discard(username)
        if username in self.USERS:
            self.USERS[username]['confirmed'] = False
        await self.broadcast(room, 'staff_list', {
            'checked_in': [], 'logged_in': [], 'removed': [username],
        })

    ## Store change handling (the changefeed replacement)

    def _on_change(self, old, new):
        # Track claims globally, like the unfiltered changefeed did.
        if old and old['data'].get('claimant') in self.USERS:
            user = self.USERS[old['data']['claimant']]
            user.setdefault('claims', set()).discard(old['username'])
        if new and new['data'].get('claimant') in self.USERS:
            user = self.USERS[new['data']['claimant']]
            user.setdefault('claims', set()).add(new['username'])

        # Work out which rooms see which change.  A document moving
        # between rooms looks like a delete in the old room and an add
        # in the new one (matching the per-room changefeeds).
        events = []
        if new is None:
            entry = (self.ENTRIES.get(old['room'], {}).pop(old['username'], None)
                     or self.make_entry(old))
            events.append((old['room'], 'deleted', entry))
        elif old is None or old['room'] != new['room']:
            if old is not None:
                old_entry = (self.ENTRIES.get(old['room'], {}).pop(old['username'], None)
                             or self.make_entry(old))
                events.append((old['room'], 'deleted', old_entry))
            entry = self.make_entry(new)
            self.ENTRIES.setdefault(new['room'], {})[new['username']] = entry
            events.append((new['room'], 'added', entry))
        else:
            entry = self.ENTRIES[new['room']].get(new['username'])
            if entry is None:
                entry = self.make_entry(new)
                self.ENTRIES[new['room']][new['username']] = entry
            else:
                entry.update(new)
            events.append((new['room'], 'edited', entry))

        for room, kind, entry in events:
            for username, sockets in list(self.SOCKETS.get(room, {}).items()):
                user = self.USERS.get(username, {})
                asyncio.ensure_future(
                    self._send_edit(list(sockets), user, kind, entry))

    async def _send_edit(self, sockets, user, kind, entry):
        msg = {'added_entries': [], 'edited_entries': [], 'deleted_usernames': []}
        try:
            if kind == 'deleted':
                msg['deleted_usernames'] = [
                    entry.username if entry.visible_to(user)
                    else util.hash_username(entry.username)
                ]
            else:
                rendered = await entry.render(user, self.USERS)
                key = 'added_entries' if kind == 'added' else 'edited_entries'
                msg[key] = [rendered]
        except Exception as err:
            self.log.error('unable to render edit: %r', err)
            return
        for ws in sockets:
            try:
                await ws.send_json({'event': 'edit', 'data': msg})
            except Exception:
                pass

    ## Socket handling

    async def _handle_connection(self, reader, writer):
        try:
            request_line, headers = await read_http_head(reader)
        except Exception:
            writer.close()
            return
        try:
            target = request_line.split(' ')[1]
        except IndexError:
            writer.close()
            return

        if headers.get('upgrade', '').lower() == 'websocket':
            try:
                await websocket_handshake(writer, headers)
            except Exception:
                writer.close()
                return
            ws = WebSocket(reader, writer)
            self._connections.add(ws)
            try:
                await self._handle_socket(ws)
            except Exception as err:
                self.log.error('socket handler error: %r', err)
            finally:
                # Close the transport deterministically: on Python
                # 3.12.1+ Server.wait_closed() waits for every accepted
                # connection to be closed, so relying on GC to close
                # abandoned sockets makes stop() hang.
                self._connections.discard(ws)
                await ws.close()
        else:
            try:
                status = await serve_static(writer, self.params['WWW_ROOT'],
                                            self._strip_prefix(target))
                if not status.startswith('200'):
                    method = request_line.split(' ')[0]
                    self.log.warning(
                        'static request failed: %s -- %s %s (WWW_ROOT=%s)',
                        status, method, target, self.params['WWW_ROOT'])
            except Exception as err:
                self.log.error('error serving %r: %r', target, err)
            writer.close()

    def _strip_prefix(self, target):
        """Drop a configured URL prefix (e.g. '/queue') from a request
        target, so the server answers both /queue/* and /* when proxied
        behind nginx without the prefix stripped."""
        path, sep, query = target.partition('?')
        for prefix in self.params.get('URL_PREFIXES') or []:
            prefix = prefix.rstrip('/')
            if prefix and (path == prefix or path.startswith(prefix + '/')):
                path = path[len(prefix):] or '/'
                break
        return path + sep + query

    async def _handle_socket(self, ws):
        self.log.debug('incoming socket connection')
        state = {'user': None, 'room': None}
        try:
            while True:
                try:
                    msg = await ws.recv_json()
                except ValueError:
                    continue
                event = msg.get('event')
                data = msg.get('data') or {}
                result = None
                try:
                    if event == 'authenticate':
                        result = await self._authenticate(ws, state, data)
                    elif state['user'] is None:
                        result = {'error': 'Not authenticated'}
                    else:
                        handler = self.HANDLERS.get(event)
                        if handler is None:
                            result = {'error': 'Unknown event: %r' % event}
                        else:
                            result = await handler(self, state['user'],
                                                   state['room'], data)
                except Exception as err:
                    self.log.error('error handling %r: %r', event, err)
                    result = None
                if msg.get('id') is not None:
                    await ws.send_json({'ack': msg['id'], 'data': result})
        except ConnectionClosed:
            pass
        finally:
            self.log.debug('disconnected socket')
            if state['user'] is not None:
                sockets = self.SOCKETS[state['room']].get(
                    state['user']['username'], [])
                if ws in sockets:
                    sockets.remove(ws)

    async def _authenticate(self, ws, state, msg):
        room = msg.get('room')
        if room not in self.SOCKETS:
            self.log.warning('failed authentication: bad room %r', room)
            return {'error': 'Invalid authentication'}
        try:
            user = await authentication.validate_auth(
                self.catsoop, msg,
                max_age=self.params['CATSOOP'].get('AUTH_MAX_AGE'))
        except Exception as err:
            self.log.warning('failed authentication: %r', err)
            return {'error': 'Invalid authentication'}

        self.log.info('successful authentication: %r', user.get('username'))
        username = user['username']

        if username in self.USERS:
            # Keep claims/confirmed from the old user data, but replace
            # everything else (preserving object identity, as the
            # original did with Object.assign).
            existing = self.USERS[username]
            user['claims'] = existing.get('claims', set())
            user['confirmed'] = existing.get('confirmed', False)
            existing.clear()
            existing.update(user)
            user = existing
        else:
            user['claims'] = set()
            user['confirmed'] = False
            self.USERS[username] = user

        # On re-authentication, drop the socket's previous registration
        # (possibly under another user/room) so it is never listed twice.
        if state['user'] is not None:
            old = self.SOCKETS[state['room']].get(state['user']['username'], [])
            if ws in old:
                old.remove(ws)

        state['user'] = user
        state['room'] = room
        sockets = self.SOCKETS[room].setdefault(username, [])
        if ws not in sockets:
            sockets.append(ws)

        if authentication.is_staff(user):
            if 'auto_check_in' in user['permissions']:
                await self.check_in(username, room)
            elif username not in self.STAFF_SETS[room]['confirmed']:
                await self.log_in(username, room)

        return {
            'username': username,
            'token': user.get('token'),
            'permissions': sorted(user['permissions']),
        }

    ## Event handlers (port of attach_authorized_handlers)

    async def _get_all(self, user, room, msg):
        docs = self.store.filter(dict(msg, room=room))
        docs.sort(key=lambda d: d['date_added'])
        entries = [self.make_entry(doc) for doc in docs]
        return [await entry.render(user, self.USERS) for entry in entries]

    async def _add(self, user, room, msg):
        self.log.info('new entry: user=%r msg=%r room=%r',
                      user.get('username'), msg, room)
        if self.LOCKS[room]:
            return {'success': False}

        entry_class = ENTRY_TYPES[msg['type']]
        data = entry_class.data_skeleton(msg.get('data') or {}, user)
        now = util.now_iso()

        def conflict(old_doc, new_doc):
            # If the rooms don't match, drop the claimant from both
            # sides before merging; never overwrite the original
            # date_added or entry state.
            if old_doc['room'] != new_doc['room']:
                old_doc = util.without(old_doc, ('data', 'claimant'))
                new_doc = util.without(new_doc, ('data', 'claimant'))
            new_doc = util.without(new_doc, 'date_added', ('data', 'state'))
            return util.deep_merge(old_doc, new_doc)

        self.store.insert({
            'username': user['username'],
            'type': msg['type'],
            'date_added': now,
            'last_modified': now,
            'data': data,
            'room': room,
        }, conflict=conflict)
        return {'success': True}

    async def _action(self, user, room, msg):
        self.log.info('entry action: user=%r msg=%r room=%r',
                      user.get('username'), msg, room)
        doc = self.store.get(msg.get('username'))
        if doc is None or doc['room'] != room:
            return None
        entry = self.ENTRIES[room].get(doc['username'])
        if entry is None:
            return None
        await entry.do_action(msg.get('action'), user)
        return None

    async def _lock(self, user, room, msg):
        self.log.info('lock queue: user=%r room=%r', user.get('username'), room)
        if 'lock' in user['permissions']:
            self.LOCKS[room] = True
            await self.broadcast(room, 'locked', True)
        return None

    async def _unlock(self, user, room, msg):
        self.log.info('unlock queue: user=%r room=%r', user.get('username'), room)
        if 'lock' in user['permissions']:
            self.LOCKS[room] = False
            await self.broadcast(room, 'locked', False)
        return None

    async def _get_locked(self, user, room, msg):
        return self.LOCKS[room]

    async def _clear(self, user, room, msg):
        self.log.info('clear queue: user=%r room=%r', user.get('username'), room)
        if 'clear' not in user['permissions']:
            return None
        self.store.delete_where({'room': room})
        return None

    async def _get_staff_list(self, user, room, msg):
        return {
            'confirmed': sorted(self.STAFF_SETS[room]['confirmed']),
            'unconfirmed': sorted(self.STAFF_SETS[room]['unconfirmed']),
        }

    async def _check_in(self, user, room, msg):
        self.log.info('check_in: user=%r msg=%r', user.get('username'), msg)
        if authentication.is_staff(self.USERS.get(msg.get('username'))):
            await self.check_in(msg['username'], room)
        return None

    async def _check_out(self, user, room, msg):
        self.log.info('check_out: user=%r msg=%r', user.get('username'), msg)
        if authentication.is_staff(self.USERS.get(msg.get('username'))):
            await self.check_out(msg['username'], room)
        return None

    HANDLERS = {
        'get_all': _get_all,
        'add': _add,
        'action': _action,
        'lock': _lock,
        'unlock': _unlock,
        'get_locked': _get_locked,
        'clear': _clear,
        'get_staff_list': _get_staff_list,
        'check_in': _check_in,
        'check_out': _check_out,
    }

    ## Lifecycle

    async def start(self):
        host = self.params['SERVER'].get('HOST', '127.0.0.1')
        port = self.params['SERVER'].get('PORT', 3100)
        self.server = await asyncio.start_server(
            self._handle_connection, host, port)
        self.port = self.server.sockets[0].getsockname()[1]
        self.log.info('listening on port %d', self.port)
        return self

    async def stop(self):
        if self.server is not None:
            self.server.close()
            # Close every live connection so their handlers finish;
            # wait_closed() (3.12.1+) waits for all of them.
            for ws in list(self._connections):
                await ws.close()
            await self.server.wait_closed()

    async def serve_forever(self):
        if self.server is None:
            await self.start()
        async with self.server:
            await self.server.serve_forever()

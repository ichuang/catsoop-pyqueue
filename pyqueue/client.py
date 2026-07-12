"""A Python queue client (port of imports/client.js).

Speaks the JSON-over-WebSocket protocol described in
pyqueue/websocket.py.  Used by the tests and handy for scripting.
"""

import asyncio

from .websocket import ws_connect, ConnectionClosed


class Client:
    def __init__(self, url, room='default'):
        """``url`` is the ws:// URL of the queue server."""
        self.url = url
        self.room = room
        self.username = None
        self.ws = None
        self._next_id = 0
        self._pending = {}
        self._listeners = {}
        self._recv_task = None

    async def connect(self):
        self.ws = await ws_connect(self.url)
        self._recv_task = asyncio.ensure_future(self._recv_loop())
        return self

    async def _recv_loop(self):
        try:
            while True:
                msg = await self.ws.recv_json()
                if 'ack' in msg:
                    future = self._pending.pop(msg['ack'], None)
                    if future is not None and not future.done():
                        future.set_result(msg.get('data'))
                elif 'event' in msg:
                    for callback in list(self._listeners.get(msg['event'], [])):
                        result = callback(msg.get('data'))
                        if asyncio.iscoroutine(result):
                            asyncio.ensure_future(result)
        except (ConnectionClosed, asyncio.CancelledError):
            for future in self._pending.values():
                if not future.done():
                    future.set_exception(ConnectionClosed())
            self._pending.clear()

    async def send(self, event, data=None):
        self._next_id += 1
        mid = self._next_id
        future = asyncio.get_event_loop().create_future()
        self._pending[mid] = future
        await self.ws.send_json({'event': event, 'data': data or {}, 'id': mid})
        return await future

    def recv(self, event, callback):
        self._listeners.setdefault(event, []).append(callback)

    def stop_recv(self, event, callback):
        listeners = self._listeners.get(event, [])
        if callback in listeners:
            listeners.remove(callback)

    async def close(self):
        if self._recv_task is not None:
            self._recv_task.cancel()
            try:
                await self._recv_task
            except asyncio.CancelledError:
                pass
        if self.ws is not None:
            await self.ws.close()

    # Convenience wrappers matching imports/client.js

    async def login(self, auth_data):
        result = await self.send('authenticate', dict({'room': self.room}, **auth_data))
        if result and not result.get('error'):
            self.username = result['username']
        return result

    async def is_locked(self):
        return await self.send('get_locked')

    async def lock(self):
        return await self.send('lock')

    async def unlock(self):
        return await self.send('unlock')

    async def get_entries(self, filter=None):
        return await self.send('get_all', filter or {})

    async def add(self, type, data):
        return await self.send('add', {'type': type, 'data': data})

    async def remove(self):
        return await self.send('action', {'action': 'remove',
                                          'username': self.username})

    async def clear(self):
        return await self.send('clear')

    async def action(self, type, data):
        return await self.send('action', dict({'action': type}, **data))

    async def get_staff_list(self):
        return await self.send('get_staff_list')

    async def check_in(self, username):
        return await self.send('check_in', {'username': username})

    async def check_out(self, username):
        return await self.send('check_out', {'username': username})

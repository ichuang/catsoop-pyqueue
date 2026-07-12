"""Minimal HTTP + WebSocket (RFC 6455) support on asyncio streams.

This replaces Express + socket.io with pure stdlib code.  It provides:

  * ``read_http_head``   — parse an incoming HTTP request head
  * ``websocket_handshake`` — complete a server-side WS upgrade
  * ``WebSocket``        — frame codec (text frames carrying JSON)
  * ``ws_connect``       — a client-side connector (used by the Python
                           client and the tests)
  * ``serve_static``     — a tiny static-file responder for ``www/``

The wire protocol carried over the WebSocket mirrors socket.io's
emit-with-ack semantics:

    client -> server   {"event": name, "data": {...}, "id": 7}
    server -> client   {"ack": 7, "data": <result>}          (reply)
    server -> client   {"event": name, "data": {...}}        (push)
"""

import asyncio
import base64
import hashlib
import json
import mimetypes
import os
import urllib.parse

WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

_OP_TEXT = 0x1
_OP_BIN = 0x2
_OP_CLOSE = 0x8
_OP_PING = 0x9
_OP_PONG = 0xA


class ConnectionClosed(Exception):
    pass


async def read_http_head(reader):
    """Read and parse an HTTP request/response head.

    Returns (first_line, headers) where headers is a lowercase-keyed dict.
    """
    data = await reader.readuntil(b'\r\n\r\n')
    lines = data.decode('latin-1').split('\r\n')
    headers = {}
    for line in lines[1:]:
        if not line:
            continue
        key, _, value = line.partition(':')
        headers[key.strip().lower()] = value.strip()
    return lines[0], headers


def accept_key(key):
    digest = hashlib.sha1((key + WS_GUID).encode('latin-1')).digest()
    return base64.b64encode(digest).decode('latin-1')


async def websocket_handshake(writer, headers):
    """Send the 101 Switching Protocols response for a WS upgrade."""
    key = headers.get('sec-websocket-key', '')
    writer.write((
        'HTTP/1.1 101 Switching Protocols\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Accept: %s\r\n'
        '\r\n' % accept_key(key)
    ).encode('latin-1'))
    await writer.drain()


class WebSocket:
    """A connected WebSocket.  ``mask=True`` for client-side sockets."""

    def __init__(self, reader, writer, mask=False):
        self.reader = reader
        self.writer = writer
        self.mask = mask
        self.closed = False
        self._send_lock = asyncio.Lock()

    async def _send_frame(self, opcode, payload):
        if self.closed:
            raise ConnectionClosed()
        header = bytearray([0x80 | opcode])
        length = len(payload)
        mask_bit = 0x80 if self.mask else 0
        if length < 126:
            header.append(mask_bit | length)
        elif length < 1 << 16:
            header.append(mask_bit | 126)
            header += length.to_bytes(2, 'big')
        else:
            header.append(mask_bit | 127)
            header += length.to_bytes(8, 'big')
        if self.mask:
            key = os.urandom(4)
            header += key
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        async with self._send_lock:
            self.writer.write(bytes(header) + bytes(payload))
            await self.writer.drain()

    async def _read_frame(self):
        try:
            head = await self.reader.readexactly(2)
            fin = bool(head[0] & 0x80)
            opcode = head[0] & 0x0F
            masked = bool(head[1] & 0x80)
            length = head[1] & 0x7F
            if length == 126:
                length = int.from_bytes(await self.reader.readexactly(2), 'big')
            elif length == 127:
                length = int.from_bytes(await self.reader.readexactly(8), 'big')
            key = await self.reader.readexactly(4) if masked else None
            payload = await self.reader.readexactly(length)
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            self.closed = True
            raise ConnectionClosed()
        if key:
            payload = bytes(b ^ key[i % 4] for i, b in enumerate(payload))
        return fin, opcode, payload

    async def recv_text(self):
        buf = bytearray()
        while True:
            fin, opcode, payload = await self._read_frame()
            if opcode == _OP_CLOSE:
                self.closed = True
                try:
                    await self._close_frame()
                except Exception:
                    pass
                raise ConnectionClosed()
            if opcode == _OP_PING:
                await self._send_frame(_OP_PONG, payload)
                continue
            if opcode == _OP_PONG:
                continue
            buf += payload
            if fin:
                return buf.decode('utf-8')

    async def recv_json(self):
        return json.loads(await self.recv_text())

    async def send_text(self, text):
        await self._send_frame(_OP_TEXT, text.encode('utf-8'))

    async def send_json(self, obj):
        await self.send_text(json.dumps(obj))

    async def _close_frame(self):
        header = bytearray([0x80 | _OP_CLOSE, 0x80 if self.mask else 0])
        if self.mask:
            header += os.urandom(4)
        self.writer.write(bytes(header))
        await self.writer.drain()

    async def close(self):
        if not self.closed:
            self.closed = True
            try:
                await self._close_frame()
            except Exception:
                pass
        try:
            self.writer.close()
            await self.writer.wait_closed()
        except Exception:
            pass


async def ws_connect(url):
    """Open a client WebSocket to a ws://host:port/path URL."""
    parts = urllib.parse.urlsplit(url)
    host = parts.hostname
    port = parts.port or (443 if parts.scheme == 'wss' else 80)
    path = parts.path or '/'
    if parts.query:
        path += '?' + parts.query
    reader, writer = await asyncio.open_connection(
        host, port, ssl=(parts.scheme == 'wss'))
    key = base64.b64encode(os.urandom(16)).decode('latin-1')
    writer.write((
        'GET %s HTTP/1.1\r\n'
        'Host: %s:%d\r\n'
        'Upgrade: websocket\r\n'
        'Connection: Upgrade\r\n'
        'Sec-WebSocket-Key: %s\r\n'
        'Sec-WebSocket-Version: 13\r\n'
        '\r\n' % (path, host, port, key)
    ).encode('latin-1'))
    await writer.drain()
    status, headers = await read_http_head(reader)
    if ' 101 ' not in status + ' ':
        writer.close()
        raise ConnectionError('WebSocket handshake failed: %s' % status)
    if headers.get('sec-websocket-accept') != accept_key(key):
        writer.close()
        raise ConnectionError('WebSocket handshake failed: bad accept key')
    return WebSocket(reader, writer, mask=True)


def _http_response(status, body=b'', content_type='text/plain'):
    return ('HTTP/1.1 %s\r\n'
            'Content-Type: %s\r\n'
            'Content-Length: %d\r\n'
            'Connection: close\r\n'
            '\r\n' % (status, content_type, len(body))).encode('latin-1') + body


async def serve_static(writer, root, target):
    """Serve a file under ``root`` for the request target ``target``.

    Returns the response status line ('200 OK', '404 Not Found',
    '403 Forbidden') so the caller can log failures.
    """
    path = urllib.parse.unquote(urllib.parse.urlsplit(target).path)
    if path.endswith('/'):
        path += 'index.html'
    root = os.path.abspath(root)
    full = os.path.abspath(os.path.join(root, path.lstrip('/')))
    if not (full == root or full.startswith(root + os.sep)):
        status = '403 Forbidden'
        writer.write(_http_response(status, b'403'))
    elif not os.path.isfile(full):
        status = '404 Not Found'
        writer.write(_http_response(status, b'404'))
    else:
        status = '200 OK'
        ctype = mimetypes.guess_type(full)[0] or 'application/octet-stream'
        with open(full, 'rb') as f:
            writer.write(_http_response(status, f.read(), ctype))
    await writer.drain()
    return status

"""CAT-SOOP API client (port of server/catsoop.js), stdlib urllib only.

Requests run in a worker thread via asyncio.to_thread so they don't
block the event loop.  Like the original, TLS verification is disabled
(the Node.js version used ``insecure: true`` / ``rejectUnauthorized:
false``).
"""

import asyncio
import json
import ssl
import urllib.parse
import urllib.request


class CatsoopError(Exception):
    pass


def url_join(*parts):
    return '/'.join(p.strip('/') for p in parts)


class Catsoop:
    def __init__(self, api_root, token):
        self.api_root = api_root
        self.token = token
        self._ssl_ctx = ssl.create_default_context()
        self._ssl_ctx.check_hostname = False
        self._ssl_ctx.verify_mode = ssl.CERT_NONE

    def _post_form_sync(self, uri, form):
        data = urllib.parse.urlencode(
            {k: v for k, v in form.items() if v is not None}
        ).encode('utf-8')
        req = urllib.request.Request(uri, data=data, method='POST')
        with urllib.request.urlopen(req, context=self._ssl_ctx, timeout=30) as res:
            return json.loads(res.read().decode('utf-8'))

    async def _post_form(self, uri, form):
        return await asyncio.to_thread(self._post_form_sync, uri, form)

    async def post(self, route, form, uri=None):
        """POST to an API route; raise CatsoopError unless res['ok']."""
        if uri is None:
            uri = url_join(self.api_root, route)
        res = await self._post_form(uri, dict({'api_token': self.token}, **form))
        if not res.get('ok'):
            raise CatsoopError(res.get('error'))
        res.pop('ok', None)
        return res

    async def submit(self, uri, form):
        """Submit an answer to a CAT-SOOP question page."""
        res = await self._post_form(
            uri, dict({'api_token': self.token, 'action': 'submit'}, **form))
        for value in res.values():
            if isinstance(value, dict) and value.get('error_msg'):
                raise CatsoopError(value['error_msg'])

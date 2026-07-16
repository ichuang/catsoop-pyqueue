"""A mock CAT-SOOP API server for the tests (port of test/catsoop.js)."""

import json
import threading
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


class MockCatsoop:
    """Serves the three endpoints the queue talks to:

      POST /get_user_information  -> echo auth back as user_info
      POST /groups/get_my_group   -> [as, as-partner]
      POST /assignments/0         -> record the submission
    """

    def __init__(self):
        self.submissions = []
        mock = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                length = int(self.headers.get('Content-Length', 0))
                body = self.rfile.read(length).decode('utf-8')
                form = {k: v[0] for k, v in urllib.parse.parse_qs(body).items()}

                if self.path == '/get_user_information':
                    if form.get('succeed'):
                        res = {'ok': True, 'user_info': {
                            'username': form.get('username'),
                            'role': form.get('role'),
                            'name': form.get('name', form.get('username')),
                        }}
                    else:
                        res = {'ok': False, 'error': 'mock error message'}
                elif self.path == '/groups/get_my_group':
                    # A path of ["nogroup"] simulates a student who has
                    # not formed a group in CAT-SOOP's groups store.
                    if json.loads(form.get('path') or 'null') == ['nogroup']:
                        res = {'ok': False,
                               'error': '%s has not been assigned to a group'
                                        % form['as']}
                    else:
                        res = {'ok': True,
                               'members': [form['as'], form['as'] + '-partner']}
                elif self.path == '/assignments/0':
                    mock.submissions.append(form)
                    names = json.loads(form['names'])
                    res = {name: {} for name in names}
                else:
                    self.send_error(404)
                    return

                payload = json.dumps(res).encode('utf-8')
                self.send_response(200)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.port = self.server.server_address[1]
        self.url = 'http://127.0.0.1:%d' % self.port
        self.thread = threading.Thread(target=self.server.serve_forever,
                                       daemon=True)

    def start(self):
        self.thread.start()
        return self

    def stop(self):
        self.server.shutdown()
        self.server.server_close()

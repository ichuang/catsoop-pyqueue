"""End-to-end tests for the pyqueue server (port of test/test.js).

Each test starts a real server on an ephemeral port, connects real
WebSocket clients through the Python Client, and authenticates against
a mock CAT-SOOP API — no external services required.

Run from the repo root:  python -m unittest discover -s test -v
"""

import asyncio
import base64
import hashlib
import hmac
import json
import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pyqueue.client import Client
from pyqueue.queue_server import QueueApp
from pyqueue.util import hash_username
from mock_catsoop import MockCatsoop


def test_params(catsoop_url):
    return {
        'SERVER': {'HOST': '127.0.0.1', 'PORT': 0},
        'STORE': {'PATH': None},
        'CATSOOP': {'TOKEN': 'testtoken', 'API_ROOT': catsoop_url},
        'STAFF_CHECK_IN_REQUIRED': False,
        'URL_ROOT': '/',
        'URL_PREFIXES': ['/queue'],
        'ROOMS': ['default', 'other'],
        'PRINT_LOGS': False,
        'LOG_DIR': None,
        'WWW_ROOT': os.path.join(os.path.dirname(__file__), '..', 'www'),
    }


class QueueTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.catsoop = MockCatsoop().start()
        self.app = QueueApp(test_params(self.catsoop.url))
        await self.app.start()
        self.clients = []

    async def asyncTearDown(self):
        for client in self.clients:
            await client.close()
        await self.app.stop()
        self.catsoop.stop()

    async def user(self, username, role, room='default'):
        """Connect and authenticate a client."""
        client = Client('ws://127.0.0.1:%d/ws' % self.app.port, room=room)
        await client.connect()
        self.clients.append(client)
        auth = await client.login({'succeed': '1', 'username': username,
                                   'role': role})
        assert not (auth or {}).get('error'), auth
        return client

    async def settle(self):
        """Let pending broadcast tasks run."""
        for _ in range(10):
            await asyncio.sleep(0.01)

    def help_data(self, location='table 1'):
        return {
            'location': location,
            'assignment': {'name': 'q1', 'display_name': 'Question 1',
                           'page': self.catsoop.url + '/assignments/0',
                           'path': ['test', 'page']},
        }

    def signed_auth(self, user_info, token='testtoken', timestamp=None):
        """Build a signed auth blob exactly as the CAT-SOOP queue
        plugin's post_load.py does."""
        ui = dict(user_info)
        ui['queue_timestamp'] = time.time() if timestamp is None else timestamp
        payload = base64.b64encode(json.dumps(ui).encode('utf-8'))
        key = hashlib.sha256(token.encode('ascii')).hexdigest().encode('ascii')
        verifier = hmac.new(key, payload, hashlib.sha256).hexdigest()
        return base64.b64encode(json.dumps({
            'payload': payload.decode('utf-8'),
            'verifiers': [verifier],
        }).encode('utf-8')).decode('utf-8')

    ## Authentication

    async def test_auth_failure(self):
        client = Client('ws://127.0.0.1:%d/ws' % self.app.port)
        await client.connect()
        self.clients.append(client)
        auth = await client.login({'username': 'nope', 'role': 'Student'})
        self.assertEqual(auth, {'error': 'Invalid authentication'})

    async def test_auth_success_permissions(self):
        client = await self.user('ta1', 'TA')
        auth = await client.send('authenticate',
                                 {'room': 'default', 'succeed': '1',
                                  'username': 'ta1', 'role': 'TA'})
        for permission in ('clear', 'lock', 'claim', 'checkoff',
                           'queue_view_all'):
            self.assertIn(permission, auth['permissions'])

    async def test_signed_auth_success(self):
        client = Client('ws://127.0.0.1:%d/ws' % self.app.port)
        await client.connect()
        self.clients.append(client)
        auth = await client.login({
            'auth': self.signed_auth({'username': 'ta9', 'role': 'TA',
                                      'name': 'A TA'}),
        })
        self.assertEqual(auth['username'], 'ta9')
        for permission in ('clear', 'lock', 'claim', 'checkoff'):
            self.assertIn(permission, auth['permissions'])

    async def test_signed_auth_bad_signature(self):
        client = Client('ws://127.0.0.1:%d/ws' % self.app.port)
        await client.connect()
        self.clients.append(client)
        auth = await client.login({
            'auth': self.signed_auth({'username': 'evil', 'role': 'Admin'},
                                     token='wrongtoken'),
        })
        self.assertEqual(auth, {'error': 'Invalid authentication'})

    async def test_signed_auth_expired(self):
        self.app.params['CATSOOP']['AUTH_MAX_AGE'] = 60
        client = Client('ws://127.0.0.1:%d/ws' % self.app.port)
        await client.connect()
        self.clients.append(client)
        auth = await client.login({
            'auth': self.signed_auth({'username': 'ta9', 'role': 'TA'},
                                     timestamp=time.time() - 3600),
        })
        self.assertEqual(auth, {'error': 'Invalid authentication'})

    async def test_requires_auth(self):
        client = Client('ws://127.0.0.1:%d/ws' % self.app.port)
        await client.connect()
        self.clients.append(client)
        result = await client.send('get_all', {})
        self.assertEqual(result, {'error': 'Not authenticated'})

    ## Adding entries and visibility

    async def test_add_and_visibility(self):
        student = await self.user('alice', 'Student')
        other = await self.user('bob', 'Student')
        staff = await self.user('ta1', 'TA')

        result = await student.add('help', self.help_data())
        self.assertEqual(result, {'success': True})

        mine = await student.get_entries()
        self.assertEqual(len(mine), 1)
        self.assertEqual(mine[0]['username'], 'alice')
        self.assertEqual(mine[0]['data']['state'], 'unclaimed')

        # Another student sees only an anonymized placeholder.
        theirs = await other.get_entries()
        self.assertEqual(len(theirs), 1)
        self.assertEqual(theirs[0]['username'], hash_username('alice'))
        self.assertEqual(theirs[0]['type'], '')
        self.assertEqual(theirs[0]['actions'], [])

        # Staff see everything and can claim.
        staffs = await staff.get_entries()
        self.assertEqual(staffs[0]['username'], 'alice')
        self.assertEqual(staffs[0]['actions'], ['claim'])

    async def test_add_broadcasts_edit(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')

        edits = []
        staff.recv('edit', edits.append)
        await student.add('help', self.help_data())
        await self.settle()

        self.assertEqual(len(edits), 1)
        self.assertEqual(edits[0]['added_entries'][0]['username'], 'alice')

    async def test_rooms_are_separate(self):
        student = await self.user('alice', 'Student', room='other')
        staff = await self.user('ta1', 'TA', room='default')
        await student.add('help', self.help_data())
        self.assertEqual(await staff.get_entries(), [])

    ## Claiming

    async def test_claim_disclaim(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')
        await student.add('help', self.help_data())

        await staff.action('claim', {'username': 'alice'})
        await self.settle()
        [entry] = await staff.get_entries()
        self.assertEqual(entry['data']['state'], 'claimed')
        self.assertEqual(entry['data']['claimant'], 'ta1')
        self.assertEqual(entry['actions'], ['disclaim', 'remove'])

        # The student sees who claimed them.
        [entry] = await student.get_entries()
        self.assertEqual(entry['data']['claimant'], 'ta1')

        await staff.action('disclaim', {'username': 'alice'})
        await self.settle()
        [entry] = await staff.get_entries()
        self.assertEqual(entry['data']['state'], 'unclaimed')
        self.assertNotIn('claimant', entry['data'])

    async def test_only_one_claim_per_staffer(self):
        alice = await self.user('alice', 'Student')
        bob = await self.user('bob', 'Student')
        staff = await self.user('ta1', 'TA')
        await alice.add('help', self.help_data())
        await bob.add('help', self.help_data())

        await staff.action('claim', {'username': 'alice'})
        await self.settle()
        await staff.action('claim', {'username': 'bob'})
        await self.settle()

        entries = {e['username']: e for e in await staff.get_entries()}
        self.assertEqual(entries['alice']['data']['state'], 'claimed')
        self.assertEqual(entries['bob']['data']['state'], 'unclaimed')

    async def test_claimed_entry_cannot_be_reclaimed(self):
        alice = await self.user('alice', 'Student')
        ta1 = await self.user('ta1', 'TA')
        ta2 = await self.user('ta2', 'TA')
        await alice.add('help', self.help_data())

        await ta1.action('claim', {'username': 'alice'})
        await self.settle()
        await ta2.action('claim', {'username': 'alice'})
        await self.settle()

        [entry] = await ta1.get_entries()
        self.assertEqual(entry['data']['claimant'], 'ta1')
        # The other staffer gets no actions on someone else's claim.
        [entry] = await ta2.get_entries()
        self.assertEqual(entry['actions'], [])

    async def test_students_cannot_claim(self):
        alice = await self.user('alice', 'Student')
        bob = await self.user('bob', 'Student')
        await alice.add('help', self.help_data())
        await bob.action('claim', {'username': 'alice'})
        await self.settle()
        [entry] = await alice.get_entries()
        self.assertEqual(entry['data']['state'], 'unclaimed')

    ## Removal

    async def test_student_can_remove_own_entry(self):
        student = await self.user('alice', 'Student')
        await student.add('help', self.help_data())
        await student.remove()
        await self.settle()
        self.assertEqual(await student.get_entries(), [])

    async def test_student_cannot_remove_others(self):
        alice = await self.user('alice', 'Student')
        bob = await self.user('bob', 'Student')
        await alice.add('help', self.help_data())
        await bob.action('remove', {'username': 'alice'})
        await self.settle()
        self.assertEqual(len(await alice.get_entries()), 1)

    async def test_remove_broadcasts_delete(self):
        alice = await self.user('alice', 'Student')
        bob = await self.user('bob', 'Student')
        await alice.add('help', self.help_data())
        await self.settle()

        edits = []
        bob.recv('edit', edits.append)
        await alice.remove()
        await self.settle()
        self.assertEqual(edits[-1]['deleted_usernames'],
                         [hash_username('alice')])

    ## Locking

    async def test_lock_unlock(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')

        self.assertFalse(await student.is_locked())
        await staff.lock()
        self.assertTrue(await student.is_locked())
        self.assertEqual(await student.add('help', self.help_data()),
                         {'success': False})

        await staff.unlock()
        self.assertEqual(await student.add('help', self.help_data()),
                         {'success': True})

    async def test_students_cannot_lock(self):
        student = await self.user('alice', 'Student')
        await student.lock()
        self.assertFalse(await student.is_locked())

    ## Clearing

    async def test_clear(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')
        await student.add('help', self.help_data())

        await student.clear()          # no permission: no effect
        self.assertEqual(len(await staff.get_entries()), 1)

        await staff.clear()
        await self.settle()
        self.assertEqual(await staff.get_entries(), [])

    ## Staff list

    async def test_staff_list_auto_check_in(self):
        staff = await self.user('ta1', 'TA')
        staff_list = await staff.get_staff_list()
        self.assertEqual(staff_list['confirmed'], ['ta1'])

    async def test_check_out_and_in(self):
        staff = await self.user('ta1', 'TA')
        await staff.check_out('ta1')
        staff_list = await staff.get_staff_list()
        self.assertEqual(staff_list['confirmed'], [])

        await staff.check_in('ta1')
        staff_list = await staff.get_staff_list()
        self.assertEqual(staff_list['confirmed'], ['ta1'])

    async def test_cannot_check_in_students(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')
        await staff.check_in('alice')
        staff_list = await staff.get_staff_list()
        self.assertNotIn('alice', staff_list['confirmed'])

    ## Static files

    async def test_static_url_prefix(self):
        import urllib.request

        def status(path):
            url = 'http://127.0.0.1:%d%s' % (self.app.port, path)
            try:
                with urllib.request.urlopen(url) as res:
                    return res.status
            except urllib.error.HTTPError as err:
                return err.code

        # /queue/* is served the same as /* (nginx forwards the prefix)
        self.assertEqual(await asyncio.to_thread(status, '/css/queue.css'), 200)
        self.assertEqual(await asyncio.to_thread(status, '/queue/css/queue.css'), 200)
        self.assertEqual(await asyncio.to_thread(status, '/queue/js/client.js'), 200)
        self.assertEqual(await asyncio.to_thread(status, '/queue/nope.js'), 404)

    ## Checkoffs

    async def test_checkoff_group_membership(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')
        await student.add('checkoff', self.help_data())
        await self.settle()

        [entry] = await staff.get_entries()
        self.assertEqual([m['username'] for m in entry['data']['group']],
                         ['alice', 'alice-partner'])

    async def test_group_checkoff_submits_and_removes(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')
        await student.add('checkoff', self.help_data())
        await staff.action('claim', {'username': 'alice'})
        await self.settle()

        await staff.action('group_checkoff', {'username': 'alice'})
        await self.settle()

        self.assertEqual(sorted(s['as'] for s in self.catsoop.submissions),
                         ['alice', 'alice-partner'])
        self.assertIn('testtoken,ta1', self.catsoop.submissions[0]['data'])
        self.assertEqual(await staff.get_entries(), [])

    async def test_single_checkoff_submits_one(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')
        await student.add('checkoff', self.help_data())
        await staff.action('claim', {'username': 'alice'})
        await self.settle()

        await staff.action('single_checkoff', {'username': 'alice'})
        await self.settle()

        self.assertEqual([s['as'] for s in self.catsoop.submissions],
                         ['alice'])
        self.assertEqual(await staff.get_entries(), [])

    async def test_checkoff_hidden_from_other_students(self):
        alice = await self.user('alice', 'Student')
        bob = await self.user('bob', 'Student')
        await alice.add('checkoff', self.help_data())
        [entry] = await bob.get_entries()
        self.assertEqual(entry['username'], hash_username('alice'))

    ## Re-adding (conflict merge)

    async def test_re_add_preserves_date_and_state(self):
        student = await self.user('alice', 'Student')
        staff = await self.user('ta1', 'TA')
        await student.add('help', self.help_data('table 1'))
        [before] = await student.get_entries()

        await staff.action('claim', {'username': 'alice'})
        await self.settle()
        await student.add('help', self.help_data('table 9'))
        await self.settle()

        [after] = await staff.get_entries()
        self.assertEqual(after['date_added'], before['date_added'])
        self.assertEqual(after['data']['state'], 'claimed')
        self.assertEqual(after['data']['location'], 'table 9')


class PersistenceTest(unittest.IsolatedAsyncioTestCase):
    async def test_state_survives_restart(self):
        import tempfile
        catsoop = MockCatsoop().start()
        with tempfile.TemporaryDirectory() as tmp:
            params = test_params(catsoop.url)
            params['STORE'] = {'PATH': os.path.join(tmp, 'queue.json')}

            app = QueueApp(params)
            await app.start()
            client = Client('ws://127.0.0.1:%d/ws' % app.port)
            await client.connect()
            await client.login({'succeed': '1', 'username': 'alice',
                                'role': 'Student'})
            await client.add('help', {'location': 'table 3',
                                      'assignment': {'name': 'q1'}})
            await client.close()
            await app.stop()

            # A new app instance picks the entry back up from disk.
            app2 = QueueApp(params)
            await app2.start()
            client2 = Client('ws://127.0.0.1:%d/ws' % app2.port)
            await client2.connect()
            await client2.login({'succeed': '1', 'username': 'ta1',
                                 'role': 'TA'})
            [entry] = await client2.get_entries()
            self.assertEqual(entry['username'], 'alice')
            self.assertEqual(entry['data']['location'], 'table 3')
            await client2.close()
            await app2.stop()
        catsoop.stop()


if __name__ == '__main__':
    unittest.main()

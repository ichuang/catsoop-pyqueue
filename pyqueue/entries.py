"""Queue entry types (port of server/entry_types.js).

An entry wraps a store document (username, type, date_added,
last_modified, data, room) and knows how to render itself for a given
viewer and which actions that viewer may take on it.  Action methods
mutate the store; the store's change events then drive the broadcasts.
"""

import asyncio
import json

from . import util


class Entry:
    ACTIONS = ('claim', 'disclaim', 'remove')

    def __init__(self, doc, ctx):
        """``ctx`` is the QueueApp, providing .store, .catsoop, .params."""
        self.ctx = ctx
        self.update(doc)

    def update(self, doc):
        for key, value in doc.items():
            setattr(self, key, value)

    @staticmethod
    def data_skeleton(data, user):
        return {
            'location': data.get('location'),
            'mlypod': data.get('mlypod'),
            'meeting_url': data.get('meeting_url'),
            'lab_in_session': data.get('lab_in_session'),
            'assignment': data.get('assignment'),
            'state': 'unclaimed',
        }

    def visible_to(self, user):
        raise NotImplementedError

    async def render(self, user, users):
        if self.visible_to(user):
            known = users.get(self.username) or {}
            real_name = known.get('full_name', known.get('name')) or ''
            if 'subject' in known:
                real_name += ' (%s)' % known['subject']
            return {
                'data': dict(self.data,
                             group=[{'username': self.username,
                                     'real_name': real_name}]),
                'type': self.type,
                'actions': self.actions(user),
                'date_added': self.date_added,
                'last_modified': self.last_modified,
                'username': self.username,
                'real_name': real_name,
            }
        else:
            return {
                'data': {'state': self.data.get('state')},
                'type': '',
                'actions': [],
                'date_added': self.date_added,
                'last_modified': '',
                'username': util.hash_username(self.username),
                'real_name': '',
            }

    def actions(self, user):
        state = self.data.get('state')
        if state == 'claimed':
            if self.data.get('claimant') != user.get('username'):
                return []
            return ['disclaim', 'remove']
        if state == 'unclaimed':
            return ['claim']
        return []

    async def do_action(self, name, user):
        if name not in self.ACTIONS:
            raise ValueError('unknown action: %r' % name)
        await getattr(self, name)(user)

    async def claim(self, user):
        if 'claim' not in user.get('permissions', ()):
            return
        if self.ctx.params.get('STAFF_CHECK_IN_REQUIRED') and not user.get('confirmed'):
            return

        def replacer(doc):
            # A staffer holding a claim can't take another; an entry
            # with a claimant can't be claimed again.
            if user.get('claims') or doc['data'].get('claimant'):
                return doc
            doc = util.deep_merge(doc, {'data': {
                'state': 'claimed',
                'claimant': user['username'],
                'claimant_real_name': user.get('name'),
            }})
            doc['last_modified'] = util.now_iso()
            return doc

        self.ctx.store.replace(self.username, replacer)

    async def disclaim(self, user):
        if 'claim' not in user.get('permissions', ()):
            return

        def replacer(doc):
            if doc['data'].get('claimant') != user['username']:
                return doc
            doc = util.without(doc, ('data', 'claimant'),
                               ('data', 'claimant_real_name'))
            doc['data']['state'] = 'unclaimed'
            doc['last_modified'] = util.now_iso()
            return doc

        self.ctx.store.replace(self.username, replacer)

    async def remove(self, user):
        def replacer(doc):
            if (doc['data'].get('claimant') == user['username']
                    or doc['username'] == user['username']):
                return None
            return doc

        self.ctx.store.replace(self.username, replacer)


class HelpEntry(Entry):
    def visible_to(self, user):
        return (user.get('username') == self.username
                or user.get('role') not in ('Guest', 'Student'))


class CheckoffEntry(Entry):
    ACTIONS = Entry.ACTIONS + ('single_checkoff', 'group_checkoff')

    def __init__(self, doc, ctx):
        super().__init__(doc, ctx)
        self._group = None

    def group(self):
        # Fetched once per entry, lazily: entries may be constructed at
        # startup (from persisted state) before the event loop runs.
        if self._group is None:
            self._group = asyncio.ensure_future(self._fetch_group())
        return self._group

    async def _fetch_group(self):
        try:
            return await self.ctx.catsoop.post('/groups/get_my_group', {
                'path': json.dumps((self.data.get('assignment') or {}).get('path')),
                'as': self.username,
            })
        except Exception:
            return {'members': [self.username]}

    def visible_to(self, user):
        return (user.get('username') == self.username
                or 'queue_view_all' in user.get('permissions', ()))

    async def render(self, user, users):
        group = await self.group()
        entry = await super().render(user, users)
        if self.visible_to(user):
            entry['data']['group'] = [
                {'username': m,
                 'real_name': (users.get(m) or {}).get('name') or ''}
                for m in group['members']
            ]
        return entry

    def actions(self, user):
        actions = super().actions(user)
        if (self.data.get('state') == 'claimed'
                and self.data.get('claimant') == user.get('username')):
            actions = actions + ['group_checkoff', 'single_checkoff']
        return actions

    def _submit_form(self, member, user):
        assignment = self.data['assignment']
        return {
            'names': json.dumps([assignment['name']]),
            'as': member,
            'data': json.dumps({
                assignment['name']:
                    '%s,%s' % (self.ctx.params['CATSOOP']['TOKEN'], user['username']),
            }),
        }

    def _remove_if_claimant(self, user):
        def replacer(doc):
            if doc['data'].get('claimant') == user['username']:
                return None
            return doc
        self.ctx.store.replace(self.username, replacer)

    async def single_checkoff(self, user):
        if 'checkoff' not in user.get('permissions', ()):
            return
        await self.ctx.catsoop.submit(self.data['assignment']['page'],
                                      self._submit_form(self.username, user))
        self._remove_if_claimant(user)

    async def group_checkoff(self, user):
        if 'checkoff' not in user.get('permissions', ()):
            return
        group = await self.group()
        await asyncio.gather(*(
            self.ctx.catsoop.submit(self.data['assignment']['page'],
                                    self._submit_form(member, user))
            for member in group['members']
        ))
        self._remove_if_claimant(user)


ENTRY_TYPES = {
    'help': HelpEntry,
    'checkoff': CheckoffEntry,
}

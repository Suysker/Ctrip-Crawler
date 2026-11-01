import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path
from test_files import CrawlerError, load_files


class AccountPoolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.ns = load_files()
        self.accounts = [{'alias': 'a', 'username': 'one', 'enabled': True}, {'alias': 'b', 'username': 'two', 'enabled': True}]
        self.pool = self.ns['AccountPool'](self.output, self.accounts)
        self.control_path = self.output / '.runs/r1/progress.json'
        self.control = {'cursor': 0, 'current': {'attempt_id': 'x'}, 'blocked_accounts': {}}
        self.ns['atomic_json'](self.control_path, self.control)

    def snapshot(self, revision=1):
        return {'session_identity_id': 'identity-a', 'session_revision': revision, 'fingerprint': {'os': 'windows'}, 'storage_state': {'cookies': [], 'origins': []}}

    def test_cooldown_survives_reconstruction_and_selects_next(self):
        until = (datetime.now(timezone.utc) + timedelta(minutes=30)).isoformat()
        self.pool.mark('a', 'cooldown', 'challenge', cooldown_until=until, control_path=self.control_path, control=self.control)
        restored = self.ns['AccountPool'](self.output, self.accounts)
        self.assertEqual(restored.select(preferred='a', blocked=self.control['blocked_accounts']), 'b')
        self.assertEqual(restored.record('a')['state'], 'cooldown')

    def test_snapshot_refresh_cannot_clear_task_block(self):
        self.pool.initialize_session('a', self.snapshot())
        self.pool.mark('a', 'cooldown', 'challenge', cooldown_until=datetime.now(timezone.utc).isoformat(), control_path=self.control_path, control=self.control)
        self.pool.commit_session('a', self.snapshot(2))
        self.pool.reconcile(self.control, self.control_path)
        self.assertIn('a', self.control['blocked_accounts'])

    def test_explicit_login_clears_block_but_not_server_wait(self):
        until = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
        self.pool.initialize_session('a', self.snapshot())
        self.pool.mark('a', 'cooldown', 'challenge', server_not_before=until, control_path=self.control_path, control=self.control)
        self.pool.commit_session('a', self.snapshot(2), explicit_auth=True, auth_id='login-2')
        self.pool.reconcile(self.control, self.control_path)
        self.assertNotIn('a', self.control['blocked_accounts'])
        self.assertEqual(self.pool.select(preferred='a', blocked={}), 'b')
        self.assertEqual(self.pool.record('a')['server_not_before'], until)

    def test_pending_account_mutation_replayed_before_new_change(self):
        real_atomic = self.ns['atomic_json']
        def crash_control(path, data):
            if Path(path) == self.control_path:
                raise OSError('crash')
            return real_atomic(path, data)
        self.ns['atomic_json'] = crash_control
        with self.assertRaises(CrawlerError):
            self.pool.mark('a', 'disabled', 'bad_credentials', control_path=self.control_path, control=self.control)
        self.ns['atomic_json'] = real_atomic
        restored = self.ns['AccountPool'](self.output, self.accounts)
        restored.reconcile(self.control, self.control_path)
        self.assertIn('a', self.control['blocked_accounts'])
        self.assertEqual(restored.record('a')['state'], 'disabled')
        before = self.control.copy()
        restored.reconcile(self.control, self.control_path)
        self.assertEqual(before, self.control)

    def test_alias_cannot_be_rebound_to_another_login(self):
        with self.assertRaises(CrawlerError):
            self.ns['AccountPool'](self.output, [{'alias': 'a', 'username': 'changed', 'enabled': True}])

    def test_session_revision_cannot_go_backwards(self):
        self.pool.initialize_session('a', self.snapshot(3))
        with self.assertRaises(CrawlerError):
            self.pool.commit_session('a', self.snapshot(2))
        self.assertEqual(self.pool.load_session('a')['session_revision'], 3)

    def test_expired_local_cooldown_does_not_clear_task_block(self):
        past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
        self.pool.mark('a', 'cooldown', 'challenge', cooldown_until=past, control_path=self.control_path, control=self.control)
        self.assertEqual(self.pool.select(preferred='a', blocked=self.control['blocked_accounts']), 'b')
        self.assertEqual(self.pool.select(preferred='a', blocked={}), 'a')

    def test_unknown_files_and_passwords_not_written(self):
        state = self.ns['read_json'](self.output / 'accounts.json')
        self.assertNotIn('one', self.ns['_files_json'].dumps(state))
        self.assertNotIn('two', self.ns['_files_json'].dumps(state))
        self.assertNotIn('password', str(state))
        self.assertFalse(self.pool.session_path('a').exists())


if __name__ == '__main__':
    unittest.main()

import csv
import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timezone, timedelta
from pathlib import Path


class CrawlerError(Exception):
    def __init__(self, code, message):
        self.code = code
        super().__init__(message)


def load_files():
    ns = {
        '__name__': 'files_under_test',
        'CrawlerError': CrawlerError,
        'SCHEMA_VERSION': 1,
        'utc_now': lambda: datetime.now(timezone.utc).isoformat(),
        'parse_utc': lambda value: datetime.fromisoformat(value.replace('Z', '+00:00')),
        'stable_hash': lambda value: hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=False).encode()).hexdigest(),
    }
    source = Path(__file__).resolve().parents[1] / 'source_sections/files.py.inc'
    exec(compile(source.read_text(encoding='utf-8'), str(source), 'exec'), ns)
    return ns


class FileStoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = Path(self.temp.name)
        self.ns = load_files()
        self.store = self.ns['FileStore'](self.output, 'run-1')
        self.plan = {'run_id': 'run-1', 'schema_version': 1, 'cities': [], 'dates': ['2027-01-01'], 'config': {'timezone': 'Asia/Shanghai'}}
        self.progress = self.store.create(self.plan)
        self.progress['current'] = {'attempt_id': 'try-1', 'worker_generation': 1}
        self.store.save_progress(self.progress)
        self.task = {'index': 0, 'key': 'task-1', 'run_id': 'run-1', 'origin': {'code': 'SHA', 'name': '上海'}, 'destination': {'code': 'HKG', 'name': '香港'}, 'departure_date': '2027-01-01'}
        self.observation = {'rows': [{'航班号': 'MU1', '票价': '12.30'}], 'columns': ['航班号', '票价'], 'observed_at': '2026-12-31T16:01:00+00:00', 'status': 'succeeded', 'comfort_status': 'disabled', 'price_status': 'complete', 'price_reason': []}

    def csv_path(self):
        return self.output / '2027-01-01/2027-01-01/上海-香港.csv'

    def test_publish_and_load_preserve_plan_and_commit_once(self):
        self.store.publish(self.task, self.observation, self.progress)
        plan, progress = self.store.load()
        self.assertEqual(plan['run_id'], 'run-1')
        self.assertEqual(progress['cursor'], 1)
        self.assertIsNone(progress['publish_intent'])
        with self.csv_path().open(encoding='utf-8') as handle:
            self.assertEqual(list(csv.DictReader(handle))[0]['票价'], '12.30')
        meta = self.ns['read_json'](self.csv_path().with_suffix('.meta.json'))
        self.assertEqual(meta['observed_at'], self.observation['observed_at'])
        self.assertEqual(meta['sha256'], hashlib.sha256(self.csv_path().read_bytes()).hexdigest())
        self.assertFalse(self.store.recover(progress))
        self.assertEqual(progress['cursor'], 1)

    def test_existing_unowned_csv_is_preserved(self):
        path = self.csv_path()
        path.parent.mkdir(parents=True)
        path.write_text('old data', encoding='utf-8')
        with self.assertRaises(CrawlerError) as cm:
            self.store.publish(self.task, self.observation, self.progress)
        self.assertEqual(cm.exception.code, 'storage_error')
        self.assertEqual(path.read_text(encoding='utf-8'), 'old data')
        self.assertEqual(self.progress['cursor'], 0)

    def test_crash_after_csv_before_receipt_recovers_without_recrawl(self):
        real_atomic = self.ns['atomic_json']
        def crash_receipt(path, data):
            if str(path).endswith('.meta.json'):
                raise OSError('injected meta write failure')
            return real_atomic(path, data)
        self.ns['atomic_json'] = crash_receipt
        with self.assertRaises(CrawlerError):
            self.store.publish(self.task, self.observation, self.progress)
        self.ns['atomic_json'] = real_atomic
        _, restored = self.store.load()
        self.assertIsNotNone(restored['publish_intent'])
        self.assertTrue(self.store.recover(restored))
        self.assertEqual(restored['cursor'], 1)
        self.assertTrue(self.csv_path().with_suffix('.meta.json').exists())

    def test_crash_with_valid_temp_intent_finishes_publish(self):
        real_replace = self.ns['_files_os'].replace
        def fail_publish(src, dst):
            if str(dst).endswith('.csv'):
                raise OSError('injected publication failure')
            return real_replace(src, dst)
        self.ns['_files_os'].replace = fail_publish
        try:
            with self.assertRaises(CrawlerError):
                self.store.publish(self.task, self.observation, self.progress)
        finally:
            self.ns['_files_os'].replace = real_replace
        _, restored = self.store.load()
        self.assertTrue(self.store.recover(restored))
        self.assertTrue(self.csv_path().exists())

    def test_corrupt_formal_csv_is_not_replaced_from_temp(self):
        real_atomic = self.ns['atomic_json']
        def crash_receipt(path, data):
            if str(path).endswith('.meta.json'):
                raise OSError('crash')
            return real_atomic(path, data)
        self.ns['atomic_json'] = crash_receipt
        with self.assertRaises(CrawlerError):
            self.store.publish(self.task, self.observation, self.progress)
        self.ns['atomic_json'] = real_atomic
        self.csv_path().write_text('externally changed', encoding='utf-8')
        _, restored = self.store.load()
        with self.assertRaises(CrawlerError):
            self.store.recover(restored)
        self.assertEqual(self.csv_path().read_text(encoding='utf-8'), 'externally changed')
        self.assertEqual(restored['cursor'], 0)

    def test_meta_done_progress_not_advanced_counts_once(self):
        real_save = self.store.save_progress
        def fail_advance(progress):
            if progress['cursor'] == 1:
                raise CrawlerError('storage_error', 'crash before checkpoint')
            real_save(progress)
        self.store.save_progress = fail_advance
        with self.assertRaises(CrawlerError):
            self.store.publish(self.task, self.observation, self.progress)
        self.store.save_progress = real_save
        _, restored = self.store.load()
        self.assertTrue(self.store.recover(restored))
        self.assertEqual(restored['stats']['succeeded'], 1)
        self.assertFalse(self.store.recover(restored))
        self.assertEqual(restored['stats']['succeeded'], 1)

    def test_empty_result_has_header_and_distinct_receipt(self):
        observation = dict(self.observation, rows=[], status='empty', price_status='not_applicable', price_reason=[])
        self.store.publish(self.task, observation, self.progress)
        self.assertEqual(self.csv_path().read_text(encoding='utf-8').strip(), '航班号,票价')
        meta = self.ns['read_json'](self.csv_path().with_suffix('.meta.json'))
        self.assertEqual((meta['status'], meta['row_count']), ('empty', 0))

    def test_corrupt_plan_is_not_silently_recreated(self):
        plan = self.ns['read_json'](self.store.plan_path)
        plan['dates'] = ['2030-01-01']
        self.ns['atomic_json'](self.store.plan_path, plan)
        with self.assertRaises(CrawlerError):
            self.store.load()

    def test_missing_checkpoint_after_creation_cannot_reset_budget(self):
        self.store.progress_path.unlink()
        with self.assertRaises(CrawlerError):
            self.store.load()

    def test_interrupted_first_checkpoint_can_be_initialized_once(self):
        other = self.ns['FileStore'](self.output, 'interrupted')
        real_atomic = self.ns['atomic_json']
        def fail_checkpoint(path, data):
            if Path(path) == other.progress_path:
                raise OSError('power loss before first checkpoint')
            return real_atomic(path, data)
        self.ns['atomic_json'] = fail_checkpoint
        with self.assertRaises((CrawlerError, OSError)):
            other.create(dict(self.plan, run_id='interrupted'))
        self.ns['atomic_json'] = real_atomic
        _, progress = other.load()
        self.assertEqual(progress['cursor'], 0)
        initialization = self.ns['read_json'](other.run_dir / 'initialization.json')
        self.assertTrue(initialization['progress_initialized'])
        other.progress_path.unlink()
        with self.assertRaises(CrawlerError):
            other.load()

    def test_first_progress_preserves_initialization_site_recovery_budget(self):
        other = self.ns['FileStore'](self.output, 'limited')
        until = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        self.ns['atomic_json'](other.initialization_path, {
            'rate_limit_recoveries': 1, 'server_not_before': until,
        })
        progress = other.create(dict(self.plan, run_id='limited'))
        stored = self.ns['read_json'](other.progress_path)
        self.assertEqual(progress['rate_limit_recoveries'], 1)
        self.assertEqual(stored['rate_limit_recoveries'], 1)
        self.assertEqual(stored['server_not_before'], until)

    def test_missing_first_progress_recovers_site_budget_from_initialization(self):
        other = self.ns['FileStore'](self.output, 'limited-interrupted')
        until = (datetime.now(timezone.utc) + timedelta(seconds=30)).isoformat()
        self.ns['atomic_json'](other.initialization_path, {
            'rate_limit_recoveries': 1, 'server_not_before': until,
        })
        real_atomic = self.ns['atomic_json']

        def fail_checkpoint(path, data):
            if Path(path) == other.progress_path:
                raise OSError('interrupted first checkpoint')
            return real_atomic(path, data)

        self.ns['atomic_json'] = fail_checkpoint
        try:
            with self.assertRaises((CrawlerError, OSError)):
                other.create(dict(self.plan, run_id='limited-interrupted'))
        finally:
            self.ns['atomic_json'] = real_atomic
        _, restored = other.load()
        self.assertEqual(restored['rate_limit_recoveries'], 1)
        self.assertEqual(restored['server_not_before'], until)

    def test_path_traversal_rejected(self):
        with self.assertRaises(CrawlerError):
            self.ns['safe_path'](self.output, '../elsewhere.json')

    def test_missing_comfort_reason_and_count_are_committed_with_result(self):
        for status in ('partial', 'unavailable'):
            with self.subTest(status=status):
                other = self.ns['FileStore'](self.output / status, 'comfort')
                progress = other.create(dict(self.plan, run_id='comfort'))
                progress['current'] = {'attempt_id': 'comfort-attempt'}
                other.save_progress(progress)
                task = dict(self.task, run_id='comfort')
                observation = dict(self.observation, comfort_status=status,
                                   comfort_reason='missing_or_incomplete_comfort_response')
                other.publish(task, observation, progress)
                receipt = self.ns['read_json'](next((self.output / status).rglob('*.meta.json')))
                self.assertEqual(receipt['comfort_status'], status)
                self.assertEqual(receipt['comfort_reason'], observation['comfort_reason'])
                self.assertEqual(progress['stats']['comfort_' + status], 1)
                self.assertFalse(other.recover(progress))
                self.assertEqual(progress['stats']['comfort_' + status], 1)

    def test_recovery_rejects_changed_comfort_receipt_and_does_not_count_twice(self):
        real_save = self.store.save_progress

        def fail_advance(progress):
            if progress['cursor'] == 1:
                raise CrawlerError('storage_error', 'interrupted checkpoint')
            return real_save(progress)

        self.store.save_progress = fail_advance
        observation = dict(self.observation, comfort_status='partial', comfort_reason='incomplete')
        with self.assertRaises(CrawlerError):
            self.store.publish(self.task, observation, self.progress)
        self.store.save_progress = real_save
        receipt_path = self.csv_path().with_suffix('.meta.json')
        receipt = self.ns['read_json'](receipt_path)
        receipt['comfort_reason'] = 'changed'
        self.ns['atomic_json'](receipt_path, receipt)
        _, restored = self.store.load()
        with self.assertRaises(CrawlerError):
            self.store.recover(restored)
        self.assertEqual(restored['cursor'], 0)

    def test_staging_without_intent_cleans_only_owned_temp(self):
        owned = self.output / '2027-01-01/2027-01-01/.run-1.try-1.partial.csv'
        owned.parent.mkdir(parents=True)
        owned.write_text('unfinished')
        unknown = owned.with_name('unknown.tmp')
        unknown.write_text('keep')
        self.progress['staging'] = {'run_id': 'run-1', 'task_index': 0, 'attempt_id': 'try-1', 'temp_path': str(owned.relative_to(self.output)), 'target_path': str(self.csv_path().relative_to(self.output)), 'result_id': 'r1'}
        self.store.save_progress(self.progress)
        self.assertFalse(self.store.recover(self.progress))
        self.assertFalse(owned.exists())
        self.assertTrue(unknown.exists())

    def test_partial_price_is_preserved_and_counted_once_after_checkpoint_interruption(self):
        real_save = self.store.save_progress

        def fail_advance(progress):
            if progress['cursor'] == 1:
                raise CrawlerError('storage_error', 'interrupted checkpoint')
            return real_save(progress)

        self.store.save_progress = fail_advance
        reason = ['currency_missing', 'tax_missing', 'total_missing']
        observation = dict(self.observation, price_status='partial', price_reason=reason)
        with self.assertRaises(CrawlerError):
            self.store.publish(self.task, observation, self.progress)
        self.store.save_progress = real_save
        receipt = self.ns['read_json'](self.csv_path().with_suffix('.meta.json'))
        self.assertEqual(receipt['price_status'], 'partial')
        self.assertEqual(receipt['price_reason'], reason)
        _, restored = self.store.load()
        self.assertTrue(self.store.recover(restored))
        self.assertEqual(restored['stats']['price_partial'], 1)
        self.assertEqual(restored['stats']['succeeded'], 1)
        self.assertFalse(self.store.recover(restored))
        self.assertEqual(restored['stats']['price_partial'], 1)

    def test_publication_requires_explicit_price_quality_metadata(self):
        observation = dict(self.observation)
        observation.pop('price_status')
        observation.pop('price_reason')
        with self.assertRaises(CrawlerError):
            self.store.publish(self.task, observation, self.progress)
        self.assertFalse(self.csv_path().exists())

    def test_recovery_does_not_infer_quality_for_old_or_changed_price_receipt(self):
        real_save = self.store.save_progress

        def fail_advance(progress):
            if progress['cursor'] == 1:
                raise CrawlerError('storage_error', 'interrupted checkpoint')
            return real_save(progress)

        self.store.save_progress = fail_advance
        observation = dict(self.observation, price_status='partial', price_reason=['tax_missing'])
        with self.assertRaises(CrawlerError):
            self.store.publish(self.task, observation, self.progress)
        self.store.save_progress = real_save
        _, restored = self.store.load()
        old = dict(restored)
        old['publish_intent'] = dict(restored['publish_intent'])
        old['publish_intent'].pop('price_status', None)
        old['publish_intent'].pop('price_reason', None)
        with self.assertRaises(CrawlerError) as error:
            self.store.recover(old)
        self.assertEqual(error.exception.code, 'state_invalid')
        receipt_path = self.csv_path().with_suffix('.meta.json')
        receipt = self.ns['read_json'](receipt_path)
        receipt['price_status'] = 'complete'
        receipt['price_reason'] = []
        self.ns['atomic_json'](receipt_path, receipt)
        with self.assertRaises(CrawlerError):
            self.store.recover(restored)
        self.assertEqual(restored['cursor'], 0)


if __name__ == '__main__':
    unittest.main()

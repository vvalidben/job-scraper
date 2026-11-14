import json
import sqlite3
import tempfile
import unittest
from datetime import date, timedelta
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from jobspy_services import Store, SearchRunner, parse_source, canonical_url, fetch_source
from jobspy_features import (WorkspaceLock, matches_filters, validate_filters, inspect_listing,
                             work_mode, experience_level)
from jobspy_background import run_cycle, task_xml


def posting(**overrides):
    return dict(title='SOC internship', company='Example', location='Rabat', site='linkedin',
                job_url='https://example.com/jobs/1', date_posted='2026-11-10', **overrides)


def structured(**overrides):
    data = {'@type': 'JobPosting', 'title': 'SOC internship',
            'hiringOrganization': {'name': 'Example'}, 'url': 'https://example.com/jobs/1',
            'validThrough': '2026-12-01T23:59:00+01:00', 'jobLocationType': 'TELECOMMUTE',
            'baseSalary': {'currency': 'MAD', 'value': {'minValue': 5000, 'maxValue': 8000, 'unitText': 'MONTH'}}}
    data.update(overrides)
    return '<script type="application/ld+json">' + json.dumps(data) + '</script>'


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.store = Store(self.root / 'workspace.sqlite3')

    def add_job(self, **changes):
        job = posting()
        job.update(changes)
        return self.store.upsert([job])[0]

    def test_dedup_tracking_survives_refresh_and_cross_board_merge(self):
        key = self.add_job()
        self.store.annotate(key, status='Applied', notes='Call next week', recruiter='A recruiter')
        self.store.upsert([dict(posting(), job_url='https://example.com/jobs/1?utm_source=mail')])
        self.store.upsert([dict(posting(), site='indeed', job_url='https://indeed.com/viewjob?jk=xyz')])
        jobs = self.store.jobs()
        self.assertEqual(len(jobs), 1)
        self.assertEqual(jobs[0]['_notes'], 'Call next week')
        self.assertEqual(jobs[0]['_applied_on'], date.today().isoformat())
        self.assertEqual(jobs[0]['sources'], 'indeed, linkedin')

    def test_same_board_repost_kept_separate(self):
        self.add_job()
        self.store.upsert([dict(posting(), job_url='https://example.com/jobs/2')])
        self.assertEqual(len(self.store.jobs()), 2)

    def test_detected_deadline_and_manual_override(self):
        key = self.add_job(valid_through='2026-12-01T20:00:00Z')
        self.assertEqual(self.store.jobs()[0]['_deadline_origin'], 'source')
        self.store.annotate(key, deadline='2026-12-05', deadline_origin='manual')
        self.store.upsert([dict(posting(), valid_through='2026-12-10')])
        self.assertEqual(self.store.jobs()[0]['_deadline'], '2026-12-05')
        self.store.apply_check(key, dict(availability='Open', checked_at='2026-11-13',
                                      availability_note='Verified', detected_deadline='2026-12-15'))
        self.assertEqual(self.store.jobs()[0]['_deadline'], '2026-12-05')

    def test_source_deadline_updates(self):
        key = self.add_job(valid_through='2026-12-01')
        self.store.upsert([dict(posting(), valid_through='2026-12-10')])
        self.assertEqual(self.store.jobs()[0]['_deadline'], '2026-12-10')

    def test_invalid_dates_rejected_without_partial_updates(self):
        key = self.add_job()
        for fields in ({'follow_up': '2026-02-30'}, {'applied_on': '2026-01-01junk'},
                       {'interview_at': 'tomorrow'}, {'interview_at': '2026-1-1 9:00'}, {'availability': 'Maybe'}):
            with self.assertRaises(ValueError):
                self.store.annotate(key, **fields)
        self.assertEqual(self.store.jobs()[0]['_follow_up'], '')

    def test_full_backup_restore_and_safety_copy(self):
        key = self.add_job()
        self.store.annotate(key, notes='Keep me', recruiter='Recruiter', contact='example@example.com', follow_up='2026-12-01')
        self.store.set('profile', 'Python Linux')
        self.store.set('settings', {'query': 'SOC'})
        self.store.save_preset('SOC Rabat', {'query': 'SOC'}, 24)
        search = self.store.begin_search({'query': 'SOC'})
        self.store.finish_search(search, 'Finished', [])
        backup = self.store.backup(self.root / 'export.sqlite3')
        self.store.annotate(key, notes='Newer notes')
        self.store.set('profile', 'Changed')
        safety = self.store.restore(backup)
        self.assertEqual(self.store.jobs()[0]['_notes'], 'Keep me')
        self.assertEqual(self.store.jobs()[0]['_contact'], 'example@example.com')
        self.assertEqual(self.store.get('profile'), 'Python Linux')
        self.assertEqual(len(self.store.presets()), 1)
        self.assertEqual(self.store.history()[0]['summary'], 'Finished')
        self.assertEqual(Store(safety).jobs()[0]['_notes'], 'Newer notes')

    def test_invalid_restore_preserves_workspace(self):
        self.add_job()
        bad = self.root / 'bad.sqlite3'
        bad.write_text('not a database')
        with self.assertRaises(ValueError):
            self.store.restore(bad)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_damaged_json_backup_rejected(self):
        self.add_job()
        backup = self.store.backup(self.root / 'bad-data.sqlite3')
        with closing(sqlite3.connect(backup)) as db:
            db.execute("UPDATE jobs SET data='broken'")
            db.commit()
        with self.assertRaises(ValueError):
            self.store.restore(backup)
        self.assertEqual(len(self.store.jobs()), 1)

    def test_backup_to_self_rejected(self):
        with self.assertRaises(ValueError):
            self.store.backup(self.store.path)

    def test_atomic_preset_claim(self):
        self.store.save_preset('test', {'query': 'SOC'}, 1)
        with self.store.connect() as db:
            db.execute('UPDATE presets SET next_run=0')
        self.assertTrue(self.store.claim_preset('test', 100))
        self.assertFalse(Store(self.store.path).claim_preset('test', 100))

    def test_reminders_overdue_followups_and_daily_dedup(self):
        key = self.add_job()
        self.store.annotate(key, deadline='2026-11-15', follow_up='2026-11-10', interview_at='2026-11-14 09:00')
        due = self.store.due_reminders(date(2026, 11, 13))
        self.assertEqual(len(due), 3)
        for _, _, field in due:
            self.store.annotate(key, **{field: '2026-11-13'})
        self.assertEqual(self.store.due_reminders(date(2026, 11, 13)), [])
        self.store.annotate(key, status='Archived')
        self.assertEqual(self.store.due_reminders(date(2026, 11, 14)), [])

    def test_lock_prevents_overlapping_process_work(self):
        first, second = WorkspaceLock(self.store.path), WorkspaceLock(self.store.path)
        self.assertTrue(first.acquire())
        try:
            self.assertFalse(second.acquire())
            self.assertFalse(run_cycle(self.store))
        finally:
            first.release()
        self.assertTrue(second.acquire())
        second.release()

    def test_legacy_v1_migration_preserves_notes(self):
        key = self.add_job()
        self.store.annotate(key, deadline='2026-12-01', notes='Existing note')
        with self.store.connect() as db:
            names = [r[1] for r in db.execute('PRAGMA table_info(jobs)')][11:]
            for name in names:
                db.execute(f'ALTER TABLE jobs DROP COLUMN {name}')
            db.execute('PRAGMA user_version=1')
        migrated = Store(self.store.path).jobs()[0]
        self.assertEqual(migrated['_notes'], 'Existing note')
        self.assertEqual(migrated['_deadline_origin'], 'manual')
        self.assertEqual(migrated['_follow_up'], '')

    def test_background_due_search_history_notifications_and_no_duplicate_run(self):
        self.store.save_preset('Scheduled', {'internships': True, 'filters': {}}, 1)
        self.store.set('auto_check_listings', False)
        with self.store.connect() as db:
            db.execute('UPDATE presets SET next_run=0')
        class Runner:
            calls = 0
            def run(inner, settings, emit, stop):
                inner.calls += 1
                emit(('batch', [posting()]))
                emit(('complete', {'answered': 1, 'failed': [], 'cancelled': False}))
        runner, notices = Runner(), []
        self.assertTrue(run_cycle(self.store, runner, lambda *args: notices.append(args) or True))
        self.assertTrue(run_cycle(self.store, runner, lambda *args: True))
        self.assertEqual(runner.calls, 1)
        self.assertEqual(len(self.store.jobs()), 1)
        self.assertEqual(len(notices), 1)
        self.assertIn('Background:', self.store.history()[0]['summary'])
        self.assertTrue(self.store.get('background_last_run'))

    def test_background_saves_failed_tasks_for_retry(self):
        settings = dict(query='SOC', cities=['Rabat'], sites=['linkedin'], expand=False)
        self.store.save_preset('Failing', settings, 1)
        self.store.set('auto_check_listings', False)
        with self.store.connect() as db:
            db.execute('UPDATE presets SET next_run=0')
        class Runner:
            def run(self, *args):
                raise RuntimeError('Source unavailable')
        run_cycle(self.store, Runner())
        self.assertEqual(len(self.store.get('failed_tasks')), 1)
        self.assertIn('Search failed', self.store.history()[0]['summary'])


class FilterTests(unittest.TestCase):
    def test_salary_annualization_overlap_and_currency(self):
        job = dict(posting(), min_amount=5000, max_amount=8000, interval='monthly', currency='MAD')
        filters = validate_filters(dict(salary_min='70000', salary_max='100000', currency='mad', include_unknown_salary=False))
        self.assertTrue(matches_filters(job, filters))
        self.assertFalse(matches_filters(dict(job, max_amount=5500), filters))
        self.assertFalse(matches_filters(dict(job, currency='EUR'), filters))
        self.assertTrue(matches_filters(dict(job, currency='EUR'), dict(filters, include_unknown_salary=True)))

    def test_unknown_salary_and_zero_lower_limit(self):
        self.assertFalse(matches_filters(posting(), dict(salary_min='0', currency='MAD', include_unknown_salary=False)))
        self.assertTrue(matches_filters(posting(), dict(salary_min='100', currency='MAD', include_unknown_salary=True)))

    def test_invalid_salary_limits(self):
        for values in ({'salary_min': 'nan'}, {'salary_min': '-1'}, {'salary_min': '100', 'salary_max': '50', 'currency': 'MAD'}, {'salary_min': '10'}):
            with self.assertRaises(ValueError):
                validate_filters(values)

    def test_exclusions_accents_and_work_modes(self):
        job = dict(posting(), description='Cybersécurité, télétravail', company='Example Agency')
        self.assertFalse(matches_filters(job, {'exclude_keywords': 'finance;cybersecurite'}))
        self.assertFalse(matches_filters(job, {'exclude_companies': 'agency'}))
        self.assertTrue(matches_filters(job, {'work_mode': 'Remote'}))
        self.assertFalse(matches_filters(job, {'work_mode': 'On-site'}))
        self.assertEqual(work_mode(dict(posting(), is_remote=False)), 'Unknown')
        self.assertEqual(work_mode(dict(job, description='hybride, remote')), 'Hybrid')
        self.assertEqual(work_mode(dict(job, work_mode='On-site')), 'On-site')

    def test_experience_and_hide_closed(self):
        self.assertEqual(experience_level(posting()), 'Internship')
        self.assertEqual(experience_level(dict(posting(), title='Senior SOC analyst')), 'Senior')
        self.assertFalse(matches_filters(dict(posting(), _availability='Closed'), {'hide_closed': True}))


class ImportAndAvailabilityTests(unittest.TestCase):
    def test_structured_import_salary_remote_deadline(self):
        row = parse_source(structured(), 'https://example.com/jobs/1')[0]
        self.assertEqual(row['valid_through'], '2026-12-01T23:59:00+01:00')
        self.assertEqual(row['min_amount'], '5000')
        self.assertEqual(row['work_mode'], 'Remote')
        self.assertEqual(row['interval'], 'month')

    def test_rss_atom_and_malformed_json(self):
        rss = '<rss><channel><item><title>Stage SOC</title><link>/job</link><description>Hello</description></item></channel></rss>'
        self.assertEqual(parse_source(rss, 'https://example.com/feed')[0]['job_url'], 'https://example.com/job')
        atom = '<feed xmlns="http://www.w3.org/2005/Atom"><entry><title>Stage</title><link href="/stage"/><updated>2026-11-13T09:00:00Z</updated></entry></feed>'
        self.assertEqual(parse_source(atom, 'https://example.com/feed')[0]['date_posted'], '2026-11-13')
        with self.assertRaises(ValueError):
            parse_source('<script type="application/ld+json">{oops}</script>', 'https://example.com')

    def test_unsafe_xml_rejected(self):
        with self.assertRaises(ValueError):
            parse_source('<?xml version="1.0"?><!DOCTYPE rss [<!ENTITY x SYSTEM "file:///secret">]><rss/>', 'https://example.com')

    def test_http_blocks_unknown_and_removed_closed(self):
        for status, expected in ((403, 'Unknown'), (429, 'Unknown'), (500, 'Unknown'), (404, 'Closed'), (410, 'Closed')):
            result = inspect_listing(posting(), lambda _: (status, '', 'https://example.com/jobs/1'))
            self.assertEqual(result['availability'], expected)
            self.assertTrue(result['checked_at'])

    def test_source_expiry_and_manual_override_independent(self):
        result = inspect_listing(posting(), lambda _: (200, structured(), 'https://example.com/jobs/1'), date(2026, 12, 2))
        self.assertEqual(result['availability'], 'Closed')
        self.assertEqual(result['detected_deadline'], '2026-12-01')

    def test_recommended_job_cannot_confirm_original_open(self):
        text = structured(title='Different role', url='https://example.com/jobs/2')
        self.assertEqual(inspect_listing(posting(), lambda _: (200, text, 'https://example.com/jobs/1'))['availability'], 'Unknown')

    def test_explicit_closure_and_captcha(self):
        for text, state in (('This job is no longer available', 'Closed'), ('Verify you are human', 'Unknown'), ('Welcome to our careers page', 'Unknown')):
            self.assertEqual(inspect_listing(posting(), lambda _: (200, text, 'https://example.com'))['availability'], state)

    def test_timeout_unknown_and_import_http_failure(self):
        def timeout(_):
            raise TimeoutError('Timed out')
        self.assertEqual(inspect_listing(posting(), timeout)['availability'], 'Unknown')
        with patch('jobspy_services.fetch_page', return_value=(403, '', 'https://example.com')):
            with self.assertRaisesRegex(ValueError, 'HTTP 403'):
                fetch_source('https://example.com')


class RecoveryTests(unittest.TestCase):
    class Stop:
        def is_set(self):
            return False
        def wait(self, delay):
            return False

    def test_transient_failure_retries_then_keeps_results(self):
        import pandas as pd
        calls = []
        def scrape(**params):
            calls.append(params)
            if len(calls) == 1:
                raise TimeoutError('connection timeout')
            return pd.DataFrame([posting()])
        events = []
        settings = dict(query='SOC', expand=False, cities=['Rabat'], sites=['linkedin'], limit=10)
        SearchRunner(scrape=scrape, pacing=0).run(settings, events.append, self.Stop())
        self.assertEqual(len(calls), 2)
        self.assertEqual(events[-1][1]['answered'], 1)
        self.assertTrue(any(e[0] == 'batch' for e in events))

    def test_block_cooldown_preserves_both_failed_tasks(self):
        calls = []
        def scrape(**params):
            calls.append(params)
            raise RuntimeError('HTTP 429 blocked')
        events = []
        settings = dict(query='SOC', expand=False, cities=['Rabat', 'Casablanca'], sites=['linkedin'], limit=10)
        SearchRunner(scrape=scrape, pacing=0).run(settings, events.append, self.Stop())
        self.assertEqual(len(calls), 1)
        self.assertEqual(len(events[-1][1]['failed']), 2)

    def test_cancel_sends_completion(self):
        import threading
        stop = threading.Event()
        stop.set()
        events = []
        settings = dict(query='SOC', expand=False, cities=['Rabat'], sites=['linkedin'], limit=10)
        SearchRunner(scrape=lambda **_: self.fail('Cancelled search made request')).run(settings, events.append, stop)
        self.assertTrue(events[-1][1]['cancelled'])
        self.assertEqual(len(events[-1][1]['failed']), 1)

    def test_task_xml_quotes_paths_and_limits_instances(self):
        xml = task_xml(Path('C:/space & test/workspace.sqlite3'), 'S-1-5-21-123', 'C:/Python Path/pythonw.exe')
        root = ET.fromstring(xml)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(root.find('.//t:MultipleInstancesPolicy', ns).text, 'IgnoreNew')
        self.assertEqual(root.find('.//t:LogonType', ns).text, 'InteractiveToken')
        self.assertIn('--once', root.find('.//t:Arguments', ns).text)
        self.assertIn('"C:', root.find('.//t:Arguments', ns).text)


if __name__ == '__main__':
    unittest.main()

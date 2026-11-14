"""Run due searches/reminders without opening Tk; Windows Task Scheduler entry point."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import logging
import os
import subprocess
import sys
import threading
import time
from datetime import datetime, timedelta, timezone, date
from pathlib import Path
from xml.etree import ElementTree as ET

from jobspy_features import WorkspaceLock, inspect_listing, matches_filters, native_notify
from jobspy_services import Store, SearchRunner, configure_logging, internship_evidence

BASE = Path(__file__).resolve().parent


def task_name(database):
    suffix = hashlib.sha256(str(Path(database).resolve()).casefold().encode()).hexdigest()[:12]
    return f'JobSpy Studio {suffix}'


def task_xml(database, user_sid, executable=None):
    ns = 'http://schemas.microsoft.com/windows/2004/02/mit/task'
    ET.register_namespace('', ns)
    root = ET.Element(f'{{{ns}}}Task', version='1.2')
    def add(parent, name, text=None):
        element = ET.SubElement(parent, f'{{{ns}}}{name}')
        element.text = text
        return element
    info = add(root, 'RegistrationInfo')
    add(info, 'Description', 'JobSpy Studio scheduled searches, local reminders and listing checks.')
    triggers = add(root, 'Triggers')
    trigger = add(triggers, 'TimeTrigger')
    repetition = add(trigger, 'Repetition')
    add(repetition, 'Interval', 'PT5M')
    add(repetition, 'StopAtDurationEnd', 'false')
    add(trigger, 'StartBoundary', (datetime.now() + timedelta(minutes=1)).isoformat(timespec='seconds'))
    add(trigger, 'Enabled', 'true')
    login = add(triggers, 'LogonTrigger')
    add(login, 'Enabled', 'true')
    add(login, 'UserId', user_sid)
    principals = add(root, 'Principals')
    principal = add(principals, 'Principal')
    principal.set('id', 'Author')
    add(principal, 'UserId', user_sid)
    add(principal, 'LogonType', 'InteractiveToken')
    add(principal, 'RunLevel', 'LeastPrivilege')
    settings = add(root, 'Settings')
    for key, value in (('MultipleInstancesPolicy', 'IgnoreNew'), ('DisallowStartIfOnBatteries', 'false'),
                       ('StopIfGoingOnBatteries', 'false'), ('StartWhenAvailable', 'true'),
                       ('ExecutionTimeLimit', 'PT2H'), ('Enabled', 'true')):
        add(settings, key, value)
    actions = add(root, 'Actions')
    actions.set('Context', 'Author')
    action = add(actions, 'Exec')
    python = Path(executable or sys.executable)
    if python.name.casefold() == 'python.exe' and python.with_name('pythonw.exe').exists():
        python = python.with_name('pythonw.exe')
    add(action, 'Command', str(python))
    add(action, 'Arguments', subprocess.list2cmdline([str(BASE / 'jobspy_background.py'), '--once', '--database', str(Path(database).resolve())]))
    add(action, 'WorkingDirectory', str(BASE))
    return ET.tostring(root, encoding='unicode')


def scheduler_command(*arguments, check=True):
    if os.name != 'nt':
        raise RuntimeError('Background task registration requires Windows.')
    result = subprocess.run(['schtasks.exe', *arguments], capture_output=True, text=True,
                            errors='replace', creationflags=subprocess.CREATE_NO_WINDOW, timeout=30)
    if check and result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip() or 'Windows rejected the scheduled task.')
    return result


def background_status(store):
    if os.name != 'nt':
        return False
    return scheduler_command('/Query', '/TN', task_name(store.path), check=False).returncode == 0


def enable_background(store):
    identity = subprocess.run(['whoami.exe', '/user', '/fo', 'csv', '/nh'], capture_output=True,
                              text=True, check=True, creationflags=subprocess.CREATE_NO_WINDOW, timeout=10)
    sid = next(csv.reader(io.StringIO(identity.stdout.strip())))[1]
    target = store.path.parent / 'background-task.xml'
    target.write_text(task_xml(store.path, sid), encoding='utf-16')
    scheduler_command('/Create', '/TN', task_name(store.path), '/XML', str(target), '/F')
    store.set('background_enabled', True)
    scheduler_command('/Run', '/TN', task_name(store.path))


def disable_background(store):
    store.set('background_enabled', False)
    name = task_name(store.path)
    scheduler_command('/End', '/TN', name, check=False)
    if background_status(store):
        scheduler_command('/Delete', '/TN', name, '/F')


def run_cycle(store, runner=None, notify=native_notify, checker=inspect_listing, stop=None):
    """Injectable for offline tests. One OS lock coordinates with the desktop app."""
    stop = stop or threading.Event()
    lock = WorkspaceLock(store.path)
    if not lock.acquire():
        return False
    logger = logging.getLogger('studio')
    try:
        store.daily_backup()
        runner = runner or SearchRunner()
        now = time.time()
        due = [p for p in store.presets() if p['interval_hours'] and p['next_run'] <= now]
        for preset in due:
            if stop.is_set() or not store.claim_preset(preset['name'], now):
                continue
            settings = preset['settings']
            search_id = store.begin_search(settings)
            new_ids, result = set(), {}
            def emit(event):
                if event[0] == 'batch':
                    new_ids.update(store.upsert(event[1]))
                elif event[0] == 'complete':
                    result.update(event[1])
                elif event[0] == 'note':
                    logger.info('Background: %s', event[1])
            try:
                runner.run(settings, emit, stop)
            except Exception as exc:
                logger.exception('Background search failed')
                from jobspy_services import build_tasks
                result.update(failed=build_tasks(settings), fatal=str(exc))
            failed = result.get('failed', [])
            summary = f"Background: {len(new_ids)} new jobs; {result.get('answered', 0)} requests completed; {len(failed)} failed."
            if result.get('fatal'):
                summary += ' Search failed: ' + result['fatal']
            store.finish_search(search_id, summary, failed)
            store.set('failed_tasks', failed)
            store.set('last_search_settings', settings)
            if new_ids and store.get('notifications', True):
                matches = [j for j in store.jobs() if j['_key'] in new_ids
                           and (not settings.get('internships') or internship_evidence(j))
                           and matches_filters(j, settings.get('filters', {}))]
                if matches:
                    notify('New opportunities', f'{len(matches)} new matching jobs saved for {preset["name"]}.')
        if store.get('notifications', True):
            for kind, job, field in store.due_reminders():
                if notify(kind, f'{job["title"]} — {job.get("company", "")}'):
                    store.annotate(job['_key'], **{field: date.today().isoformat()})
        if store.get('auto_check_listings', True):
            cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
            stale = [j for j in store.jobs() if j.get('job_url') and j['_checked_at'] < cutoff
                     and j['_availability'] != 'Closed' and j['_status'] not in ('Archived', 'Rejected')]
            stale.sort(key=lambda j: j['_checked_at'])
            for job in stale[:5]:
                if stop.is_set():
                    break
                result = checker(job)
                store.apply_check(job['_key'], result)
                if 'HTTP 429' in result['availability_note'] or 'HTTP 403' in result['availability_note']:
                    break
                if stop.wait(2):
                    break
        store.set('background_last_run', datetime.now().isoformat(timespec='seconds'))
        return True
    finally:
        lock.release()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--database', type=Path, default=BASE / 'data' / 'studio.sqlite3')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--once', action='store_true')
    group.add_argument('--enable', action='store_true')
    group.add_argument('--disable', action='store_true')
    group.add_argument('--status', action='store_true')
    args = parser.parse_args()
    store = Store(args.database)
    logger = configure_logging(args.database.parent.parent)
    try:
        if args.enable:
            enable_background(store)
        elif args.disable:
            disable_background(store)
        elif args.status:
            print('Installed' if background_status(store) else 'Not installed')
            print('Last run:', store.get('background_last_run', 'Never'))
        elif store.get('background_enabled', False):
            run_cycle(store)
    except Exception:
        logger.exception('Background operation failed')
        if Path(sys.executable).name.casefold() != 'pythonw.exe':
            raise
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())

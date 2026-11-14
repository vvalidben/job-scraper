"""Filtering, listing checks and cross-process coordination for Studio."""
from __future__ import annotations

import math
import os
import re
from datetime import date, datetime, timezone
from pathlib import Path


class WorkspaceLock:
    """OS-backed lock; a crashed process cannot leave a stale search lock."""
    def __init__(self, database):
        self.path = Path(str(database) + '.work.lock')
        self.handle = None

    def acquire(self):
        if self.handle is not None:
            return False
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open('a+b')
        if handle.tell() == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            handle.close()
            return False
        self.handle = handle
        return True

    def release(self):
        if self.handle is None:
            return
        self.handle.seek(0)
        if os.name == 'nt':
            import msvcrt
            msvcrt.locking(self.handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl
            fcntl.flock(self.handle, fcntl.LOCK_UN)
        self.handle.close()
        self.handle = None


def iso_date(value):
    try:
        return date.fromisoformat(str(value or '')[:10]).isoformat()
    except ValueError:
        return ''


def number(value):
    try:
        result = float(value)
        return result if math.isfinite(result) else None
    except (TypeError, ValueError):
        return None


def validate_filters(filters):
    result = dict(filters)
    for key in ('salary_min', 'salary_max'):
        text = str(result.get(key, '')).strip()
        if text and (number(text) is None or number(text) < 0):
            raise ValueError('Salary limits must be positive numbers or blank.')
        result[key] = text
    lo, hi = number(result['salary_min']), number(result['salary_max'])
    if lo is not None and hi is not None and lo > hi:
        raise ValueError('Minimum salary cannot exceed maximum salary.')
    result['currency'] = str(result.get('currency', '')).strip().upper()
    if (lo is not None or hi is not None) and not result['currency']:
        raise ValueError('Choose a currency for salary filtering.')
    return result


def work_mode(job):
    from jobspy_services import normalize
    explicit = normalize(job.get('work_mode') or job.get('job_location_type'))
    text = normalize(' '.join(str(job.get(k, '')) for k in ('title', 'location', 'description')))
    if explicit in ('hybrid', 'hybride'):
        return 'Hybrid'
    if explicit in ('remote', 'telecommute'):
        return 'Remote'
    if explicit in ('on-site', 'onsite', 'on site'):
        return 'On-site'
    if 'hybrid' in text or 'hybride' in text:
        return 'Hybrid'
    if str(job.get('is_remote', '')).lower() in ('true', '1'):
        return 'Remote'
    if re.search(r'\b(remote|teletravail|a distance)\b', text):
        return 'Remote'
    if re.search(r'\b(on.site|presentiel)\b', text):
        return 'On-site'
    # A false remote flag alone does not rule out hybrid work.
    return 'Unknown'


def experience_level(job):
    from jobspy_services import normalize, internship_evidence
    text = normalize(' '.join(str(job.get(k, '')) for k in ('job_level', 'title')))
    if internship_evidence(job):
        return 'Internship'
    for level, pattern in (('Senior', r'\b(senior|sr|lead|principal|manager|director|expert)\b'),
                           ('Entry', r'\b(junior|jr|entry|debutant|graduate|sans experience)\b'),
                           ('Mid', r'\b(mid|intermediate|confirme|associate)\b')):
        if re.search(pattern, text):
            return level
    return 'Unknown'


def matches_filters(job, filters):
    from jobspy_services import normalize
    text = normalize(' '.join(str(job.get(k, '')) for k in ('title', 'company', 'description', 'skills')))
    for term in re.split(r'[;\n]', filters.get('exclude_keywords', '')):
        if term.strip() and normalize(term) in text:
            return False
    company = normalize(job.get('company'))
    for term in re.split(r'[;\n]', filters.get('exclude_companies', '')):
        if term.strip() and normalize(term) in company:
            return False
    mode = filters.get('work_mode', 'Any')
    if mode != 'Any' and work_mode(job) != mode:
        return False
    level = filters.get('experience', 'Any')
    if level != 'Any' and experience_level(job) != level:
        return False
    if filters.get('hide_closed') and job.get('_availability') == 'Closed':
        return False
    lo, hi = number(filters.get('salary_min')), number(filters.get('salary_max'))
    if lo is None and hi is None:
        return True
    a, b = number(job.get('min_amount')), number(job.get('max_amount'))
    a = a if a is not None else b
    b = b if b is not None else a
    period = str(job.get('interval', '')).lower().removeprefix('compensationinterval.')
    factors = {'yearly': 1, 'year': 1, 'annual': 1, 'monthly': 12, 'month': 12}
    if a is None or period not in factors or str(job.get('currency', '')).upper() != filters.get('currency', '').upper():
        return bool(filters.get('include_unknown_salary', True))
    a, b = a * factors[period], b * factors[period]
    return (lo is None or b >= lo) and (hi is None or a <= hi)


def native_notify(title, message):
    """Return whether Windows accepted a toast; callers retain a local fallback."""
    if os.name != 'nt':
        return False
    try:
        import base64
        import subprocess
        from xml.etree import ElementTree as ET
        toast = ET.Element('toast', duration='short')
        binding = ET.SubElement(ET.SubElement(toast, 'visual'), 'binding', template='ToastGeneric')
        for value in (title, message):
            ET.SubElement(binding, 'text').text = re.sub(r'[\x00-\x08\x0b\x0c\x0e-\x1f]', '', str(value))[:500]
        payload = base64.b64encode(ET.tostring(toast, encoding='utf-8')).decode('ascii')
        # Posting text is data on stdin, never interpolated into shell code.
        script = """
$ErrorActionPreference = 'Stop'
[Windows.UI.Notifications.ToastNotificationManager, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.UI.Notifications.ToastNotification, Windows.UI.Notifications, ContentType=WindowsRuntime] > $null
[Windows.Data.Xml.Dom.XmlDocument, Windows.Data.Xml.Dom.XmlDocument, ContentType=WindowsRuntime] > $null
$xml = New-Object Windows.Data.Xml.Dom.XmlDocument
$xml.LoadXml([Text.Encoding]::UTF8.GetString([Convert]::FromBase64String([Console]::ReadLine())))
$toast = [Windows.UI.Notifications.ToastNotification]::new($xml)
[Windows.UI.Notifications.ToastNotificationManager]::CreateToastNotifier('JobSpy Studio').Show($toast)
"""
        result = subprocess.run(['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand',
                                 base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
                                input=payload + '\n', capture_output=True, text=True,
                                creationflags=subprocess.CREATE_NO_WINDOW, timeout=15)
        if result.returncode:
            raise RuntimeError(result.stderr.strip())
        return result.returncode == 0
    except Exception:
        import logging
        logging.getLogger('studio').exception('Windows notification failed')
        return False


def inspect_listing(job, fetch=None, today=None):
    """HTTP blocks are unknown; 404/410 or explicit expiry confirms closure."""
    from jobspy_services import fetch_page, parse_source, canonical_url, normalize
    checked = datetime.now(timezone.utc).isoformat(timespec='seconds')
    result = dict(checked_at=checked, availability='Unknown', availability_note='')
    try:
        status, text, url = (fetch or fetch_page)(job.get('job_url', ''))
        if status in (404, 410):
            result.update(availability='Closed', availability_note=f'Original posting returned HTTP {status}.')
            return result
        if status != 200:
            result['availability_note'] = f'HTTP {status}; availability could not be verified.'
            return result
        if re.search(r'captcha|verify you are human|access denied|security challenge', text, re.I):
            result['availability_note'] = 'The source blocked verification.'
            return result
        try:
            entries = parse_source(text, url)
        except ValueError:
            entries = []
        # Do not use a recommended role on a removed listing as evidence.
        matches = [j for j in entries if canonical_url(j.get('job_url')) == canonical_url(job.get('job_url'))
                   or (normalize(j.get('title')) == normalize(job.get('title'))
                       and normalize(j.get('company')) == normalize(job.get('company')))]
        if matches:
            posting = matches[0]
            deadline = iso_date(posting.get('valid_through'))
            result.update(availability='Open', availability_note='Matching JobPosting data found.')
            if deadline:
                result['detected_deadline'] = deadline
                if deadline < (today or date.today()).isoformat():
                    result.update(availability='Closed', availability_note='The source application deadline has passed.')
        else:
            from bs4 import BeautifulSoup
            visible = normalize(BeautifulSoup(text, 'html.parser').get_text(' ', strip=True))
            phrases = ('no longer accepting applications', 'this job has expired',
                       'this job is no longer available', 'cette offre a expire',
                       "cette offre n'est plus disponible", 'cette offre est pourvue')
            if any(p in visible for p in phrases):
                result.update(availability='Closed', availability_note='The posting explicitly says it is closed.')
            else:
                result['availability_note'] = 'Page loaded, but no matching posting or closure evidence was found.'
    except Exception as exc:
        result['availability_note'] = f'Could not verify: {exc}'[:300]
    return result

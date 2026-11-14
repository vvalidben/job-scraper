"""Local storage, search recovery, matching and structured source imports."""
from __future__ import annotations

import hashlib
import ipaddress
import json
import logging
from logging.handlers import RotatingFileHandler
import re
import socket
import sqlite3
import threading
import time
import unicodedata
from contextlib import contextmanager, closing
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from xml.etree import ElementTree as ET

from jobspy_features import iso_date

STATUSES = ('Interested', 'Applied', 'Interview', 'Offer', 'Rejected', 'Archived')
SKILLS = {
    'Python': ('python',), 'SQL': ('sql', 'postgresql', 'mysql'),
    'Linux': ('linux', 'ubuntu', 'debian', 'red hat'), 'Windows': ('windows',),
    'Networking': ('networking', 'tcp/ip', 'reseaux', 'network security'),
    'SIEM': ('siem',), 'Splunk': ('splunk',), 'Microsoft Sentinel': ('sentinel',),
    'SOC': ('soc', 'security operations center', 'security operations centre'),
    'Incident response': ('incident response', 'reponse aux incidents'),
    'Threat hunting': ('threat hunting',), 'Penetration testing': ('pentest', 'penetration testing', 'test intrusion'),
    'Vulnerability management': ('vulnerability', 'vulnerabilites', 'vulnerability management'),
    'Wireshark': ('wireshark',), 'Nmap': ('nmap',), 'Burp Suite': ('burp suite',),
    'Metasploit': ('metasploit',), 'Firewalls': ('firewall', 'pare-feu', 'fortinet', 'palo alto'),
    'Active Directory': ('active directory',), 'IAM': ('iam', 'identity and access', 'gestion des identites'),
    'ISO 27001': ('iso 27001', 'iso27001',), 'GRC': ('grc', 'gouvernance', 'governance risk'),
    'NIST': ('nist',), 'AWS': ('aws', 'amazon web services'), 'Azure': ('azure',),
    'Docker': ('docker',), 'Kubernetes': ('kubernetes', 'k8s'), 'Git': ('git', 'github', 'gitlab'),
    'Java': ('java',), 'JavaScript': ('javascript', 'typescript'), 'PowerShell': ('powershell',),
    'Bash': ('bash',), 'Excel': ('excel',), 'Power BI': ('power bi',),
    'French': ('french', 'francais', 'francaise'), 'English': ('english', 'anglais', 'anglaise'),
    'Security+': ('security+',), 'CISSP': ('cissp',), 'CEH': ('ceh',), 'OSCP': ('oscp',),
}


def normalize(value):
    return ' '.join(''.join(c for c in unicodedata.normalize('NFKD', str(value or '').casefold())
                           if not unicodedata.combining(c)).split())


def clean(value):
    if value is None:
        return ''
    if isinstance(value, (list, tuple)):
        return ', '.join(clean(x) for x in value)
    value = str(value).strip()
    return '' if value.casefold() in ('nan', 'none', '<na>', 'nat') else value


def canonical_url(value):
    try:
        p = urlsplit(clean(value))
        if p.scheme not in ('http', 'https') or not p.hostname or p.username or p.password:
            return ''
        # IDs and other meaningful query arguments are preserved.
        args = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
                if not k.lower().startswith('utm_') and k.lower() not in
                ('trk', 'trackingid', 'refid', 'ref', 'source', 'src', 'from')]
        host = p.hostname.lower().removeprefix('www.')
        port = p.port
        if port and port not in (80, 443):
            host += f':{port}'
        path = p.path.rstrip('/') or '/'
        if host.endswith('linkedin.com'):
            match = re.search(r'/jobs/view/(?:.*-)?(\d+)$', path)
            if match:
                return f'https://linkedin.com/jobs/view/{match[1]}'
        return urlunsplit(('https', host, path, urlencode(sorted(args)), ''))
    except (ValueError, TypeError):
        return ''


def fingerprint(job):
    parts = [normalize(job.get(k)) for k in ('title', 'company', 'location')]
    return hashlib.sha256('|'.join(parts).encode()).hexdigest() if all(parts) else ''


def internship_evidence(job):
    strong = r'\b(internship|intern|interns|stagiaire|stagiaires|stage|stages|pfe|pfa|internat)\b'
    for field, label in (('job_type', 'Job type'), ('job_level', 'Experience level'), ('title', 'Title')):
        if re.search(strong, normalize(job.get(field))):
            return f'{label}: internship / stage label'
    # Require an explicit offer, not incidental mentions such as "supervise interns".
    text = normalize(job.get('description'))
    patterns = (r'\b(?:offre|proposons|propose|recherche|cherchons|recrutons)\b.{0,55}\b(?:un |une |des )?(?:stage|stagiaire)\b',
                r'\b(?:stage de fin d.etudes|stage pfe|stage pfa|convention de stage)\b',
                r'\b(?:this (?:role|position) is (?:an? )?internship|we (?:are seeking|are hiring|seek) an? intern)\b')
    if any(re.search(p, text) for p in patterns):
        return 'Description: explicit internship wording (review original)'
    return ''


def expand_queries(query, expand=True):
    terms = [x.strip() for x in re.split(r'[;|\n]', query) if x.strip()]
    if not terms:
        raise ValueError('Enter at least one search term.')
    if expand and len(terms) == 1:
        n = normalize(terms[0])
        if any(x in n for x in ('cyber', 'securite informatique', 'soc')):
            terms += ['cybersecurity', 'cybersécurité', 'sécurité informatique', 'SOC analyst']
    return list(dict.fromkeys(terms))[:8]


def extract_skills(text):
    text = normalize(text)
    return {name for name, aliases in SKILLS.items() if any(
        re.search(r'(?<!\w)' + re.escape(alias) + r'(?!\w)', text) for alias in aliases)}


def match_cv(job, profile):
    job_skills = extract_skills(' '.join(clean(job.get(k)) for k in ('title', 'description', 'skills')))
    cv_skills = extract_skills(profile)
    shared = sorted(job_skills & cv_skills)
    missing = sorted(job_skills - cv_skills)
    return {'score': round(100 * len(shared) / len(job_skills)) if job_skills and profile.strip() else None,
            'matched': shared, 'missing': missing, 'mentioned': sorted(job_skills)}


def read_profile(path):
    path = Path(path)
    if path.stat().st_size > 10_000_000:
        raise ValueError('Choose a CV smaller than 10 MB.')
    if path.suffix.lower() == '.pdf':
        from pypdf import PdfReader
        text = '\n'.join(page.extract_text() or '' for page in PdfReader(path).pages)
    elif path.suffix.lower() == '.docx':
        from zipfile import ZipFile
        with ZipFile(path) as archive:
            info = archive.getinfo('word/document.xml')
            if info.file_size > 10_000_000:
                raise ValueError('This document is too large.')
            root = ET.fromstring(archive.read(info))
            text = '\n'.join(''.join(p.itertext()) for p in root.iter(
                '{http://schemas.openxmlformats.org/wordprocessingml/2006/main}p'))
    else:
        text = path.read_text(encoding='utf-8-sig')
    if not text.strip():
        raise ValueError('No text found. For a scanned PDF, paste the CV text instead.')
    return text[:100_000]


def configure_logging(base):
    folder = Path(base) / 'data'
    folder.mkdir(exist_ok=True)
    logger = logging.getLogger('studio')
    logger.setLevel(logging.INFO)
    if not logger.handlers:
        handler = RotatingFileHandler(folder / 'studio.log', maxBytes=1_000_000, backupCount=3, encoding='utf-8')
        handler.setFormatter(logging.Formatter('%(asctime)s %(levelname)s %(message)s'))
        logger.addHandler(handler)
    return logger


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as db:
            if db.execute('PRAGMA user_version').fetchone()[0] > 2:
                raise ValueError('This workspace needs a newer version of JobSpy Studio.')
            db.executescript('''
                CREATE TABLE IF NOT EXISTS jobs (
                  key TEXT PRIMARY KEY, fingerprint TEXT, data TEXT NOT NULL,
                  first_seen TEXT NOT NULL, last_seen TEXT NOT NULL, seen INTEGER DEFAULT 0,
                  favorite INTEGER DEFAULT 0, status TEXT DEFAULT 'Interested',
                  notes TEXT DEFAULT '', deadline TEXT DEFAULT '', reminder TEXT DEFAULT '');
                CREATE INDEX IF NOT EXISTS job_fingerprint ON jobs(fingerprint);
                CREATE TABLE IF NOT EXISTS aliases (url TEXT PRIMARY KEY, job_key TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS searches (id INTEGER PRIMARY KEY, started TEXT,
                  finished TEXT, settings TEXT, summary TEXT, failed TEXT);
                CREATE TABLE IF NOT EXISTS presets (name TEXT PRIMARY KEY, settings TEXT NOT NULL,
                  interval_hours REAL DEFAULT 0, next_run REAL DEFAULT 0);
            ''')
            columns = {r[1] for r in db.execute('PRAGMA table_info(jobs)')}
            additions = {'applied_on': '', 'recruiter': '', 'contact': '', 'interview_at': '',
                         'follow_up': '', 'follow_reminder': '', 'interview_reminder': '',
                         'checked_at': '', 'availability': 'Unknown', 'availability_note': '',
                         'deadline_origin': 'manual'}
            for name, default in additions.items():
                if name not in columns:
                    db.execute(f"ALTER TABLE jobs ADD COLUMN {name} TEXT NOT NULL DEFAULT '{default}'")
            db.execute('PRAGMA user_version = 2')

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def get(self, key, default=None):
        with self.connect() as db:
            row = db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set(self, key, value):
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value, ensure_ascii=False)))

    def upsert(self, rows):
        new = []
        now = datetime.now(timezone.utc).isoformat(timespec='seconds')
        with self.connect() as db:
            for raw in rows:
                job = {k: clean(v) for k, v in raw.items() if not k.startswith('_')}
                if not job.get('title'):
                    continue
                url = canonical_url(job.get('job_url'))
                fp = fingerprint(job)
                row = db.execute('SELECT jobs.* FROM jobs JOIN aliases ON jobs.key=aliases.job_key WHERE url=?', (url,)).fetchone() if url else None
                if row is None and fp:
                    # Cross-board duplicates only; same-board reposts keep their separate IDs.
                    candidates = db.execute('SELECT * FROM jobs WHERE fingerprint=?', (fp,)).fetchall()
                    for candidate in candidates:
                        old = json.loads(candidate['data'])
                        if not url or canonical_url(old.get('job_url')) == url:
                            row = candidate
                            break
                        if old.get('site') and job.get('site') and old['site'] != job['site']:
                            a, b = old.get('date_posted', '')[:10], job.get('date_posted', '')[:10]
                            if a and b and a == b:
                                row = candidate
                                break
                key = row['key'] if row else hashlib.sha256((url or fp or json.dumps(job, sort_keys=True)).encode()).hexdigest()
                if row:
                    old = json.loads(row['data'])
                    sources = set(old.get('sources', old.get('site', '')).split(', ')) | {job.get('site', '')}
                    for name, value in job.items():
                        if value and (name != 'description' or len(value) > len(old.get(name, ''))):
                            if name not in ('job_url', 'site') or not old.get(name):
                                old[name] = value
                    old['sources'] = ', '.join(sorted(s for s in sources if s))
                    db.execute('UPDATE jobs SET data=?, last_seen=? WHERE key=?', (json.dumps(old, ensure_ascii=False), now, key))
                else:
                    job['sources'] = job.get('site', '')
                    db.execute('INSERT INTO jobs(key,fingerprint,data,first_seen,last_seen) VALUES (?,?,?,?,?)',
                               (key, fp, json.dumps(job, ensure_ascii=False), now, now))
                    new.append(key)
                if url:
                    db.execute('INSERT OR IGNORE INTO aliases VALUES (?,?)', (url, key))
                detected = iso_date(job.get('valid_through') or job.get('application_deadline'))
                if detected and (not row or row['deadline_origin'] != 'manual' or not row['deadline']):
                    db.execute("UPDATE jobs SET deadline=?, deadline_origin='source', reminder='' WHERE key=? AND deadline!=?",
                               (detected, key, detected))
        return new

    def jobs(self):
        with self.connect() as db:
            rows = db.execute('SELECT * FROM jobs ORDER BY first_seen DESC, key').fetchall()
        return [dict(json.loads(r['data']), **{'_' + k: r[k] for k in r.keys() if k not in ('data', 'fingerprint')}) for r in rows]

    def annotate(self, key, **fields):
        allowed = {'seen', 'favorite', 'status', 'notes', 'deadline', 'reminder', 'deadline_origin',
                   'applied_on', 'recruiter', 'contact', 'interview_at', 'follow_up',
                   'follow_reminder', 'interview_reminder', 'checked_at', 'availability', 'availability_note'}
        if not fields or not set(fields) <= allowed:
            raise ValueError('Invalid job fields.')
        if 'status' in fields and fields['status'] not in STATUSES:
            raise ValueError('Invalid application status.')
        for name in ('deadline', 'applied_on', 'follow_up'):
            if fields.get(name) and iso_date(fields[name]) != fields[name]:
                raise ValueError(f'Use YYYY-MM-DD for {name.replace("_", " ")}.')
        if fields.get('interview_at'):
            parsed = datetime.strptime(fields['interview_at'], '%Y-%m-%d %H:%M')
            if parsed.strftime('%Y-%m-%d %H:%M') != fields['interview_at']:
                raise ValueError('Use YYYY-MM-DD HH:MM for the interview.')
        if 'availability' in fields and fields['availability'] not in ('Open', 'Closed', 'Unknown'):
            raise ValueError('Invalid availability.')
        with self.connect() as db:
            if fields.get('status') == 'Applied' and 'applied_on' not in fields:
                db.execute("UPDATE jobs SET applied_on=? WHERE key=? AND applied_on=''", (date.today().isoformat(), key))
            db.execute('UPDATE jobs SET ' + ','.join(f'{k}=?' for k in fields) + ' WHERE key=?', (*fields.values(), key))

    def apply_check(self, key, result):
        fields = {k: result[k] for k in ('checked_at', 'availability', 'availability_note')}
        with self.connect() as db:
            row = db.execute('SELECT deadline,deadline_origin FROM jobs WHERE key=?', (key,)).fetchone()
        detected = result.get('detected_deadline')
        if row and detected and (not row['deadline'] or row['deadline_origin'] != 'manual'):
            fields.update(deadline=detected, deadline_origin='source', reminder='')
        self.annotate(key, **fields)

    def backup(self, destination):
        destination = Path(destination)
        if destination.resolve() == self.path.resolve():
            raise ValueError('Choose a different file for the backup.')
        destination.parent.mkdir(parents=True, exist_ok=True)
        import os
        import tempfile
        fd, temporary = tempfile.mkstemp(prefix='.studio-backup-', dir=destination.parent)
        os.close(fd)
        try:
            with self.connect() as source, closing(sqlite3.connect(temporary)) as target:
                source.backup(target)
            os.replace(temporary, destination)
        finally:
            Path(temporary).unlink(missing_ok=True)
        return destination

    @staticmethod
    def validate_backup(path):
        path = Path(path).resolve()
        if not path.is_file():
            raise ValueError('Backup file not found.')
        try:
            with closing(sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)) as db:
                if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                    raise ValueError('Backup failed its database integrity check.')
                if db.execute('PRAGMA user_version').fetchone()[0] not in (1, 2):
                    raise ValueError('Unsupported workspace backup version.')
                expected = {
                    'jobs': {'key', 'fingerprint', 'data', 'first_seen', 'last_seen', 'seen', 'favorite', 'status', 'notes', 'deadline', 'reminder'},
                    'aliases': {'url', 'job_key'}, 'meta': {'key', 'value'},
                    'searches': {'id', 'started', 'finished', 'settings', 'summary', 'failed'},
                    'presets': {'name', 'settings', 'interval_hours', 'next_run'},
                }
                for table, columns in expected.items():
                    if not columns <= {r[1] for r in db.execute(f'PRAGMA table_info({table})')}:
                        raise ValueError('This is not a complete Studio workspace backup.')
                for (payload,) in db.execute('SELECT data FROM jobs'):
                    if not isinstance(json.loads(payload), dict):
                        raise ValueError('Invalid job data in backup.')
                for (payload,) in db.execute('SELECT value FROM meta'):
                    json.loads(payload)
                for (payload,) in db.execute('SELECT settings FROM presets'):
                    if not isinstance(json.loads(payload), dict):
                        raise ValueError('Invalid saved search in backup.')
                return db.execute('SELECT count(*) FROM jobs').fetchone()[0]
        except (sqlite3.Error, json.JSONDecodeError) as exc:
            raise ValueError('Invalid or damaged Studio backup.') from exc

    def restore(self, source):
        source = Path(source).resolve()
        if source == self.path.resolve():
            raise ValueError('The selected file is already the active workspace.')
        self.validate_backup(source)
        safety = self.backup(self.path.parent / 'backups' / f'pre-restore-{datetime.now():%Y%m%d-%H%M%S-%f}.sqlite3')
        with closing(sqlite3.connect(source.as_uri() + '?mode=ro', uri=True)) as backup, self.connect() as target:
            backup.backup(target)
        self.__init__(self.path)
        return safety

    def daily_backup(self):
        folder = self.path.parent / 'backups'
        target = folder / f'automatic-{date.today().isoformat()}.sqlite3'
        if not target.exists():
            self.backup(target)
            for old in sorted(folder.glob('automatic-*.sqlite3'), reverse=True)[7:]:
                old.unlink()
        return target

    def due_reminders(self, today=None):
        today = today or date.today()
        stamp = today.isoformat()
        soon = (today + timedelta(days=3)).isoformat()
        reminders = []
        for job in self.jobs():
            if job['_status'] in ('Rejected', 'Archived'):
                continue
            for kind, field, due in (
                ('Application deadline', 'reminder', stamp <= job['_deadline'] <= soon),
                ('Follow-up due', 'follow_reminder', bool(job['_follow_up']) and job['_follow_up'] <= stamp),
                ('Interview coming up', 'interview_reminder', stamp <= job['_interview_at'][:10] <= (today + timedelta(days=1)).isoformat())):
                if due and job['_' + field] != stamp:
                    reminders.append((kind, job, field))
        return reminders

    def claim_preset(self, name, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            return db.execute('UPDATE presets SET next_run=? + interval_hours*3600 WHERE name=? AND interval_hours>0 AND next_run<=?',
                              (now, name, now)).rowcount == 1

    def begin_search(self, settings):
        with self.connect() as db:
            cur = db.execute('INSERT INTO searches(started,settings) VALUES (?,?)',
                             (datetime.now(timezone.utc).isoformat(), json.dumps(settings)))
            return cur.lastrowid

    def finish_search(self, search_id, summary, failed):
        with self.connect() as db:
            db.execute('UPDATE searches SET finished=?,summary=?,failed=? WHERE id=?',
                       (datetime.now(timezone.utc).isoformat(), summary, json.dumps(failed), search_id))

    def history(self):
        with self.connect() as db:
            return [dict(r) for r in db.execute('SELECT * FROM searches ORDER BY id DESC LIMIT 100')]

    def save_preset(self, name, settings, interval=0):
        if not name.strip() or not 0 <= float(interval) <= 8760:
            raise ValueError('Enter a name and an interval between 0 and 8760 hours.')
        with self.connect() as db:
            db.execute('INSERT OR REPLACE INTO presets VALUES (?,?,?,?)',
                       (name.strip(), json.dumps(settings), float(interval), time.time() + float(interval) * 3600 if interval else 0))

    def presets(self):
        with self.connect() as db:
            return [dict(r, settings=json.loads(r['settings'])) for r in db.execute('SELECT * FROM presets ORDER BY name')]

    def delete_preset(self, name):
        with self.connect() as db:
            db.execute('DELETE FROM presets WHERE name=?', (name,))

    def defer_preset(self, name, hours):
        with self.connect() as db:
            db.execute('UPDATE presets SET next_run=? WHERE name=?', (time.time() + hours * 3600, name))


def build_tasks(settings):
    terms = expand_queries(settings['query'], settings.get('expand', True))
    return [{'site': site, 'city': city, 'term': term} for city in settings['cities']
            for site in settings['sites'] for term in terms]


class SearchRunner:
    """Serial, cancellable pacing; bounded retries; do not hammer a blocked board."""
    def __init__(self, scrape=None, pacing=2, cooldown=60):
        self.scrape = scrape
        self.pacing, self.cooldown = pacing, cooldown
        self.last = {}
        self.blocked_until = {}

    def run(self, settings, emit, stop, tasks=None):
        if self.scrape is None:
            from jobspy import scrape_jobs
            self.scrape = scrape_jobs
        tasks = build_tasks(settings) if tasks is None else tasks
        failed, answered, count = [], 0, 0
        for index, task in enumerate(tasks):
            if stop.is_set():
                failed.extend(t for t in tasks[index:] if t not in failed)
                break
            site, city, term = (task[k] for k in ('site', 'city', 'term'))
            if self.blocked_until.get(site, 0) > time.monotonic():
                failed.append(task)
                emit(('note', f'{site}: cooling down after a block; use Retry failed later.'))
                continue
            emit(('progress', f'{index+1}/{len(tasks)} · {site.title()} · {city} · {term}'))
            params = dict(site_name=[site], search_term=term, location=f'{city}, Morocco',
                          country_indeed='Morocco', results_wanted=settings['limit'],
                          description_format='plain', verbose=0,
                          fetch_description=settings.get('details', False))
            if settings.get('internships'):
                if site == 'linkedin':
                    params['search_term'] += ' (stage OR internship OR stagiaire OR PFE)'
                else:
                    params['job_type'] = 'internship'
            if settings.get('days'):
                params['hours_old'] = settings['days'] * 24
            for attempt in range(3):
                wait = max(0, self.pacing - (time.monotonic() - self.last.get(site, 0)))
                if stop.wait(wait):
                    break
                self.last[site] = time.monotonic()
                rows, error = [], ''
                try:
                    frame = self.scrape(**params)
                    rows = frame.to_dict('records')
                    for board, warnings in frame.attrs.get('warnings', {}).items():
                        for warning in warnings:
                            emit(('note', f'{city} / {board}: {warning}'))
                    error = '; '.join(str(x) for x in frame.attrs.get('errors', {}).values())
                except Exception as exc:
                    error = str(exc)
                if rows:
                    emit(('batch', rows))
                    count += len(rows)
                if not error:
                    answered += 1
                    break
                emit(('note', f'{site} / {city} / {term}: {error}'))
                logging.getLogger('studio').warning('Search task failed: %s / %s: %s', site, city, error)
                if re.search(r'\b(403|429)\b|captcha|blocked', error, re.I):
                    self.blocked_until[site] = time.monotonic() + self.cooldown
                    failed.append(task)
                    break
                transient = re.search(r'timeout|timed out|connection|\b(500|502|503|504)\b', error, re.I)
                if attempt == 2 or not transient:
                    failed.append(task)
                    break
                emit(('note', f'{site}: retry {attempt+1}/2 after a temporary failure.'))
                if stop.wait(2 ** (attempt + 1)):
                    break
            if stop.is_set():
                failed.extend(t for t in tasks[index:] if t not in failed)
                break
        emit(('complete', {'answered': answered, 'failed': failed, 'rows': count, 'cancelled': stop.is_set()}))


def public_url(url):
    parsed = urlsplit(url)
    if parsed.scheme not in ('https', 'http') or not parsed.hostname or parsed.username or parsed.password:
        raise ValueError('Use a public HTTP or HTTPS URL without credentials.')
    addresses = socket.getaddrinfo(parsed.hostname, parsed.port or (443 if parsed.scheme == 'https' else 80))
    if not addresses or any(not ipaddress.ip_address(x[4][0]).is_global for x in addresses):
        raise ValueError('Source imports require a public internet address.')
    return url


def fetch_page(url):
    """Fetch only explicit public URLs; validate redirects and cap size/time."""
    import requests
    with requests.Session() as session:
        session.trust_env = False
        for _ in range(5):
            public_url(url)
            with session.get(url, timeout=(8, 20), stream=True, allow_redirects=False,
                             headers={'User-Agent': 'JobSpyStudio/2.0 (personal job research)'}) as response:
                if response.is_redirect:
                    url = urljoin(url, response.headers['Location'])
                    continue
                if response.status_code != 200:
                    return response.status_code, '', url
                chunks, size = [], 0
                for chunk in response.iter_content(65536):
                    size += len(chunk)
                    if size > 3_000_000:
                        raise ValueError('Source exceeds the 3 MB import limit.')
                    chunks.append(chunk)
                return response.status_code, b''.join(chunks).decode(response.encoding or 'utf-8', errors='replace'), url
        raise ValueError('Too many source redirects.')


def fetch_source(url):
    status, text, final_url = fetch_page(url)
    if status != 200:
        raise ValueError(f'Source returned HTTP {status}.')
    return parse_source(text, final_url)


def parse_source(text, url):
    from bs4 import BeautifulSoup
    site = urlsplit(url).hostname or 'Imported source'
    rows = []
    if text.lstrip().startswith(('<rss', '<?xml', '<feed')):
        if re.search(r'<!DOCTYPE|<!ENTITY', text, re.I):
            raise ValueError('XML entities are not supported.')
        root = ET.fromstring(text)
        for item in root.iter():
            if item.tag.split('}')[-1] not in ('item', 'entry'):
                continue
            fields = {c.tag.split('}')[-1]: c for c in item}
            def value(name):
                node = fields.get(name)
                return ''.join(node.itertext()).strip() if node is not None else ''
            link = fields.get('link')
            href = (link.attrib.get('href') or value('link')) if link is not None else ''
            from email.utils import parsedate_to_datetime
            posted = value('published') or value('pubDate') or value('updated')
            try:
                posted = parsedate_to_datetime(posted).date().isoformat()
            except (ValueError, TypeError):
                posted = posted[:10] if re.match(r'\d{4}-\d{2}-\d{2}', posted) else ''
            rows.append(dict(title=value('title'), job_url=urljoin(url, href) if href else '', site=site,
                             description=BeautifulSoup(value('description') or value('summary') or value('content'), 'html.parser').get_text(' ', strip=True),
                             company=value('author'), location='', date_posted=posted))
    else:
        soup = BeautifulSoup(text, 'html.parser')
        def walk(value):
            if isinstance(value, list):
                for child in value:
                    yield from walk(child)
            elif isinstance(value, dict):
                types = value.get('@type', [])
                if types == 'JobPosting' or isinstance(types, list) and 'JobPosting' in types:
                    yield value
                for child in value.values():
                    if isinstance(child, (list, dict)):
                        yield from walk(child)
        for script in soup.find_all('script', type='application/ld+json'):
            try:
                entries = list(walk(json.loads(script.string or script.get_text())))
            except (ValueError, TypeError):
                continue
            for item in entries:
                company = item.get('hiringOrganization') or {}
                locations = item.get('jobLocation') or []
                if isinstance(locations, dict):
                    locations = [locations]
                cities = []
                for location in locations:
                    if isinstance(location, dict):
                        address = location.get('address', {})
                        cities.append(', '.join(clean(address.get(k)) for k in ('addressLocality', 'addressRegion', 'addressCountry') if address.get(k)) if isinstance(address, dict) else clean(address))
                salary = item.get('baseSalary') or {}
                salary = salary if isinstance(salary, dict) else {}
                amount = salary.get('value') or {}
                amount = amount if isinstance(amount, dict) else {'value': amount}
                rows.append(dict(title=clean(item.get('title')), company=clean(company.get('name')) if isinstance(company, dict) else clean(company),
                                 location='; '.join(cities), job_url=urljoin(url, clean(item.get('url')) or url), site=site,
                                 date_posted=clean(item.get('datePosted'))[:10], job_type=clean(item.get('employmentType')),
                                 valid_through=clean(item.get('validThrough')),
                                 work_mode='Remote' if item.get('jobLocationType') == 'TELECOMMUTE' else '',
                                 min_amount=clean(amount.get('minValue', amount.get('value'))),
                                 max_amount=clean(amount.get('maxValue', amount.get('value'))),
                                 currency=clean(salary.get('currency')), interval=clean(amount.get('unitText')).lower(),
                                 description=BeautifulSoup(clean(item.get('description')), 'html.parser').get_text(' ', strip=True)))
    rows = [r for r in rows if r.get('title')]
    if not rows:
        raise ValueError('No RSS/Atom entries or JobPosting structured data found. Use a direct posting URL or import CSV.')
    return rows[:500]

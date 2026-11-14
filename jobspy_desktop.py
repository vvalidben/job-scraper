"""A local desktop interface for the installed JobSpy library."""
from __future__ import annotations

import csv
import queue
import re
import threading
import tkinter as tk
import webbrowser
from datetime import datetime
from pathlib import Path
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlsplit

BASE = Path(__file__).resolve().parent
BG, WHITE, INK, MUTED = '#f3f5f7', '#ffffff', '#162333', '#627387'
NAVY, ACCENT, LINE = '#142638', '#12756e', '#dce3e9'


def clean(value):
    if value is None:
        return ''
    text = str(value)
    return '' if text.lower() in ('nan', 'none', '<na>', 'nat') else text


def internship_evidence(job):
    if re.search(r'\binternship\b|\bstage\b', clean(job.get('job_type')), re.I):
        return 'Job type: internship'
    if re.search(r'\binternship\b', clean(job.get('job_level')), re.I):
        return 'Experience level: internship'
    if re.search(r'\b(internship|intern|interns|stagiaire|stagiaires|stage|pfe|pfa)\b',
                 clean(job.get('title')), re.I):
        return 'Internship keyword in title'
    return ''


def merge_jobs(rows):
    unique = {}
    for row in rows:
        job = {key: clean(value) for key, value in row.items()}
        key = job.get('job_url') or '|'.join(job.get(k, '').lower() for k in ('title', 'company', 'location'))
        unique.setdefault(key, job)
    return sorted(unique.values(), key=lambda j: j.get('date_posted', ''), reverse=True)


def search_jobs(settings, emit, stop, scrape=None):
    """No Tk access here: all worker updates go through the event queue."""
    if scrape is None:
        from jobspy import scrape_jobs
        scrape = scrape_jobs
    rows, answered, failed = [], 0, 0
    total = len(settings['cities']) * len(settings['sites'])
    step = 0
    for city in settings['cities']:
        for site in settings['sites']:
            if stop.is_set():
                emit(('done', merge_jobs(rows), answered, failed, True))
                return
            step += 1
            emit(('progress', f'Searching {site.title()} in {city}  ·  {step}/{total}'))
            query = settings['query']
            params = dict(site_name=[site], search_term=query, location=f'{city}, Morocco',
                          country_indeed='Morocco', results_wanted=settings['limit'],
                          description_format='plain', verbose=0)
            if settings['internships']:
                if site == 'linkedin':
                    params['search_term'] = f'{query} (stage OR internship OR stagiaire OR PFE)'
                else:
                    params['job_type'] = 'internship'
            if settings['days']:
                params['hours_old'] = settings['days'] * 24
            try:
                frame = scrape(**params)
                errors = frame.attrs.get('errors', {})
                for board, error in errors.items():
                    emit(('note', f'{city} / {board}: {error}'))
                for board, warnings in frame.attrs.get('warnings', {}).items():
                    for warning in warnings:
                        emit(('note', f'{city} / {board}: {warning}'))
                if errors:
                    failed += 1
                else:
                    answered += 1
                rows.extend(frame.to_dict('records'))
                emit(('results', merge_jobs(rows)))
            except Exception as exc:
                failed += 1
                emit(('note', f'{city} / {site}: {exc}'))
    emit(('done', merge_jobs(rows), answered, failed, False))


class ScrollArea(tk.Frame):
    def __init__(self, parent, bg=BG):
        super().__init__(parent, bg=bg)
        self.canvas = tk.Canvas(self, bg=bg, bd=0, highlightthickness=0)
        bar = ttk.Scrollbar(self, orient='vertical', command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=bar.set)
        bar.pack(side='right', fill='y')
        self.canvas.pack(side='left', fill='both', expand=True)
        self.body = tk.Frame(self.canvas, bg=bg)
        self.item = self.canvas.create_window((0, 0), window=self.body, anchor='nw')
        self.body.bind('<Configure>', lambda e: self.canvas.configure(scrollregion=self.canvas.bbox('all')))
        self.canvas.bind('<Configure>', lambda e: self.canvas.itemconfigure(self.item, width=e.width))


class JobApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title('JobSpy Studio — Morocco')
        self.geometry('1380x880')
        self.minsize(1100, 740)
        self.configure(bg=BG)
        self.option_add('*Font', ('Segoe UI', 10))
        self.jobs, self.visible, self.selected = [], [], None
        self.events, self.stop = queue.Queue(), threading.Event()
        self.running, self.notes = False, []
        self.query = tk.StringVar(value='cybersecurity')
        self.cities = {c: tk.BooleanVar(value=True) for c in ('Rabat', 'Casablanca')}
        self.sites = {s: tk.BooleanVar(value=True) for s in ('linkedin', 'indeed')}
        self.internships = tk.BooleanVar(value=True)
        self.unverified = tk.BooleanVar(value=False)
        self.limit = tk.StringVar(value='20')
        self.age = tk.StringVar(value='Any time')
        self.find = tk.StringVar()
        self.status = tk.StringVar(value='Ready when you are. Choose your filters and start a search.')
        self.configure_styles()
        self.build()
        self.find.trace_add('write', lambda *_: self.render())
        self.bind_all('<MouseWheel>', self.wheel)
        self._poll_timer = self.after(100, self.poll)
        self._load_timer = self.after(180, self.load_previous)
        self.protocol('WM_DELETE_WINDOW', self.close)

    def configure_styles(self):
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('TCombobox', padding=7, fieldbackground=WHITE)
        style.configure('Horizontal.TProgressbar', background=ACCENT, troughcolor=LINE, borderwidth=0)

    def label(self, parent, text, size=10, bold=False, fg=INK, bg=None, **kw):
        return tk.Label(parent, text=text, font=('Segoe UI', size, 'bold' if bold else 'normal'),
                        fg=fg, bg=bg or parent.cget('bg'), anchor='w', **kw)

    def button(self, parent, text, command, primary=False):
        return tk.Button(parent, text=text, command=command, bg=ACCENT if primary else WHITE,
                         fg=WHITE if primary else INK, activebackground='#0e625c' if primary else '#e9eef2',
                         activeforeground=WHITE if primary else INK, relief='flat', bd=0,
                         padx=16, pady=10, cursor='hand2', font=('Segoe UI', 10, 'bold'))

    def check(self, parent, text, variable, dark=False, command=None):
        bg = NAVY if dark else parent.cget('bg')
        return tk.Checkbutton(parent, text=text, variable=variable, command=command, bg=bg,
                              fg='#e3edf4' if dark else INK, activebackground=bg,
                              activeforeground=WHITE if dark else INK,
                              selectcolor='#24415a' if dark else WHITE, anchor='w',
                              cursor='hand2', padx=0, pady=4, bd=0, highlightthickness=0)

    def build(self):
        sidebar = tk.Frame(self, bg=NAVY, width=280, padx=25, pady=26)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        self.label(sidebar, 'J /  JobSpy Studio', 19, True, WHITE).pack(fill='x')
        self.label(sidebar, 'YOUR NEXT OPPORTUNITY', 9, fg='#91b3c4').pack(fill='x', pady=(4, 28))
        self.label(sidebar, 'SEARCH FILTERS', 9, True, '#91b3c4').pack(fill='x', pady=(0, 14))
        self.label(sidebar, 'Role or keyword', fg=WHITE).pack(fill='x')
        entry = tk.Entry(sidebar, textvariable=self.query, relief='flat', bg=WHITE, fg=INK,
                         insertbackground=INK, font=('Segoe UI', 11), bd=8)
        entry.pack(fill='x', pady=(7, 4))
        self.label(sidebar, 'Try cybersécurité, SOC or sécurité', 9, fg='#aac0ce', wraplength=225).pack(fill='x')
        self.label(sidebar, 'Cities  /  Morocco', 10, True, WHITE).pack(fill='x', pady=(22, 6))
        for city, var in self.cities.items():
            self.check(sidebar, city, var, True).pack(fill='x')
        self.label(sidebar, 'Job boards', 10, True, WHITE).pack(fill='x', pady=(18, 6))
        for site, var in self.sites.items():
            self.check(sidebar, site.title(), var, True).pack(fill='x')
        self.label(sidebar, 'Opportunity', 10, True, WHITE).pack(fill='x', pady=(18, 6))
        self.check(sidebar, 'Internships / stages only', self.internships, True, self.render).pack(fill='x')
        self.label(sidebar, 'Posted', fg=WHITE).pack(fill='x', pady=(16, 6))
        ttk.Combobox(sidebar, textvariable=self.age, state='readonly',
                     values=('Any time', 'Past 24 hours', 'Past 7 days', 'Past 30 days')).pack(fill='x')
        self.label(sidebar, 'Results per board, per city', fg=WHITE).pack(fill='x', pady=(16, 6))
        ttk.Combobox(sidebar, textvariable=self.limit, state='readonly', values=('10', '20', '30', '50')).pack(fill='x')
        self.search_button = self.button(sidebar, 'Search opportunities  →', self.start, True)
        self.search_button.pack(fill='x', pady=(24, 6))
        self.cancel_button = self.button(sidebar, 'Stop after current request', self.cancel)
        self.label(sidebar, 'Powered by JobSpy\nSearch locally. Apply on the job board.', 9,
                   fg='#aac0ce', wraplength=225, justify='left').pack(side='bottom', fill='x', pady=(16, 0))

        main = tk.Frame(self, bg=BG, padx=28, pady=26)
        main.pack(side='left', fill='both', expand=True)
        top = tk.Frame(main, bg=BG)
        top.pack(fill='x')
        self.label(top, 'CAREER WORKSPACE   /   MOROCCO', 9, True, MUTED).pack(side='left')
        self.button(top, 'Export CSV', self.export).pack(side='right')
        self.button(top, 'Open CSV', self.import_csv).pack(side='right', padx=(0, 8))
        self.label(main, 'Find your next chapter.', 27, True).pack(fill='x', pady=(14, 4))
        self.label(main, 'Cybersecurity opportunities in Rabat & Casablanca.', 11, fg=MUTED).pack(fill='x')

        stats = tk.Frame(main, bg=BG)
        stats.pack(fill='x', pady=(24, 18))
        self.metrics = []
        for i, title in enumerate(('VISIBLE JOBS', 'INTERNSHIP LABELS', 'TOTAL LOADED')):
            tile = tk.Frame(stats, bg=WHITE, padx=18, pady=12, highlightbackground=LINE, highlightthickness=1)
            tile.pack(side='left', fill='x', expand=True, padx=(0, 10 if i < 2 else 0))
            num = self.label(tile, '0', 23, True, ACCENT)
            num.pack(fill='x')
            self.label(tile, title, 9, True, MUTED).pack(fill='x')
            self.metrics.append(num)
        tools = tk.Frame(main, bg=BG)
        tools.pack(fill='x', pady=(0, 14))
        self.label(tools, 'Opportunities', 14, True).pack(side='left')
        self.check(tools, 'Include unverified matches', self.unverified, command=self.render).pack(side='right')
        self.label(main, 'Search within results', 9, fg=MUTED).pack(fill='x')
        tk.Entry(main, textvariable=self.find, relief='flat', bg=WHITE, fg=INK, bd=8,
                 font=('Segoe UI', 11)).pack(fill='x', pady=(5, 14))

        split = tk.PanedWindow(main, orient='horizontal', bg=BG, bd=0, sashwidth=12, sashrelief='flat')
        split.pack(fill='both', expand=True)
        self.cards = ScrollArea(split)
        self.details = tk.Frame(split, bg=WHITE, padx=20, pady=20, highlightbackground=LINE, highlightthickness=1)
        split.add(self.cards, stretch='always', minsize=290, width=460)
        split.add(self.details, stretch='always', minsize=275, width=350)
        self.progress = ttk.Progressbar(main, mode='indeterminate')
        self.progress.pack(fill='x', pady=(15, 7))
        footer = tk.Frame(main, bg=BG)
        footer.pack(fill='x')
        self.status_label = tk.Label(footer, textvariable=self.status, bg=BG, fg=MUTED,
                                     anchor='w', justify='left', wraplength=690, font=('Segoe UI', 9))
        self.status_label.pack(side='left', fill='x', expand=True)
        tk.Button(footer, text='Search details', command=self.show_notes, relief='flat',
                  bg=BG, fg=ACCENT, cursor='hand2').pack(side='right')
        self.render()

    def wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if widget == self.cards:
                self.cards.canvas.yview_scroll(-int(event.delta / 120), 'units')
                return 'break'
            widget = getattr(widget, 'master', None)

    def render(self):
        word = self.find.get().casefold().strip()
        self.visible = [j for j in self.jobs if
                        (not self.internships.get() or self.unverified.get() or internship_evidence(j))
                        and (not word or word in ' '.join(j.get(k, '') for k in ('title', 'company', 'location')).casefold())]
        for metric, value in zip(self.metrics, (len(self.visible), sum(bool(internship_evidence(j)) for j in self.jobs), len(self.jobs))):
            metric.configure(text=str(value))
        for child in self.cards.body.winfo_children():
            child.destroy()
        if not self.visible:
            self.label(self.cards.body, 'A fresh start.', 20, True).pack(fill='x', padx=12, pady=(35, 12))
            text = ('No internship labels found in these results.\n\nTry searching “cybersécurité” or “SOC”. You can also include unverified matches to review all listings.'
                    if self.jobs else 'Choose your cities and keywords, then click Search opportunities.\n\nAlready have a results file? Use Open CSV to read it here.')
            empty = self.label(self.cards.body, text, 11, fg=MUTED, wraplength=330, justify='left')
            empty.pack(fill='x', padx=12)
            empty.bind('<Configure>', lambda e: empty.configure(wraplength=max(200, e.width - 12)))
        for job in self.visible:
            active = self.selected is job
            card = tk.Frame(self.cards.body, bg=WHITE, padx=16, pady=14,
                            highlightbackground=ACCENT if active else LINE, highlightthickness=2 if active else 1,
                            cursor='hand2')
            card.pack(fill='x', pady=(0, 10), padx=(0, 3))
            evidence = internship_evidence(job)
            self.label(card, f"{job.get('site', 'Job board').upper()}  ·  {'INTERNSHIP' if evidence else 'TYPE UNVERIFIED'}",
                       8, True, ACCENT if evidence else '#886128').pack(fill='x', pady=(0, 7))
            title = self.label(card, job.get('title') or 'Untitled role', 12, True, wraplength=390, justify='left')
            title.pack(fill='x')
            title.bind('<Configure>', lambda e, label=title: label.configure(wraplength=max(150, e.width)))
            company = self.label(card, job.get('company') or 'Company not provided', 10, fg=MUTED,
                                 wraplength=390, justify='left')
            company.pack(fill='x', pady=(5, 8))
            company.bind('<Configure>', lambda e, label=company: label.configure(wraplength=max(150, e.width)))
            meta = self.label(card, f"{job.get('location') or 'Location not provided'}\nPosted {job.get('date_posted') or 'date unavailable'}",
                              9, fg=MUTED, wraplength=390, justify='left')
            meta.pack(fill='x')
            meta.bind('<Configure>', lambda e, label=meta: label.configure(wraplength=max(150, e.width)))
            for target in [card, *card.winfo_children()]:
                target.bind('<Button-1>', lambda e, j=job: self.select(j))
        if self.selected not in self.visible:
            self.selected = self.visible[0] if self.visible else None
        self.show_detail()

    def select(self, job):
        self.selected = job
        self.render()

    def show_detail(self):
        for child in self.details.winfo_children():
            child.destroy()
        self.label(self.details, 'JOB DETAILS', 9, True, MUTED).pack(fill='x', pady=(0, 18))
        job = self.selected
        if not job:
            self.label(self.details, 'Your next move\nstarts here.', 22, True, justify='left').pack(fill='x', pady=(12, 15))
            info = self.label(self.details, 'Select a result to view the role, company and application link.\n\nInternship labels are based on the listed job type, experience level or title. Check the original posting before applying.',
                              11, fg=MUTED, wraplength=280, justify='left')
            info.pack(fill='x')
            info.bind('<Configure>', lambda e: info.configure(wraplength=max(180, e.width)))
            return
        title = self.label(self.details, job.get('title', ''), 18, True, wraplength=300, justify='left')
        title.pack(fill='x')
        title.bind('<Configure>', lambda e: title.configure(wraplength=max(160, e.width)))
        company = self.label(self.details, job.get('company', ''), 12, fg=ACCENT, wraplength=300, justify='left')
        company.pack(fill='x', pady=(10, 18))
        company.bind('<Configure>', lambda e: company.configure(wraplength=max(160, e.width)))
        for name, value in [('LOCATION', job.get('location')), ('POSTED', job.get('date_posted')),
                            ('MATCH', internship_evidence(job) or 'Internship type not verified')]:
            self.label(self.details, name, 8, True, MUTED).pack(fill='x', pady=(6, 3))
            value_label = self.label(self.details, value or 'Not provided', 10, wraplength=300, justify='left')
            value_label.pack(fill='x')
            value_label.bind('<Configure>', lambda e, label=value_label: label.configure(wraplength=max(160, e.width)))
        self.button(self.details, 'Open original posting  ↗', lambda: self.open_job(job), True).pack(fill='x', pady=(20, 12))
        self.label(self.details, 'ABOUT THE ROLE', 8, True, MUTED).pack(fill='x', pady=(6, 8))
        text = tk.Text(self.details, bg=WHITE, fg=INK, bd=0, highlightthickness=0, wrap='word',
                       font=('Segoe UI', 10), height=5, width=20, padx=0, pady=0)
        text.insert('1.0', job.get('description') or 'This search did not include a full description. Open the original posting to read the requirements and apply.')
        text.configure(state='disabled')
        text.pack(fill='both', expand=True)
        text.bind('<MouseWheel>', lambda e: (text.yview_scroll(-int(e.delta / 120), 'units'), 'break')[1])

    def open_job(self, job):
        url = job.get('job_url', '')
        parsed = urlsplit(url)
        if parsed.scheme not in ('http', 'https') or not parsed.hostname:
            messagebox.showwarning('No usable link', 'This result does not include a valid web link.')
            return
        webbrowser.open(url)

    def load_previous(self):
        files = sorted(BASE.glob('*.csv'), key=lambda p: p.stat().st_mtime, reverse=True)
        if files:
            self.read_csv(files[0])

    def import_csv(self):
        if self.running:
            messagebox.showinfo('Search in progress', 'Wait for this search to finish before opening another file.')
            return
        path = filedialog.askopenfilename(initialdir=BASE, filetypes=[('Job results', '*.csv')])
        if path:
            self.read_csv(Path(path))

    def read_csv(self, path):
        try:
            with path.open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or 'title' not in reader.fieldnames:
                    raise ValueError('Choose a JobSpy CSV with a title column.')
                rows = list(reader)
            self.jobs = merge_jobs(rows)
            self.selected = None
            self.render()
            self.status.set(f'Loaded {len(self.jobs)} listings from {path.name}. Original file preserved.')
        except Exception as exc:
            messagebox.showerror('Could not open CSV', str(exc))

    def export(self):
        if not self.visible:
            messagebox.showinfo('No results to export', 'Search first, or include unverified matches to export existing listings.')
            return
        path = filedialog.asksaveasfilename(initialdir=BASE, initialfile=f'jobs-{datetime.now():%Y%m%d-%H%M%S}.csv',
                                           defaultextension='.csv', filetypes=[('CSV spreadsheet', '*.csv')])
        if not path:
            return
        try:
            fields = list(dict.fromkeys(key for j in self.visible for key in j))
            with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(self.visible)
            self.status.set(f'Exported {len(self.visible)} visible jobs to {Path(path).name}.')
        except Exception as exc:
            messagebox.showerror('Export failed', str(exc))

    def start(self):
        if self.running:
            return
        query = self.query.get().strip()
        cities = [name for name, var in self.cities.items() if var.get()]
        sites = [name for name, var in self.sites.items() if var.get()]
        if not query or not cities or not sites:
            messagebox.showinfo('Choose your search', 'Enter a keyword and select at least one city and job board.')
            return
        settings = dict(query=query, cities=cities, sites=sites, internships=self.internships.get(),
                        limit=int(self.limit.get()), days={'Any time': 0, 'Past 24 hours': 1,
                        'Past 7 days': 7, 'Past 30 days': 30}[self.age.get()])
        self.running = True
        self.stop.clear()
        self.notes = ['LinkedIn ignores the job-type filter in this JobSpy version. We add internship keywords and check listing labels locally.']
        self.jobs, self.selected = [], None
        self.render()
        self.search_button.configure(state='disabled', text='Searching…')
        self.cancel_button.pack(fill='x', pady=(5, 0))
        self.progress.start(12)
        self.status.set('Connecting to the job boards…')
        def worker():
            try:
                search_jobs(settings, self.events.put, self.stop)
            except Exception as exc:
                self.events.put(('note', str(exc)))
                self.events.put(('done', [], 0, 1, False))
        threading.Thread(target=worker, daemon=True).start()

    def cancel(self):
        self.stop.set()
        self.status.set('Stopping after the current board request finishes…')

    def poll(self):
        try:
            while True:
                event = self.events.get_nowait()
                if event[0] == 'progress':
                    self.status.set(event[1])
                elif event[0] == 'note':
                    self.notes.append(event[1])
                elif event[0] == 'results':
                    self.jobs = event[1]
                    self.render()
                elif event[0] == 'done':
                    self.jobs = event[1]
                    self.running = False
                    self.progress.stop()
                    self.search_button.configure(state='normal', text='Search opportunities  →')
                    self.cancel_button.pack_forget()
                    self.render()
                    prefix = 'Stopped' if event[4] else ('Search failed' if not event[2] else 'Search complete')
                    suffix = f' {event[3]} board request(s) failed; see Search details.' if event[3] else ''
                    self.status.set(f'{prefix}. {len(self.visible)} visible / {len(self.jobs)} total listings.{suffix}')
        except queue.Empty:
            pass
        self._poll_timer = self.after(120, self.poll)

    def show_notes(self):
        win = tk.Toplevel(self)
        win.title('Search details')
        win.geometry('700x400')
        text = tk.Text(win, wrap='word', bg=WHITE, fg=INK, padx=18, pady=18)
        text.pack(fill='both', expand=True)
        text.insert('1.0', '\n\n'.join(self.notes) or 'No search warnings. Job boards may return fewer jobs than requested. Internship labels still need checking on the original posting.')
        text.configure(state='disabled')

    def close(self):
        self.stop.set()
        self.after_cancel(self._poll_timer)
        self.after_cancel(self._load_timer)
        self.destroy()


if __name__ == '__main__':
    try:
        from jobspy_workspace import WorkspaceApp
        WorkspaceApp().mainloop()
    except Exception:
        import traceback
        error = traceback.format_exc()
        try:
            (BASE / 'startup-error.txt').write_text(error, encoding='utf-8')
        except OSError:
            pass
        messagebox.showerror('JobSpy Studio could not start',
                             'Please share startup-error.txt from the job scraper folder for troubleshooting.')

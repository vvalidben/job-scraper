"""Persistent career workspace layered on the existing Studio design."""
from __future__ import annotations

import csv
import json
import queue
import threading
import time
import tkinter as tk
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import jobspy_desktop as core
from jobspy_studio import StudioApp, JobCard, Toggle, BG, SIDE, SURFACE, TEXT, MUTED, PURPLE, EDGE
from jobspy_services import (Store, SearchRunner, STATUSES, configure_logging, expand_queries,
                             internship_evidence, match_cv, read_profile, fetch_source, build_tasks)
from jobspy_features import (WorkspaceLock, matches_filters, validate_filters, work_mode,
                            experience_level, native_notify, inspect_listing)

core.internship_evidence = internship_evidence
BASE = Path(__file__).resolve().parent


class WorkspaceApp(StudioApp):
    def __init__(self, store_path=None):
        self.store = Store(store_path or BASE / 'data' / 'studio.sqlite3')
        self._work_lock = WorkspaceLock(self.store.path)
        self.log = configure_logging(BASE)
        self.runner = SearchRunner()
        self.profile = self.store.get('profile', '')
        self.failed_tasks = self.store.get('failed_tasks', [])
        self.active_search = None
        self.active_settings = None
        self.new_ids = set()
        self.page = 0
        self.page_size = 60
        self.source_running = False
        self._schedule_timer = None
        self._closing = False
        self._matching = {}
        super().__init__()
        self.title('JobSpy Studio — Career Workspace')
        self._schedule_timer = self.after(1500, self.schedule_tick)
        self.report_callback_exception = self.callback_error

    def configure_styles(self):
        super().configure_styles()
        saved = self.store.get('settings', {})
        self.extra_cities = tk.StringVar(value=', '.join(c for c in saved.get('cities', []) if c not in self.cities))
        self.expand = tk.BooleanVar(value=saved.get('expand', True))
        self.details_enabled = tk.BooleanVar(value=saved.get('details', False))
        self.notifications = tk.BooleanVar(value=self.store.get('notifications', True))
        self.alerts_text = tk.StringVar(value='Saved locally · background available')
        self.view = tk.StringVar(value='All jobs')
        self.status_filter = tk.StringVar(value='Any status')
        self.sort = tk.StringVar(value='Newest')
        self.preset_name = tk.StringVar()
        self.filters = dict(saved.get('filters', {}))
        self.sites['bayt'] = tk.BooleanVar(value=False)
        if saved:
            self.apply_settings(saved)

    def apply_settings(self, settings):
        self.query.set(settings.get('query', 'cybersecurity'))
        for name, var in self.cities.items():
            var.set(name in settings.get('cities', ['Rabat', 'Casablanca']))
        for name, var in self.sites.items():
            var.set(name in settings.get('sites', ['linkedin', 'indeed']))
        self.extra_cities.set(', '.join(c for c in settings.get('cities', []) if c not in self.cities))
        self.internships.set(settings.get('internships', True))
        self.expand.set(settings.get('expand', True))
        self.details_enabled.set(settings.get('details', False))
        self.filters = dict(settings.get('filters', {}))
        self.limit.set(str(settings.get('limit', 20)))
        self.age.set({0: 'Any time', 1: 'Past 24 hours', 7: 'Past 7 days', 30: 'Past 30 days'}.get(settings.get('days', 0), 'Any time'))

    def settings(self):
        cities = [c for c, var in self.cities.items() if var.get()]
        cities += [c.strip() for c in self.extra_cities.get().split(',') if c.strip()]
        cities = list(dict.fromkeys(cities))
        sites = [s for s, var in self.sites.items() if var.get()]
        query = self.query.get().strip()
        if not query or not cities or not sites:
            raise ValueError('Enter a keyword and select at least one city and board.')
        if len(cities) > 10:
            raise ValueError('Choose up to 10 cities per search.')
        expand_queries(query, self.expand.get())
        return dict(query=query, cities=cities, sites=sites, internships=self.internships.get(),
                    limit=int(self.limit.get()), days={'Any time': 0, 'Past 24 hours': 1, 'Past 7 days': 7, 'Past 30 days': 30}[self.age.get()],
                    expand=self.expand.get(), details=self.details_enabled.get(), filters=validate_filters(self.filters))

    def build(self):
        super().build()
        form = self.filter_scroll.body
        self.section(form, '05', 'Search intelligence')
        self.label(form, 'Separate terms with ;  (up to 8)', 8, fg=MUTED).pack(fill='x')
        Toggle(form, self, 'Expand related keywords', self.expand).pack(fill='x')
        Toggle(form, self, 'Fetch full descriptions', self.details_enabled).pack(fill='x')
        self.label(form, 'OTHER CITIES · COMMA SEPARATED', 8, True, MUTED).pack(fill='x', pady=(12, 6))
        self.entry(form, self.extra_cities).pack(fill='x')
        self.button(form, 'Advanced filters', self.filters_dialog).pack(fill='x', pady=(8, 3))
        self.section(form, '06', 'Saved searches')
        self.preset_combo = ttk.Combobox(form, textvariable=self.preset_name, state='readonly', style='Studio.TCombobox')
        self.preset_combo.pack(fill='x', pady=(0, 6))
        self.button(form, 'Load saved search', self.load_preset).pack(fill='x', pady=3)
        self.button(form, 'Save / schedule search', self.save_preset).pack(fill='x', pady=3)
        self.button(form, 'Manage schedules', self.manage_presets).pack(fill='x', pady=3)
        self.button(form, 'Retry failed sources', self.retry_failed).pack(fill='x', pady=(12, 3))
        self.section(form, '07', 'Your workspace')
        self.button(form, 'CV & skills profile', self.profile_dialog).pack(fill='x', pady=3)
        self.button(form, 'Search history', self.history_dialog).pack(fill='x', pady=3)
        self.button(form, 'Sources & imports', self.sources_dialog).pack(fill='x', pady=3)
        self.button(form, 'Background & notifications', self.background_dialog).pack(fill='x', pady=3)
        self.button(form, 'Backup & restore', self.backup_dialog).pack(fill='x', pady=3)
        self.button(form, 'Check visible postings (25)', self.check_visible).pack(fill='x', pady=3)
        Toggle(form, self, 'Job & reminder notifications', self.notifications, self.save_notifications).pack(fill='x')
        self.label(form, 'Enable background mode for searches\nwhen this window is closed.\nCV text stays on this computer.', 8, fg=MUTED,
                   wraplength=210, justify='left').pack(fill='x', pady=(8, 16))
        self.refresh_presets()
        main = self.hero.master
        toolbar = tk.Frame(main, bg=BG)
        toolbar.pack(fill='x', pady=(0, 8), before=self.cards.master)
        for variable, values, width in ((self.view, ('All jobs', 'New / unread', 'Favorites', 'Tracked applications', 'Upcoming deadlines', 'Follow-ups due', 'Upcoming interviews', 'Closed postings'), 19),
                (self.status_filter, ('Any status',) + STATUSES, 15),
                (self.sort, ('Newest', 'Best CV match', 'Company', 'Deadline'), 16)):
            box = ttk.Combobox(toolbar, textvariable=variable, values=values, width=width,
                               state='readonly', style='Studio.TCombobox')
            box.pack(side='left', padx=(0, 7))
            box.bind('<<ComboboxSelected>>', lambda e: self.reset_view())
        self.button(toolbar, 'Mark view read', self.mark_read).pack(side='right')
        nav = tk.Frame(main, bg=BG)
        nav.pack(fill='x', pady=(0, 4), before=self.cards.master)
        self.page_text = tk.StringVar()
        self.label(nav, '', 8, fg=MUTED, textvariable=self.page_text).pack(side='left')
        for text, delta in (('Next →', 1), ('← Previous', -1)):
            tk.Button(nav, text=text, command=lambda d=delta: self.change_page(d), bg=SURFACE,
                      fg=TEXT, relief='flat', padx=10).pack(side='right', padx=3)
        self.label(nav, '', 8, fg=PURPLE, textvariable=self.alerts_text).pack(side='right', padx=12)
        self.render()

    def reset_view(self):
        self.page = 0
        self.render()

    def wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            if isinstance(widget, core.ScrollArea):
                widget.canvas.yview_scroll(-int(event.delta / 120), 'units')
                return 'break'
            widget = getattr(widget, 'master', None)

    def change_page(self, delta):
        self.page = max(0, min(self.page + delta, max(0, (len(self.visible)-1)//self.page_size)))
        self.render()
        self.cards.canvas.yview_moveto(0)

    def score(self, job):
        key = (job.get('_key'), job.get('description'), job.get('skills'), job.get('title'))
        if key not in self._matching:
            self._matching[key] = match_cv(job, self.profile)
        return self._matching[key]

    def render(self):
        if not hasattr(self, 'cards'):
            return
        selected_key = self.selected.get('_key') if self.selected else None
        word = self.find.get().casefold().strip()
        today = date.today().isoformat()
        soon = (date.today() + timedelta(days=7)).isoformat()
        def accepts(j):
            if not matches_filters(j, self.filters):
                return False
            if self.internships.get() and not self.unverified.get() and not internship_evidence(j):
                return False
            if word and word not in ' '.join(str(j.get(k, '')) for k in ('title', 'company', 'location', 'description', '_notes')).casefold():
                return False
            view = self.view.get()
            if view == 'New / unread' and j.get('_seen'):
                return False
            if view == 'Favorites' and not j.get('_favorite'):
                return False
            if view == 'Tracked applications' and j.get('_status', 'Interested') in ('Interested', 'Archived'):
                return False
            if view == 'Upcoming deadlines' and not today <= j.get('_deadline', '') <= soon:
                return False
            if view == 'Follow-ups due' and (not j.get('_follow_up') or j['_follow_up'] > today or j['_status'] in ('Rejected', 'Archived')):
                return False
            if view == 'Upcoming interviews' and not today <= j.get('_interview_at', '')[:10] <= soon:
                return False
            if view == 'Closed postings' and j.get('_availability') != 'Closed':
                return False
            return self.status_filter.get() == 'Any status' or j.get('_status', 'Interested') == self.status_filter.get()
        self.visible = [j for j in self.jobs if accepts(j)]
        sort = self.sort.get()
        if sort == 'Best CV match':
            self.visible.sort(key=lambda j: self.score(j)['score'] if self.score(j)['score'] is not None else -1, reverse=True)
        elif sort == 'Company':
            self.visible.sort(key=lambda j: j.get('company', '').casefold())
        elif sort == 'Deadline':
            self.visible.sort(key=lambda j: j.get('_deadline') or '9999')
        else:
            self.visible.sort(key=lambda j: (j.get('date_posted', ''), j.get('_first_seen', '')), reverse=True)
        self.page = min(self.page, max(0, (len(self.visible)-1)//self.page_size))
        batch = self.visible[self.page*self.page_size:(self.page+1)*self.page_size]
        self.selected = next((j for j in batch if j.get('_key') == selected_key), batch[0] if batch else None)
        for metric, value in zip(self.metrics, (len(self.visible), sum(bool(internship_evidence(j)) for j in self.jobs), len(self.jobs))):
            metric.configure(text=str(value))
        for child in self.cards.body.winfo_children():
            child.destroy()
        self.card_widgets = []
        if not batch:
            self.empty_state()
        for index, job in enumerate(batch):
            card = JobCard(self.cards.body, self, job, index)
            card.pack(fill='x', padx=(0, 2), pady=(0, 11))
            self.card_widgets.append(card)
        if hasattr(self, 'page_text'):
            self.page_text.set(f'{len(self.visible)} matches · page {self.page+1}/{max(1,(len(self.visible)+59)//60)}')
        self.show_detail()

    def select(self, job):
        if job.get('_key'):
            self.store.annotate(job['_key'], seen=1)
            job['_seen'] = 1
        super().select(job)

    def show_detail(self):
        super().show_detail()
        if not self.selected:
            return
        job = self.selected
        children = self.details.winfo_children()
        panel = tk.Frame(self.details, bg=SURFACE)
        panel.pack(fill='x', pady=(10, 5), before=children[0])
        flags = ('NEW · ' if not job.get('_seen') else '') + ('★ · ' if job.get('_favorite') else '') + job.get('_status', 'Interested')
        self.label(panel, flags, 9, True, PURPLE).pack(fill='x')
        actions = tk.Frame(panel, bg=SURFACE)
        actions.pack(fill='x', pady=5)
        for text, callback in (('☆ Favorite' if not job.get('_favorite') else '★ Saved', self.favorite), ('Track / notes', self.tracker_dialog), ('Check posting', self.check_selected)):
            tk.Button(actions, text=text, command=callback, bg=BG, fg=TEXT, relief='flat', padx=8, pady=6).pack(side='left', padx=(0, 5))
        if job.get('_deadline'):
            origin = 'from source' if job.get('_deadline_origin') == 'source' else 'entered manually'
            self.wrapped(panel, f"Deadline: {job['_deadline']} ({origin})", 9, fg=PURPLE).pack(fill='x')
        self.wrapped(panel, f"{work_mode(job)} work mode · {experience_level(job)} experience", 9, fg=MUTED).pack(fill='x')
        if job.get('min_amount') or job.get('max_amount'):
            self.wrapped(panel, f"Salary: {job.get('min_amount', '')} – {job.get('max_amount', '')} {job.get('currency', '')} / {job.get('interval', 'unspecified period')}", 9, fg=MUTED).pack(fill='x')
        self.wrapped(panel, f"Posting: {job.get('_availability', 'Unknown')} · last checked {job.get('_checked_at') or 'never'}", 9, fg=MUTED).pack(fill='x')
        if job.get('_availability_note'):
            self.wrapped(panel, job['_availability_note'], 8, fg=MUTED).pack(fill='x')
        for label, field in (('Applied', 'applied_on'), ('Recruiter', 'recruiter'), ('Contact', 'contact'), ('Interview', 'interview_at'), ('Follow-up', 'follow_up')):
            if job.get('_' + field):
                self.wrapped(panel, f'{label}: {job["_" + field]}', 9, fg=MUTED).pack(fill='x')
        if job.get('sources'):
            self.wrapped(panel, f"Sources: {job['sources']}", 8, fg=MUTED).pack(fill='x')
        if self.profile:
            result = self.score(job)
            score = f"{result['score']}% skill overlap" if result['score'] is not None else 'No recognized skills to compare'
            self.wrapped(panel, score, 10, True, PURPLE).pack(fill='x', pady=(6, 0))
            for label, values in (('Matched', result['matched']), ('Not found in CV', result['missing'])):
                if values:
                    self.wrapped(panel, f"{label}: {', '.join(values)}", 9, fg=MUTED).pack(fill='x', pady=3)
            self.wrapped(panel, 'Keyword evidence only; not a hiring prediction.', 8, fg=MUTED).pack(fill='x')

    def reload_jobs(self):
        self.jobs = self.store.jobs()
        self._matching.clear()
        self.render()

    def load_previous(self):
        if not self.store.get('csv_migrated', False):
            for path in BASE.glob('*.csv'):
                try:
                    with path.open(encoding='utf-8-sig', newline='') as stream:
                        reader = csv.DictReader(stream)
                        if reader.fieldnames and 'title' in reader.fieldnames:
                            self.store.upsert(list(reader))
                except Exception:
                    self.log.exception('Could not migrate CSV: %s', path.name)
            self.store.set('csv_migrated', True)
        self.store.daily_backup()
        self.reload_jobs()
        self.status.set(f'{len(self.jobs)} saved listings loaded. Searches keep your existing jobs.')

    def read_csv(self, path):
        try:
            with Path(path).open(encoding='utf-8-sig', newline='') as stream:
                reader = csv.DictReader(stream)
                if not reader.fieldnames or 'title' not in reader.fieldnames:
                    raise ValueError('Choose a CSV with a title column.')
                rows = list(reader)
            added = self.store.upsert(rows)
            self.reload_jobs()
            self.status.set(f'Imported {len(rows)} rows; {len(added)} new listings. Original file preserved.')
        except Exception as exc:
            messagebox.showerror('Could not import CSV', str(exc), parent=self)

    def start(self, settings=None, tasks=None, preset_name=None):
        if self.running or self.source_running:
            self.status.set('Wait for the current search or source import to finish.')
            return
        if not self._work_lock.acquire():
            self.status.set('The background worker is busy. Try again after it finishes.')
            return
        try:
            settings = settings or self.settings()
            if preset_name and not self.store.claim_preset(preset_name):
                self._work_lock.release()
                return
            self.store.set('settings', settings)
            self.active_search = self.store.begin_search(settings)
        except Exception as exc:
            self._work_lock.release()
            messagebox.showerror('Check your search', str(exc), parent=self)
            return
        self.active_settings = settings
        self.running = True
        self.stop.clear()
        self.new_ids = set()
        self.notes = ['Full descriptions add requests. Failed boards can be retried; 403/429 responses trigger a cooldown.']
        self.search_button.configure(state='disabled', text='Searching…')
        self.cancel_button.pack(fill='x', pady=(5, 0))
        self.progress.start()
        self.status.set('Searching; your saved jobs remain available.')
        def worker():
            try:
                self.runner.run(settings, self.events.put, self.stop, tasks)
            except Exception as exc:
                self.log.exception('Search worker failed')
                self.events.put(('note', str(exc)))
                self.events.put(('complete', dict(answered=0, failed=tasks if tasks is not None else build_tasks(settings), rows=0, cancelled=self.stop.is_set(), fatal=str(exc))))
        threading.Thread(target=worker, daemon=True).start()

    def retry_failed(self):
        if self.running:
            return
        if not self.failed_tasks:
            messagebox.showinfo('Retry failed', 'There are no failed requests from the last search.', parent=self)
            return
        settings = self.active_settings or self.store.get('last_search_settings')
        if settings:
            self.start(settings, list(self.failed_tasks))

    def poll(self):
        changed = False
        for _ in range(100):
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event[0]
            try:
                if kind == 'progress':
                    self.status.set(event[1])
                elif kind == 'note':
                    self.notes.append(event[1])
                elif kind == 'batch':
                    self.new_ids.update(self.store.upsert(event[1]))
                    changed = True
                elif kind == 'complete':
                    result = event[1]
                    self.failed_tasks = result['failed']
                    self.store.set('failed_tasks', self.failed_tasks)
                    self.store.set('last_search_settings', self.active_settings)
                    prefix = 'Stopped' if result['cancelled'] else ('Search failed' if result.get('fatal') or not result['answered'] else 'Search complete')
                    summary = f"{prefix}: {len(self.new_ids)} new jobs; {result['answered']} requests completed; {len(self.failed_tasks)} failed."
                    self.store.finish_search(self.active_search, summary, self.failed_tasks)
                    self.running = False
                    self._work_lock.release()
                    self.progress.stop()
                    self.search_button.configure(state='normal', text='Search opportunities →')
                    self.cancel_button.pack_forget()
                    self.status.set(summary)
                    self.log.info(summary)
                    changed = True
                    if self.new_ids:
                        new_jobs = [j for j in self.store.jobs() if j['_key'] in self.new_ids]
                        matches = [j for j in new_jobs if (not self.active_settings.get('internships') or internship_evidence(j))
                                   and matches_filters(j, self.active_settings.get('filters', {}))]
                        if matches:
                            self.notify('New opportunities', f'{len(matches)} new matching jobs saved. Select New / unread to review them.')
                elif kind == 'imported':
                    new = self.store.upsert(event[1])
                    self.source_running = False
                    self._work_lock.release()
                    self.status.set(f'Imported {len(event[1])} entries, {len(new)} new listings. Review source details before applying.')
                    changed = True
                elif kind == 'import_error':
                    self.source_running = False
                    self._work_lock.release()
                    self.status.set('Source import failed. Your saved jobs are preserved.')
                    messagebox.showerror('Source import failed', event[1], parent=self)
                elif kind == 'checked':
                    self.source_running = False
                    self._work_lock.release()
                    self.status.set(f'Checked {event[1]} posting(s). Review availability and last-checked details.')
                    changed = True
                elif kind == 'check_error':
                    self.source_running = False
                    self._work_lock.release()
                    self.status.set('Posting check failed. See data/studio.log.')
            except Exception:
                self.log.exception('Could not process %s event', kind)
                self.stop.set()
                if kind in ('complete', 'imported', 'import_error', 'checked', 'check_error'):
                    self._work_lock.release()
                    self.running = False
                    self.source_running = False
                    self.progress.stop()
                    self.search_button.configure(state='normal', text='Search opportunities →')
                    self.cancel_button.pack_forget()
                self.status.set('An operation failed. See data/studio.log for details; your saved jobs are preserved.')
        if changed:
            self.reload_jobs()
        self._poll_timer = self.after(120, self.poll)

    def favorite(self):
        if self.selected:
            self.store.annotate(self.selected['_key'], favorite=0 if self.selected.get('_favorite') else 1)
            self.reload_jobs()

    def mark_read(self):
        for job in self.visible:
            self.store.annotate(job['_key'], seen=1)
        self.reload_jobs()

    def window(self, title, geometry='660x620'):
        win = tk.Toplevel(self)
        win.title(title)
        win.geometry(geometry)
        win.configure(bg=SURFACE)
        win.transient(self)
        return win

    def tracker_dialog(self):
        if not self.selected:
            return
        job = self.selected
        win = self.window('Application tracker', '650x760')
        scroll = self.scroll(win, SURFACE)
        scroll.pack(fill='both', expand=True, padx=22, pady=(20, 5))
        frame = scroll.body
        self.wrapped(frame, job['title'], 16, True).pack(fill='x', pady=(0, 12))
        status = tk.StringVar(value=job.get('_status', 'Interested'))
        favorite = tk.BooleanVar(value=bool(job.get('_favorite')))
        deadline = tk.StringVar(value=job.get('_deadline', ''))
        self.label(frame, 'Application status', fg=MUTED).pack(fill='x')
        ttk.Combobox(frame, textvariable=status, values=STATUSES, state='readonly', style='Studio.TCombobox').pack(fill='x', pady=5)
        Toggle(frame, self, 'Favorite', favorite).pack(fill='x')
        self.label(frame, 'Deadline (YYYY-MM-DD, optional)', fg=MUTED).pack(fill='x', pady=(10, 4))
        self.entry(frame, deadline).pack(fill='x')
        tracking = {}
        for field, label in (('applied_on', 'Applied on (YYYY-MM-DD)'), ('recruiter', 'Recruiter name'),
                             ('contact', 'Recruiter email / phone / profile URL'),
                             ('interview_at', 'Interview (YYYY-MM-DD HH:MM, local time)'),
                             ('follow_up', 'Follow-up date (YYYY-MM-DD)')):
            tracking[field] = tk.StringVar(value=job.get('_' + field, ''))
            self.label(frame, label, fg=MUTED).pack(fill='x', pady=(8, 4))
            self.entry(frame, tracking[field]).pack(fill='x')
        self.label(frame, 'Notes', fg=MUTED).pack(fill='x', pady=(12, 4))
        notes = tk.Text(frame, bg=BG, fg=TEXT, insertbackground=TEXT, wrap='word', height=8, relief='flat')
        notes.insert('1.0', job.get('_notes', ''))
        notes.pack(fill='both', expand=True)
        def save():
            try:
                value = deadline.get().strip()
                if value:
                    if len(value) != 10:
                        raise ValueError('Use YYYY-MM-DD for the deadline.')
                    date.fromisoformat(value)
                fields = {key: var.get().strip() for key, var in tracking.items()}
                if status.get() == 'Applied' and not fields['applied_on']:
                    fields['applied_on'] = date.today().isoformat()
                fields['follow_reminder'] = '' if fields['follow_up'] != job.get('_follow_up') else job.get('_follow_reminder', '')
                fields['interview_reminder'] = '' if fields['interview_at'] != job.get('_interview_at') else job.get('_interview_reminder', '')
                fields['deadline_origin'] = 'manual' if value != job.get('_deadline') else job.get('_deadline_origin', 'manual')
                self.store.annotate(job['_key'], favorite=int(favorite.get()), status=status.get(), deadline=value,
                                    notes=notes.get('1.0', 'end-1c'), reminder='' if value != job.get('_deadline') else job.get('_reminder', ''), **fields)
                self.reload_jobs()
                win.destroy()
            except ValueError as exc:
                messagebox.showerror('Check application details', str(exc), parent=win)
        self.button(win, 'Save application', save, True).pack(fill='x', padx=22, pady=(5, 18))

    def profile_dialog(self):
        win = self.window('CV & skills profile', '740x650')
        frame = tk.Frame(win, bg=SURFACE, padx=20, pady=20)
        frame.pack(fill='both', expand=True)
        self.wrapped(frame, 'Import or paste your CV / skills', 18, True).pack(fill='x')
        self.wrapped(frame, 'Saved only in the local database. Matching compares recognized skill keywords mentioned in the listing; it does not assess your proficiency or eligibility.', 10, fg=MUTED).pack(fill='x', pady=10)
        text = tk.Text(frame, bg=BG, fg=TEXT, insertbackground=TEXT, wrap='word', relief='flat')
        text.insert('1.0', self.profile)
        text.pack(fill='both', expand=True)
        def import_cv():
            path = filedialog.askopenfilename(parent=win, filetypes=[('CV documents', '*.txt *.md *.pdf *.docx')])
            if path:
                try:
                    value = read_profile(path)
                    text.delete('1.0', 'end')
                    text.insert('1.0', value)
                except Exception as exc:
                    messagebox.showerror('Could not read CV', str(exc), parent=win)
        def save():
            self.profile = text.get('1.0', 'end-1c')[:100_000]
            self.store.set('profile', self.profile)
            self._matching.clear()
            self.render()
            win.destroy()
        actions = tk.Frame(frame, bg=SURFACE)
        actions.pack(fill='x', pady=(12, 0))
        self.button(actions, 'Import CV', import_cv).pack(side='left')
        self.button(actions, 'Clear text', lambda: text.delete('1.0', 'end')).pack(side='left', padx=8)
        self.button(actions, 'Save profile', save, True).pack(side='right')

    def refresh_presets(self):
        self.preset_combo.configure(values=[p['name'] for p in self.store.presets()])

    def save_preset(self):
        try:
            settings = self.settings()
        except ValueError as exc:
            messagebox.showerror('Check search', str(exc), parent=self)
            return
        name = simpledialog.askstring('Save search', 'Search name:', initialvalue=self.preset_name.get() or self.query.get(), parent=self)
        if not name or not name.strip():
            return
        hours = simpledialog.askfloat('Schedule search', 'Repeat every how many hours?\n0 = manual only. Enable background mode to run when closed.', initialvalue=0, minvalue=0, maxvalue=8760, parent=self)
        if hours is None:
            return
        if 0 < hours < 1:
            messagebox.showerror('Schedule interval', 'Use at least 1 hour between scheduled searches.', parent=self)
            return
        existing = next((p for p in self.store.presets() if p['name'] == name.strip()), None)
        if existing and not messagebox.askyesno('Replace saved search', f'Replace “{name.strip()}” and its schedule?', parent=self):
            return
        self.store.save_preset(name, settings, hours)
        self.preset_name.set(name.strip())
        self.refresh_presets()
        self.status.set(f'Saved search “{name.strip()}”.' + (f' Repeats every {hours:g} hours; background mode also runs when closed.' if hours else ''))

    def load_preset(self):
        preset = next((p for p in self.store.presets() if p['name'] == self.preset_name.get()), None)
        if preset:
            self.apply_settings(preset['settings'])
            self.status.set(f'Loaded “{preset["name"]}”. Click Search opportunities to run it.')

    def manage_presets(self):
        win = self.window('Saved searches & schedules', '760x420')
        frame = tk.Frame(win, bg=SURFACE, padx=20, pady=20)
        frame.pack(fill='both', expand=True)
        self.wrapped(frame, 'Schedules run while open, or with background mode enabled. Missed runs execute once when next available.', 10, fg=MUTED).pack(fill='x', pady=(0, 12))
        presets = self.store.presets()
        listing = tk.Listbox(frame, bg=BG, fg=TEXT, selectbackground='#534377', relief='flat')
        listing.pack(fill='both', expand=True)
        for p in presets:
            when = datetime.fromtimestamp(p['next_run']).strftime('%Y-%m-%d %H:%M') if p['interval_hours'] else 'manual only'
            listing.insert('end', f'{p["name"]} · every {p["interval_hours"]:g}h · {when}')
        def action(delete=False):
            if not listing.curselection():
                return
            p = presets[listing.curselection()[0]]
            if delete:
                self.store.delete_preset(p['name'])
            else:
                self.store.save_preset(p['name'], p['settings'], 0)
            self.refresh_presets()
            win.destroy()
            self.manage_presets()
        actions = tk.Frame(frame, bg=SURFACE)
        actions.pack(fill='x', pady=10)
        self.button(actions, 'Disable schedule', action).pack(side='left')
        self.button(actions, 'Delete saved search', lambda: action(True)).pack(side='right')

    def history_dialog(self):
        win = self.window('Search history', '900x520')
        frame = tk.Frame(win, bg=SURFACE, padx=20, pady=20)
        frame.pack(fill='both', expand=True)
        rows = self.store.history()
        listing = tk.Listbox(frame, bg=BG, fg=TEXT, selectbackground='#534377', relief='flat')
        listing.pack(fill='both', expand=True)
        for row in rows:
            started = datetime.fromisoformat(row['started']).astimezone().strftime('%Y-%m-%d %H:%M')
            settings = json.loads(row['settings'])
            listing.insert('end', f'{started} · {settings["query"]} · {row["summary"] or "Interrupted / unfinished"}')
        self.wrapped(frame, 'Select a search to load its settings. No requests are sent until you click Search.', 9, fg=MUTED).pack(fill='x', pady=8)
        def load():
            if listing.curselection():
                row = rows[listing.curselection()[0]]
                self.apply_settings(json.loads(row['settings']))
                win.destroy()
        self.button(frame, 'Load selected settings', load, True).pack(fill='x')

    def sources_dialog(self):
        win = self.window('Sources & imports', '760x600')
        frame = tk.Frame(win, bg=SURFACE, padx=22, pady=20)
        frame.pack(fill='both', expand=True)
        self.wrapped(frame, 'More ways to discover opportunities', 18, True).pack(fill='x')
        self.wrapped(frame, 'LinkedIn, Indeed and optional Bayt use JobSpy. Coverage and available fields vary by board. The links below open Morocco-focused boards for manual browsing; they are not automatic search connectors.', 10, fg=MUTED).pack(fill='x', pady=12)
        for name, url in (('Emploi.ma', 'https://www.emploi.ma/recherche-jobs-maroc'), ('ReKrute', 'https://www.rekrute.com/'), ('ANAPEC', 'https://www.anapec.org/')):
            self.button(frame, f'Browse {name} ↗', lambda u=url: webbrowser.open(u)).pack(fill='x', pady=3)
        self.wrapped(frame, 'Import a public RSS / Atom feed or a direct job page with JobPosting structured data. Imports add listings to your library; feeds are imported on demand. Pages without structured data can be imported using CSV.', 10, fg=MUTED).pack(fill='x', pady=(20, 10))
        source = tk.StringVar(value=self.store.get('source_url', ''))
        self.entry(frame, source).pack(fill='x')
        def start_import():
            if self.running or self.source_running:
                messagebox.showinfo('Please wait', 'Wait for the current search or import to finish.', parent=win)
                return
            url = source.get().strip()
            if not url:
                return
            if not self._work_lock.acquire():
                messagebox.showinfo('Background worker busy', 'Try again when the background operation finishes.', parent=win)
                return
            self.store.set('source_url', url)
            self.source_running = True
            self.status.set('Importing structured source…')
            def worker():
                try:
                    self.events.put(('imported', fetch_source(url)))
                except Exception as exc:
                    self.log.warning('Structured source import failed: %s', exc)
                    self.events.put(('import_error', str(exc)))
            threading.Thread(target=worker, daemon=True).start()
            win.destroy()
        self.button(frame, 'Import source URL', start_import, True).pack(fill='x', pady=10)
        self.button(frame, 'Import job CSV', self.import_csv).pack(fill='x')

    def filters_dialog(self):
        win = self.window('Advanced filters', '680x710')
        scroll = self.scroll(win, SURFACE)
        scroll.pack(fill='both', expand=True, padx=22, pady=18)
        frame = scroll.body
        self.wrapped(frame, 'Narrow your shortlist', 18, True).pack(fill='x', pady=(0, 10))
        variables = {}
        for key, label, choices in (
                ('work_mode', 'Work arrangement', ('Any', 'Remote', 'Hybrid', 'On-site', 'Unknown')),
                ('experience', 'Experience level', ('Any', 'Internship', 'Entry', 'Mid', 'Senior', 'Unknown'))):
            variables[key] = tk.StringVar(value=self.filters.get(key, 'Any'))
            self.label(frame, label, fg=MUTED).pack(fill='x', pady=(8, 4))
            ttk.Combobox(frame, textvariable=variables[key], values=choices, state='readonly', style='Studio.TCombobox').pack(fill='x')
        for key, label, default in (
                ('salary_min', 'Minimum annual salary', ''), ('salary_max', 'Maximum annual salary', ''),
                ('currency', 'Salary currency (MAD, EUR, USD...)', 'MAD'),
                ('exclude_keywords', 'Exclude keywords / phrases (separate with ;)', ''),
                ('exclude_companies', 'Exclude company names (separate with ;)', '')):
            variables[key] = tk.StringVar(value=self.filters.get(key, default))
            self.label(frame, label, fg=MUTED).pack(fill='x', pady=(8, 4))
            self.entry(frame, variables[key]).pack(fill='x')
        for key, label, default in (('include_unknown_salary', 'Include salaries that cannot be compared', True),
                                    ('hide_closed', 'Hide confirmed closed postings', False)):
            variables[key] = tk.BooleanVar(value=self.filters.get(key, default))
            Toggle(frame, self, label, variables[key]).pack(fill='x', pady=3)
        self.wrapped(frame, 'Salary ranges must overlap your range. Monthly pay is multiplied by 12. Hourly pay, missing pay and other currencies are treated as unknown; no exchange rate is assumed. Work mode and experience use source labels and keyword clues.', 9, fg=MUTED).pack(fill='x', pady=10)
        def apply(clear=False):
            try:
                self.filters = {} if clear else validate_filters({key: var.get() for key, var in variables.items()})
                try:
                    saved = self.settings()
                except ValueError:
                    saved = self.store.get('settings', {})
                saved['filters'] = self.filters
                self.store.set('settings', saved)
                self.reset_view()
                win.destroy()
            except ValueError as exc:
                messagebox.showerror('Check filters', str(exc), parent=win)
        actions = tk.Frame(win, bg=SURFACE)
        actions.pack(fill='x', padx=22, pady=(0, 18))
        self.button(actions, 'Clear filters', lambda: apply(True)).pack(side='left')
        self.button(actions, 'Apply filters', apply, True).pack(side='right')

    def check_selected(self):
        if self.selected:
            self.check_postings([dict(self.selected)])

    def check_visible(self):
        self.check_postings([dict(job) for job in self.visible[:25]])

    def check_postings(self, jobs):
        if self.running or self.source_running:
            self.status.set('Wait for the current operation to finish.')
            return
        jobs = [j for j in jobs if j.get('job_url')]
        if not jobs:
            self.status.set('No original posting URLs to check in this selection.')
            return
        if not self._work_lock.acquire():
            self.status.set('The background worker is busy. Try again shortly.')
            return
        self.source_running = True
        self.stop.clear()
        self.status.set(f'Checking up to {len(jobs)} postings...')
        def worker():
            count = 0
            try:
                for job in jobs:
                    if self.stop.is_set():
                        break
                    result = inspect_listing(job)
                    self.store.apply_check(job['_key'], result)
                    count += 1
                    if 'HTTP 429' in result['availability_note'] or 'HTTP 403' in result['availability_note']:
                        break
                    if self.stop.wait(2):
                        break
                self.events.put(('checked', count))
            except Exception:
                self.log.exception('Posting verification failed')
                self.events.put(('check_error',))
        threading.Thread(target=worker, daemon=True).start()

    def backup_dialog(self):
        win = self.window('Backup & restore', '640x430')
        frame = tk.Frame(win, bg=SURFACE, padx=22, pady=20)
        frame.pack(fill='both', expand=True)
        self.wrapped(frame, 'Keep your entire workspace', 18, True).pack(fill='x')
        self.wrapped(frame, 'Backups include all jobs, notes, tracking dates, CV text, saved searches and history. Daily local snapshots retain the latest 7 days of use. Copy a backup to another drive for protection against disk loss.', 10, fg=MUTED).pack(fill='x', pady=12)
        self.wrapped(frame, 'Restoring replaces the current workspace. A separate safety copy is created first. Backups contain your CV and contact details.', 10, fg=MUTED).pack(fill='x', pady=8)
        def backup():
            path = filedialog.asksaveasfilename(parent=win, initialdir=BASE,
                initialfile=f'studio-backup-{datetime.now():%Y%m%d-%H%M%S}.sqlite3',
                defaultextension='.sqlite3', filetypes=[('Studio workspace backup', '*.sqlite3')])
            if path:
                try:
                    self.store.backup(path)
                    self.status.set(f'Full workspace backup saved to {path}.')
                    win.destroy()
                except Exception as exc:
                    messagebox.showerror('Backup failed', str(exc), parent=win)
        def restore():
            if self.running or self.source_running:
                messagebox.showinfo('Please wait', 'Finish or stop the current operation before restoring.', parent=win)
                return
            path = filedialog.askopenfilename(parent=win, initialdir=self.store.path.parent / 'backups', filetypes=[('Studio workspace backup', '*.sqlite3')])
            if not path:
                return
            try:
                count = self.store.validate_backup(path)
                if not messagebox.askyesno('Restore workspace', f'Replace this workspace with {count} jobs and all settings from:\n{path}\n\nA safety backup of the current workspace will be saved first.', parent=win):
                    return
                if not self._work_lock.acquire():
                    raise ValueError('Background worker is busy. Try again after it finishes.')
                try:
                    safety = self.store.restore(path)
                    self.profile = self.store.get('profile', '')
                    self.apply_settings(self.store.get('settings', {}))
                    self.notifications.set(self.store.get('notifications', True))
                    self.failed_tasks = self.store.get('failed_tasks', [])
                    self.active_settings = self.store.get('last_search_settings')
                    self.refresh_presets()
                    self.reload_jobs()
                    self.status.set(f'Workspace restored. Previous workspace saved to {safety}.')
                    win.destroy()
                finally:
                    self._work_lock.release()
            except Exception as exc:
                messagebox.showerror('Restore failed', str(exc), parent=win)
        self.button(frame, 'Save full backup', backup, True).pack(fill='x', pady=8)
        self.button(frame, 'Restore from backup', restore).pack(fill='x', pady=8)
        self.button(frame, 'Open automatic backups', lambda: __import__('os').startfile(self.store.daily_backup().parent)).pack(fill='x', pady=8)

    def background_dialog(self):
        from jobspy_background import enable_background, disable_background, background_status
        win = self.window('Background searches & notifications', '650x540')
        frame = tk.Frame(win, bg=SURFACE, padx=22, pady=20)
        frame.pack(fill='both', expand=True)
        self.wrapped(frame, 'Search while the window is closed', 18, True).pack(fill='x')
        state = tk.StringVar()
        def refresh():
            installed = background_status(self.store)
            enabled = installed and self.store.get('background_enabled', False)
            state.set(('Enabled' if enabled else 'Disabled') + f" · last run: {self.store.get('background_last_run', 'never')}")
        refresh()
        self.label(frame, '', 10, fg=PURPLE, textvariable=state).pack(fill='x', pady=12)
        self.wrapped(frame, 'Windows Task Scheduler checks every 5 minutes while you are signed in. Your computer must be awake and online. Saved search intervals still apply. Missed searches run once when available. The desktop and worker coordinate to avoid overlapping searches.', 10, fg=MUTED).pack(fill='x', pady=8)
        auto = tk.BooleanVar(value=self.store.get('auto_check_listings', True))
        Toggle(frame, self, 'Check aging listings in background', auto,
               lambda: self.store.set('auto_check_listings', auto.get())).pack(fill='x', pady=8)
        self.wrapped(frame, 'Checks up to 5 listings per run after 7 days without verification. Blocked pages remain unknown. Native notifications follow Windows notification and Focus settings.', 9, fg=MUTED).pack(fill='x', pady=8)
        def change(enable):
            try:
                (enable_background if enable else disable_background)(self.store)
                refresh()
                self.status.set('Background searches enabled.' if enable else 'Background searches disabled.')
            except Exception as exc:
                messagebox.showerror('Background setup failed', str(exc), parent=win)
                refresh()
        self.button(frame, 'Enable background mode', lambda: change(True), True).pack(fill='x', pady=5)
        self.button(frame, 'Disable background mode', lambda: change(False)).pack(fill='x', pady=5)
        self.button(frame, 'Send test notification', lambda: self.notify('JobSpy Studio', 'Windows notifications are ready.')).pack(fill='x', pady=5)

    def save_notifications(self):
        self.store.set('notifications', self.notifications.get())

    def notify(self, title, text):
        if not self.notifications.get():
            return False
        self.alerts_text.set(text)
        if native_notify(title, text):
            return True
        self.bell()
        toast = self.window(title, '460x170')
        self.wrapped(toast, title, 16, True).pack(fill='x', padx=20, pady=(16, 8))
        self.wrapped(toast, text, 10, fg=MUTED).pack(fill='x', padx=20)
        toast.after(15000, toast.destroy)
        return True

    def schedule_tick(self):
        try:
            if not self.running and not self.source_running:
                maintenance = WorkspaceLock(self.store.path)
                if maintenance.acquire():
                    try:
                        self.store.daily_backup()
                        if self.notifications.get():
                            for kind, job, field in self.store.due_reminders():
                                if self.notify(kind, f'{job["title"]} · {job.get("company", "")}'):
                                    self.store.annotate(job['_key'], **{field: date.today().isoformat()})
                    finally:
                        maintenance.release()
                self.reload_jobs()
                now = time.time()
                due = next((p for p in self.store.presets() if p['interval_hours'] and p['next_run'] <= now), None)
                if due:
                    self.start(due['settings'], preset_name=due['name'])
        except Exception:
            self.log.exception('Schedule check failed')
        if not self._closing:
            self._schedule_timer = self.after(30000, self.schedule_tick)

    def export(self):
        if not self.visible:
            messagebox.showinfo('Export', 'No jobs in the current view.', parent=self)
            return
        path = filedialog.asksaveasfilename(parent=self, initialdir=BASE,
            initialfile=f'jobs-{datetime.now():%Y%m%d-%H%M%S}.csv', defaultextension='.csv', filetypes=[('CSV', '*.csv')])
        if not path:
            return
        try:
            rows = []
            for job in self.visible:
                row = {k: v for k, v in job.items() if not k.startswith('_')}
                row.update({f'tracker_{k}': job.get('_'+k, '') for k in ('status', 'favorite', 'notes', 'deadline',
                    'deadline_origin', 'applied_on', 'recruiter', 'contact', 'interview_at', 'follow_up',
                    'checked_at', 'availability', 'availability_note')})
                result = self.score(job)
                row['skill_overlap_percent'] = result['score'] if result['score'] is not None else ''
                # Prevent spreadsheet formula execution in user-controlled values.
                rows.append({k: "'"+str(v) if str(v).lstrip().startswith(('=', '+', '-', '@')) else v for k, v in row.items()})
            fields = list(dict.fromkeys(k for row in rows for k in row))
            with open(path, 'w', encoding='utf-8-sig', newline='') as stream:
                writer = csv.DictWriter(stream, fieldnames=fields)
                writer.writeheader()
                writer.writerows(rows)
            self.status.set(f'Exported {len(rows)} jobs and tracking fields to {Path(path).name}.')
        except Exception as exc:
            messagebox.showerror('Export failed', str(exc), parent=self)

    def callback_error(self, exc_type, exc, tb):
        self.log.error('Interface error', exc_info=(exc_type, exc, tb))
        messagebox.showerror('Operation failed', 'Your saved jobs are preserved. Details were written to data/studio.log.', parent=self)

    def close(self):
        self._closing = True
        self.stop.set()
        if self._schedule_timer:
            self.after_cancel(self._schedule_timer)
        try:
            self.store.set('settings', self.settings())
        except ValueError:
            pass
        super().close()

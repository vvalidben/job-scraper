"""Designed native UI; the JobSpy search and file-handling code stays in the core."""
from __future__ import annotations

import math
import time
import tkinter as tk
from tkinter import ttk

import jobspy_desktop as core

BG = '#0b1020'
SIDE = '#0f1629'
SURFACE = '#151e33'
RAISED = '#1c2841'
EDGE = '#29354e'
TEXT = '#f1f3fb'
MUTED = '#9ba9c5'
PURPLE = '#bca7ff'
LIME = '#dbf596'
AMBER = '#ebc48d'


def mix(a, b, t):
    return '#' + ''.join(f'{round(int(a[i:i+2], 16) * (1-t) + int(b[i:i+2], 16) * t):02x}' for i in (1, 3, 5))


def rounded(canvas, x1, y1, x2, y2, radius=16, **options):
    r = max(0, min(radius, (x2-x1)/2, (y2-y1)/2))
    points = [x1+r,y1,x2-r,y1,x2,y1,x2,y1+r,x2,y2-r,x2,y2,x2-r,y2,
              x1+r,y2,x1,y2,x1,y2-r,x1,y1+r,x1,y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=24, **options)


class Motion:
    """One animation clock with replaceable channels, so rapid input stays smooth."""
    def __init__(self, root, enabled):
        self.root, self.enabled, self.tasks = root, enabled, {}
        self.timer = None

    def animate(self, key, duration, update, done=None, delay=0):
        if not self.enabled.get():
            update(1)
            if done:
                done()
            return
        self.tasks[key] = (time.perf_counter()+delay, duration, update, done)
        if self.timer is None:
            self.timer = self.root.after(16, self.tick)

    def tick(self):
        self.timer = None
        now = time.perf_counter()
        for key, task in list(self.tasks.items()):
            start, duration, update, done = task
            if now < start:
                continue
            t = min(1, (now-start)/duration) if self.enabled.get() else 1
            try:
                update(1-(1-t)**3)
            except tk.TclError:
                self.tasks.pop(key, None)
                continue
            if t >= 1 and self.tasks.get(key) is task:
                self.tasks.pop(key, None)
                if done:
                    done()
        if self.tasks:
            self.timer = self.root.after(16, self.tick)

    def cancel(self, key):
        self.tasks.pop(key, None)

    def close(self):
        self.tasks.clear()
        if self.timer is not None:
            self.root.after_cancel(self.timer)
            self.timer = None


class Action(tk.Canvas):
    def __init__(self, parent, app, text, command, primary=False, compact=False):
        super().__init__(parent, bg=parent.cget('bg'), height=38 if compact else 46,
                         width=114 if compact else 210, bd=0, highlightthickness=0,
                         cursor='hand2', takefocus=1)
        self.app, self.text, self.command, self.primary = app, text, command, primary
        self.state, self.hover, self.focused = 'normal', 0.0, False
        self.bind('<Configure>', lambda e: self.draw())
        self.bind('<Enter>', lambda e: self.over(1))
        self.bind('<Leave>', lambda e: self.over(0))
        self.bind('<Button-1>', lambda e: self.invoke())
        self.bind('<Return>', lambda e: self.invoke())
        self.bind('<space>', lambda e: self.invoke())
        self.bind('<FocusIn>', lambda e: self.focus(True))
        self.bind('<FocusOut>', lambda e: self.focus(False))

    def focus(self, value):
        self.focused = value
        self.draw()

    def over(self, target):
        start = self.hover
        self.app.motion.animate((self, 'hover'), .17, lambda t: self.set_hover(start+(target-start)*t))

    def set_hover(self, value):
        self.hover = value
        self.draw()

    def draw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        color = mix(PURPLE, '#d7cbff', self.hover) if self.primary else mix(SURFACE, RAISED, self.hover)
        if self.state == 'disabled':
            color = RAISED
        rounded(self, 1, 1, w-1, h-1, 12, fill=color,
                outline=PURPLE if self.focused else (color if self.primary else EDGE), width=1)
        self.create_text(w/2 + self.hover*1.5, h/2, text=self.text,
                         fill=BG if self.primary and self.state != 'disabled' else (MUTED if self.state == 'disabled' else TEXT),
                         font=('Segoe UI', 10, 'bold'))

    def configure(self, cnf=None, **kwargs):
        if cnf:
            kwargs.update(cnf)
        if 'text' in kwargs:
            self.text = kwargs.pop('text')
        if 'state' in kwargs:
            self.state = kwargs.pop('state')
            self.config(cursor='arrow' if self.state == 'disabled' else 'hand2')
        if kwargs:
            super().configure(**kwargs)
        if hasattr(self, 'text'):
            self.draw()

    def invoke(self):
        if self.state != 'disabled':
            self.command()
        return 'break'

    def destroy(self):
        self.app.motion.cancel((self, 'hover'))
        super().destroy()


class Toggle(tk.Canvas):
    def __init__(self, parent, app, text, variable, command=None, height=34):
        super().__init__(parent, bg=parent.cget('bg'), width=225, height=height, bd=0,
                         highlightthickness=0, takefocus=1, cursor='hand2')
        self.app, self.text, self.var, self.command = app, text, variable, command
        self.position = float(variable.get())
        self.focused = False
        self.trace = variable.trace_add('write', self.changed)
        self.bind('<Configure>', lambda e: self.draw())
        self.bind('<Button-1>', lambda e: self.invoke())
        self.bind('<space>', lambda e: self.invoke())
        self.bind('<Return>', lambda e: self.invoke())
        self.bind('<FocusIn>', lambda e: self.focus(True))
        self.bind('<FocusOut>', lambda e: self.focus(False))

    def focus(self, value):
        self.focused = value
        self.draw()

    def changed(self, *_):
        start, end = self.position, float(self.var.get())
        self.app.motion.animate((self, 'toggle'), .2, lambda t: self.update_position(start+(end-start)*t))

    def update_position(self, value):
        self.position = value
        self.draw()

    def draw(self):
        self.delete('all')
        w, h = self.winfo_width(), self.winfo_height()
        if self.focused:
            rounded(self, 0, 0, w-1, h-1, 8, fill=self.cget('bg'), outline=PURPLE)
        self.create_text(2, h/2, anchor='w', text=self.text, fill=TEXT, font=('Segoe UI', 10))
        x, y = w-39, h/2-10
        rounded(self, x, y, x+36, y+20, 10, fill=mix(EDGE, PURPLE, self.position), outline='')
        knob = x+10+16*self.position
        self.create_oval(knob-7, y+3, knob+7, y+17, fill=BG if self.position>.5 else MUTED, outline='')

    def invoke(self):
        self.var.set(not self.var.get())
        if self.command:
            self.command()
        return 'break'

    def destroy(self):
        self.var.trace_remove('write', self.trace)
        self.app.motion.cancel((self, 'toggle'))
        super().destroy()


class Metric(tk.Canvas):
    def __init__(self, parent, app, title, subtitle, color):
        super().__init__(parent, height=86, width=180, bg=BG, bd=0, highlightthickness=0)
        self.app, self.title, self.subtitle, self.color = app, title, subtitle, color
        self.value, self.target = 0.0, 0
        self.bind('<Configure>', lambda e: self.draw())

    def configure(self, cnf=None, **kwargs):
        if 'text' in kwargs:
            target = int(kwargs.pop('text'))
            if target != self.target:
                self.target = target
                start = self.value
                self.app.motion.animate((self, 'count'), .5, lambda t: self.set_value(start+(target-start)*t))
        if kwargs or cnf:
            super().configure(cnf, **kwargs)

    def set_value(self, value):
        self.value = value
        self.draw()

    def draw(self):
        self.delete('all')
        w = self.winfo_width()
        rounded(self, 0, 0, w-1, 85, 16, fill=SURFACE, outline=EDGE)
        self.create_text(19, 16, anchor='nw', text=self.title, fill=MUTED, font=('Segoe UI', 8, 'bold'))
        self.create_text(18, 35, anchor='nw', text=f'{round(self.value):02d}', fill=self.color, font=('Segoe UI', 24, 'bold'))
        self.create_text(w-17, 63, anchor='se', text=self.subtitle, fill=MUTED, font=('Segoe UI', 9))


class Hero(tk.Canvas):
    def __init__(self, parent, app):
        super().__init__(parent, height=158, bg=BG, bd=0, highlightthickness=0)
        self.app, self.phase, self.clock = app, 0.0, None
        self.bind('<Configure>', lambda e: self.draw())
        self.clock = self.after(40, self.tick)

    def draw(self):
        self.delete('all')
        w = self.winfo_width()
        rounded(self, 0, 0, w-1, 157, 23, fill='#1a2340', outline='#323e5d')
        for x in range(max(480, w-360), w-20, 28):
            self.create_line(x, 18, x, 140, fill='#25304c')
        for y in range(24, 145, 28):
            self.create_line(max(490, w-350), y, w-22, y, fill='#25304c')
        self.create_text(25, 17, anchor='nw', text='A NEW CHAPTER STARTS HERE', fill=LIME, font=('Segoe UI', 8, 'bold'))
        self.create_text(23, 37, anchor='nw', text='Your next move, in cybersecurity.', fill=TEXT, font=('Segoe UI', 25, 'bold'))
        self.create_text(25, 84, anchor='nw', text='Discover internships. Find your people. Build what comes next.',
                         fill='#bbc6df', font=('Segoe UI', 10))
        x = 25
        for label in ('MOROCCO', 'RABAT + CASABLANCA', 'EARLY CAREERS'):
            width = len(label)*6.0+22
            rounded(self, x, 117, x+width, 140, 11, fill='#26334e', outline='#3a4965')
            self.create_text(x+11, 128, text=label, anchor='w', fill='#d1daef', font=('Segoe UI', 8, 'bold'))
            x += width+8
        if w > 910:
            cx, cy = w-89, 78
            for radius in (40, 59):
                self.create_oval(cx-radius, cy-radius, cx+radius, cy+radius, outline='#4a5275')
            self.create_polygon(cx-18,cy-21,cx,cy-29,cx+18,cy-21,cx+16,cy+9,cx,cy+26,cx-16,cy+9,
                                fill='#303657', outline=PURPLE, width=2)
            self.create_line(cx-8,cy-1,cx-1,cy+6,cx+11,cy-8, fill=LIME, width=3)
            self.dot = self.create_oval(0,0,0,0, fill=LIME, outline='', tags='orb')
            self.orbit = (cx, cy)
            self.move_dot()

    def move_dot(self):
        if hasattr(self, 'dot') and self.find_withtag('orb'):
            cx, cy = self.orbit
            x, y = cx+59*math.cos(self.phase), cy+59*math.sin(self.phase)
            self.coords(self.dot, x-3, y-3, x+3, y+3)

    def tick(self):
        if self.app.motion_enabled.get():
            self.phase += .015
            self.move_dot()
        self.clock = self.after(40, self.tick)

    def destroy(self):
        if self.clock:
            self.after_cancel(self.clock)
        super().destroy()


class SearchPulse(tk.Canvas):
    def __init__(self, parent, app):
        super().__init__(parent, height=3, bg=BG, bd=0, highlightthickness=0)
        self.app, self.active, self.phase, self.clock = app, False, 0, None
        self.bind('<Configure>', lambda e: self.draw())

    def draw(self):
        self.delete('all')
        w = self.winfo_width()
        self.create_line(0, 1, w, 1, fill=EDGE, width=2)
        if self.active:
            width = w*.23
            x = (self.phase % 1)*(w+width)-width
            self.create_line(max(0,x), 1, min(w,x+width), 1, fill=PURPLE, width=3)

    def tick(self):
        if not self.active:
            return
        self.phase = self.phase+.018 if self.app.motion_enabled.get() else .45
        self.draw()
        self.clock = self.after(25 if self.app.motion_enabled.get() else 200, self.tick)

    def start(self, *_):
        if not self.active:
            self.active = True
            self.tick()

    def stop(self):
        self.active = False
        if self.clock:
            self.after_cancel(self.clock)
            self.clock = None
        self.draw()

    def destroy(self):
        self.stop()
        super().destroy()


class JobCard(tk.Canvas):
    def __init__(self, parent, app, job, index):
        super().__init__(parent, bg=BG, height=158, bd=0, highlightthickness=0,
                         cursor='hand2', takefocus=1)
        self.app, self.job, self.hover, self.reveal = app, job, 0.0, 0.0
        self.bind('<Configure>', lambda e: self.draw())
        self.bind('<Enter>', lambda e: self.over(1))
        self.bind('<Leave>', lambda e: self.over(0))
        self.bind('<Button-1>', lambda e: app.select(job))
        self.bind('<Return>', lambda e: app.select(job))
        self.bind('<space>', lambda e: app.select(job))
        self.bind('<FocusIn>', lambda e: self.over(1))
        self.bind('<FocusOut>', lambda e: self.over(0))
        self.app.motion.animate((self, 'reveal'), .38, self.set_reveal, delay=min(index, 7)*.035)

    def set_reveal(self, value):
        self.reveal = value
        self.draw()

    def over(self, target):
        start = self.hover
        self.app.motion.animate((self, 'hover'), .18, lambda t: self.set_hover(start+(target-start)*t))

    def set_hover(self, value):
        self.hover = value
        self.draw()

    def draw(self):
        self.delete('all')
        w = self.winfo_width()
        if w < 40:
            return
        selected = self.app.selected is self.job
        bg = mix(BG, mix(SURFACE, RAISED, self.hover), self.reveal)
        ink = mix(BG, TEXT, self.reveal)
        muted = mix(BG, MUTED, self.reveal)
        y = 12*(1-self.reveal)
        company = self.job.get('company') or 'Company not provided'
        title = self.job.get('title') or 'Untitled role'
        marker = ''.join(word[0] for word in company.split()[:2]).upper()
        rounded(self, 16, 18+y, 54, 56+y, 11, fill='#302c4b', outline='', tags='foreground')
        self.create_text(35, 37+y, text=marker, fill=mix(BG, PURPLE, self.reveal), font=('Segoe UI', 10, 'bold'), tags='foreground')
        self.create_text(67, 18+y, anchor='nw', text=(self.job.get('site') or 'Job board').upper(),
                         fill=muted, font=('Segoe UI', 8, 'bold'), tags='foreground')
        title_id = self.create_text(66, 38+y, anchor='nw', text=title, fill=ink, width=w-91,
                                    font=('Segoe UI', 12, 'bold'), tags='foreground')
        title_bottom = self.bbox(title_id)[3]
        company_id = self.create_text(17, title_bottom+12, anchor='nw', text=company, fill=muted,
                                      width=w-35, font=('Segoe UI', 10), tags='foreground')
        company_bottom = self.bbox(company_id)[3]
        location_id = self.create_text(17, company_bottom+7, anchor='nw', text=self.job.get('location') or 'Location not provided',
                                       fill=muted, width=w-35, font=('Segoe UI', 9), tags='foreground')
        bottom = self.bbox(location_id)[3]+13
        evidence = core.internship_evidence(self.job)
        tag = 'INTERNSHIP' if evidence else 'TYPE UNVERIFIED'
        color = LIME if evidence else AMBER
        badge_w = 86 if evidence else 120
        rounded(self, 17, bottom, 17+badge_w, bottom+23, 9, fill='#26342d' if evidence else '#352d28', outline='', tags='foreground')
        self.create_text(26, bottom+11, anchor='w', text=tag, fill=mix(BG, color, self.reveal), font=('Segoe UI', 8, 'bold'), tags='foreground')
        self.create_text(w-18, bottom+11, anchor='e', text=self.job.get('date_posted') or 'Date unavailable',
                         fill=muted, font=('Segoe UI', 9), tags='foreground')
        h = max(154, bottom+37-y)
        if abs(self.winfo_reqheight()-h) > 1:
            self.configure(height=h)
        background = rounded(self, 1, 1, w-2, h-1, 17, fill=bg,
                              outline=LIME if selected else mix(EDGE, PURPLE, self.hover), width=1)
        self.tag_lower(background)
        if selected:
            self.create_oval(w-28, 19, w-20, 27, fill=LIME, outline='')

    def destroy(self):
        self.app.motion.cancel((self, 'reveal'))
        self.app.motion.cancel((self, 'hover'))
        super().destroy()


class StudioApp(core.JobApp):
    def configure_styles(self):
        self.motion_enabled = tk.BooleanVar(value=True)
        self.motion = Motion(self, self.motion_enabled)
        self.card_widgets = []
        self.configure(bg=BG)
        width = min(1460, self.winfo_screenwidth()-80)
        height = min(850, self.winfo_screenheight()-150)
        self.geometry(f'{width}x{height}')
        self.minsize(1140, 780)
        style = ttk.Style(self)
        style.theme_use('clam')
        style.configure('Studio.TCombobox', padding=8, fieldbackground=RAISED,
                        background=RAISED, foreground=TEXT, arrowcolor=PURPLE,
                        bordercolor=EDGE, lightcolor=EDGE, darkcolor=EDGE)
        style.map('Studio.TCombobox', fieldbackground=[('readonly', RAISED)],
                  foreground=[('readonly', TEXT)], selectbackground=[('readonly', RAISED)],
                  selectforeground=[('readonly', TEXT)])
        style.configure('Studio.Vertical.TScrollbar', troughcolor=BG, background=EDGE,
                        bordercolor=BG, lightcolor=EDGE, darkcolor=EDGE, arrowcolor=MUTED,
                        width=9, arrowsize=9, gripcount=0)
        style.map('Studio.Vertical.TScrollbar', background=[('active', MUTED)])
        self.option_add('*TCombobox*Listbox.background', RAISED)
        self.option_add('*TCombobox*Listbox.foreground', TEXT)
        self.option_add('*TCombobox*Listbox.selectBackground', '#534377')

    def label(self, parent, text, size=10, bold=False, fg=TEXT, bg=None, **kwargs):
        return tk.Label(parent, text=text, font=('Segoe UI', size, 'bold' if bold else 'normal'),
                        fg=fg, bg=bg or parent.cget('bg'), anchor='w', **kwargs)

    def button(self, parent, text, command, primary=False):
        return Action(parent, self, text, command, primary)

    def section(self, parent, number, title):
        row = tk.Frame(parent, bg=SIDE)
        row.pack(fill='x', pady=(19, 6))
        self.label(row, number, 8, True, PURPLE).pack(side='left', padx=(0, 9))
        self.label(row, title.upper(), 8, True, MUTED).pack(side='left')

    def entry(self, parent, variable, bg=SURFACE):
        border = tk.Frame(parent, bg=EDGE, padx=1, pady=1)
        entry = tk.Entry(border, textvariable=variable, relief='flat', bd=10, bg=bg,
                         fg=TEXT, insertbackground=PURPLE, selectbackground='#534377',
                         selectforeground=TEXT, font=('Segoe UI', 11))
        entry.pack(fill='x')
        entry.bind('<FocusIn>', lambda e: border.configure(bg=PURPLE))
        entry.bind('<FocusOut>', lambda e: border.configure(bg=EDGE))
        return border

    def scroll(self, parent, bg):
        area = core.ScrollArea(parent, bg=bg)
        for child in area.winfo_children():
            if isinstance(child, ttk.Scrollbar):
                child.configure(style='Studio.Vertical.TScrollbar')
        return area

    def build(self):
        sidebar = tk.Frame(self, bg=SIDE, width=272)
        sidebar.pack(side='left', fill='y')
        sidebar.pack_propagate(False)
        brand = tk.Frame(sidebar, bg=SIDE, padx=23, pady=23)
        brand.pack(fill='x')
        mark = tk.Canvas(brand, width=36, height=36, bg=SIDE, bd=0, highlightthickness=0)
        mark.pack(side='left', padx=(0, 11))
        rounded(mark, 0,0,35,35,10,fill=PURPLE,outline='')
        mark.create_text(18,17,text='J',fill=BG,font=('Segoe UI',20,'bold'))
        names = tk.Frame(brand, bg=SIDE)
        names.pack(side='left')
        self.label(names, 'JobSpy', 19, True).pack(fill='x')
        self.label(names, 'STUDIO  /  MOROCCO', 7, True, MUTED).pack(fill='x')
        tk.Frame(sidebar, bg=EDGE, height=1).pack(fill='x', padx=23)
        bottom = tk.Frame(sidebar, bg=SIDE, padx=21, pady=18)
        bottom.pack(side='bottom', fill='x')
        self.search_button = self.button(bottom, 'Search opportunities  →', self.start, True)
        self.search_button.pack(fill='x')
        self.cancel_button = self.button(bottom, 'Stop after this request', self.cancel)
        self.label(bottom, 'Your search. Your next chapter.', 8, fg=MUTED).pack(anchor='center', pady=(12, 0))
        filter_scroll = self.scroll(sidebar, SIDE)
        filter_scroll.pack(fill='both', expand=True, padx=(20, 10), pady=(7, 0))
        form = filter_scroll.body
        self.section(form, '01', 'Your focus')
        self.entry(form, self.query).pack(fill='x', pady=(2, 6))
        self.label(form, 'cybersécurité · SOC · sécurité', 8, fg=MUTED).pack(fill='x')
        self.section(form, '02', 'Where you want to go')
        for city, var in self.cities.items():
            Toggle(form, self, city, var).pack(fill='x')
        self.section(form, '03', 'Your sources')
        for site, var in self.sites.items():
            Toggle(form, self, site.title(), var).pack(fill='x')
        self.section(form, '04', 'Make it yours')
        Toggle(form, self, 'Internships / stages', self.internships, self.render).pack(fill='x')
        self.label(form, 'POSTED', 8, True, MUTED).pack(fill='x', pady=(14, 6))
        ttk.Combobox(form, textvariable=self.age, state='readonly', style='Studio.TCombobox',
                     values=('Any time', 'Past 24 hours', 'Past 7 days', 'Past 30 days')).pack(fill='x')
        self.label(form, 'RESULTS PER BOARD / CITY', 8, True, MUTED).pack(fill='x', pady=(14, 6))
        ttk.Combobox(form, textvariable=self.limit, state='readonly', style='Studio.TCombobox',
                     values=('10', '20', '30', '50')).pack(fill='x', pady=(0, 14))
        self.filter_scroll = filter_scroll

        main = tk.Frame(self, bg=BG, padx=26, pady=20)
        main.pack(side='left', fill='both', expand=True)
        top = tk.Frame(main, bg=BG)
        top.pack(fill='x', pady=(0, 16))
        self.label(top, 'THE OPPORTUNITY WORKSPACE', 8, True, MUTED).pack(side='left')
        Action(top, self, 'Export CSV  ↗', self.export, compact=True).pack(side='right')
        Action(top, self, 'Open CSV', self.import_csv, compact=True).pack(side='right', padx=8)
        Toggle(top, self, 'Motion', self.motion_enabled, height=32).pack(side='right', padx=(0, 15))
        # Keep the compact motion control from consuming the header.
        for widget in top.winfo_children():
            if isinstance(widget, Toggle):
                widget.configure(width=103)
        self.hero = Hero(main, self)
        self.hero.pack(fill='x')
        stats = tk.Frame(main, bg=BG)
        stats.pack(fill='x', pady=(16, 20))
        self.metrics = []
        for i, (title, subtitle, color) in enumerate((('YOUR MATCHES', 'in this view', PURPLE),
                ('INTERNSHIP LABELS', 'review each posting', LIME), ('DISCOVERED', 'total listings', TEXT))):
            stats.columnconfigure(i, weight=1, uniform='metric')
            metric = Metric(stats, self, title, subtitle, color)
            metric.grid(row=0, column=i, sticky='ew', padx=(0, 10 if i<2 else 0))
            self.metrics.append(metric)
        tools = tk.Frame(main, bg=BG)
        tools.pack(fill='x', pady=(0, 10))
        self.label(tools, 'The shortlist', 18, True).pack(side='left')
        self.label(tools, 'Newest first', 9, fg=MUTED).pack(side='left', padx=15)
        Toggle(tools, self, 'Include unverified matches', self.unverified, self.render).pack(side='right')
        search = tk.Frame(main, bg=BG)
        search.pack(fill='x', pady=(0, 15))
        self.label(search, 'FILTER RESULTS', 8, True, MUTED).pack(side='left', padx=(0, 13))
        self.entry(search, self.find).pack(side='left', fill='x', expand=True)
        split = tk.PanedWindow(main, orient='horizontal', bg=BG, bd=0, sashwidth=14, sashrelief='flat')
        split.pack(fill='both', expand=True)
        self.cards = self.scroll(split, BG)
        self.detail_shell = tk.Canvas(split, bg=BG, highlightthickness=0, bd=0)
        self.detail_shell.bind('<Configure>', self.resize_detail)
        self.detail_scroll = self.scroll(self.detail_shell, SURFACE)
        self.details = self.detail_scroll.body
        self.detail_window = self.detail_shell.create_window((21, 22), window=self.detail_scroll, anchor='nw')
        self.detail_action = self.button(self.detail_shell, 'View opportunity  ↗', self.open_selected, True)
        self.detail_action.configure(bg=SURFACE)
        self.detail_action_window = self.detail_shell.create_window((21, 22), window=self.detail_action, anchor='nw', state='hidden')
        split.add(self.cards, stretch='always', minsize=310, width=600)
        split.add(self.detail_shell, stretch='never', minsize=290, width=335)
        self.progress = SearchPulse(main, self)
        self.progress.pack(fill='x', pady=(15, 10))
        footer = tk.Frame(main, bg=BG)
        footer.pack(fill='x')
        self.status_label = tk.Label(footer, textvariable=self.status, bg=BG, fg=MUTED,
                                     font=('Segoe UI', 9), anchor='w', justify='left', wraplength=740)
        self.status_label.pack(side='left', fill='x', expand=True)
        self.status_label.bind('<Configure>', lambda e: self.status_label.configure(wraplength=max(200, e.width)))
        more = tk.Label(footer, text='Search details  ↗', bg=BG, fg=PURPLE, font=('Segoe UI', 9), cursor='hand2', takefocus=1)
        more.pack(side='right', padx=(12, 0))
        more.bind('<Button-1>', lambda e: self.show_notes())
        more.bind('<Return>', lambda e: self.show_notes())
        self.render()

    def resize_detail(self, event):
        self.detail_shell.delete('panel')
        panel = rounded(self.detail_shell, 1,1,event.width-1,event.height-1,18,
                         fill=SURFACE,outline=EDGE,tags='panel')
        self.detail_shell.tag_lower(panel)
        self.detail_shell.itemconfigure(self.detail_window, width=max(200,event.width-42),
                                        height=max(100,event.height-44-(66 if self.selected else 0)))
        self.detail_shell.coords(self.detail_action_window, 21, event.height-67)
        self.detail_shell.itemconfigure(self.detail_action_window, width=max(200,event.width-42), height=46,
                                        state='normal' if self.selected else 'hidden')

    def wheel(self, event):
        widget = self.winfo_containing(event.x_root, event.y_root)
        while widget is not None:
            for area in (self.cards, self.filter_scroll, self.detail_scroll):
                if widget is area:
                    area.canvas.yview_scroll(-int(event.delta/120), 'units')
                    return 'break'
            widget = getattr(widget, 'master', None)

    def render(self):
        word = self.find.get().casefold().strip()
        self.visible = [j for j in self.jobs if
                        (not self.internships.get() or self.unverified.get() or core.internship_evidence(j))
                        and (not word or word in ' '.join(j.get(k, '') for k in ('title','company','location')).casefold())]
        if self.selected not in self.visible:
            self.selected = self.visible[0] if self.visible else None
        for metric, value in zip(self.metrics, (len(self.visible), sum(bool(core.internship_evidence(j)) for j in self.jobs), len(self.jobs))):
            metric.configure(text=str(value))
        for child in self.cards.body.winfo_children():
            child.destroy()
        self.card_widgets = []
        if not self.visible:
            self.empty_state()
        for index, job in enumerate(self.visible):
            card = JobCard(self.cards.body, self, job, index)
            card.pack(fill='x', padx=(0, 2), pady=(0, 11))
            self.card_widgets.append(card)
        self.show_detail()

    def empty_state(self):
        frame = tk.Frame(self.cards.body, bg=BG, padx=24, pady=27)
        frame.pack(fill='x')
        icon = tk.Canvas(frame, width=86, height=75, bg=BG, bd=0, highlightthickness=0)
        icon.pack(anchor='w', pady=(0, 17))
        rounded(icon, 3,3,68,68,20,fill='#252340',outline='#494164')
        icon.create_oval(21,19,44,42,outline=PURPLE,width=2)
        icon.create_line(42,41,53,53,fill=LIME,width=3)
        title = 'Finding your next chapter…' if self.running else ('A little more discovery.' if self.jobs else 'Big things start here.')
        text = ('Searching the job boards. Matches will appear here as each source finishes.' if self.running else
                'No listings match the current view. Try another keyword, clear the result filter, or include unverified matches to review more roles.' if self.jobs else
                'Your next cybersecurity role is out there. Choose your focus and cities, then start exploring.')
        title_label = self.label(frame, title, 20, True, wraplength=420, justify='left')
        title_label.pack(fill='x', pady=(0, 12))
        title_label.bind('<Configure>', lambda e: title_label.configure(wraplength=max(180,e.width)))
        body = self.label(frame, text, 11, fg=MUTED, wraplength=420, justify='left')
        body.pack(fill='x')
        body.bind('<Configure>', lambda e: body.configure(wraplength=max(180,e.width)))
        row = tk.Frame(frame, bg=BG)
        row.pack(fill='x', pady=(24, 0))
        for term in ('cybersécurité', 'SOC', 'sécurité'):
            button = Action(row, self, term, lambda term=term: self.query.set(term), compact=True)
            button.configure(width=108)
            button.pack(side='left', padx=(0, 7))

    def select(self, job):
        self.selected = job
        for card in self.card_widgets:
            card.draw()
        self.show_detail()

    def wrapped(self, parent, text, size=10, bold=False, fg=TEXT):
        label = self.label(parent, text, size, bold, fg, wraplength=270, justify='left')
        label.bind('<Configure>', lambda e: label.configure(wraplength=max(150,e.width)))
        return label

    def show_detail(self):
        for child in self.details.winfo_children():
            child.destroy()
        self.label(self.details, 'THE CLOSER LOOK', 8, True, PURPLE).pack(fill='x', pady=(0, 20))
        job = self.selected
        from types import SimpleNamespace
        self.resize_detail(SimpleNamespace(width=self.detail_shell.winfo_width(), height=self.detail_shell.winfo_height()))
        if not job:
            self.wrapped(self.details, 'A role worth\na closer look.', 22, True).pack(fill='x', pady=(8, 14))
            self.wrapped(self.details, 'Select a card to explore the company, location and original posting.', 11, fg=MUTED).pack(fill='x')
            tk.Frame(self.details, height=1, bg=EDGE).pack(fill='x', pady=23)
            self.wrapped(self.details, 'A small reminder', 10, True, LIME).pack(fill='x', pady=(0, 8))
            self.wrapped(self.details, 'Internship labels are clues. Always read the original listing before you apply.', 10, fg=MUTED).pack(fill='x')
            return
        self.wrapped(self.details, job.get('title') or 'Untitled role', 18, True).pack(fill='x')
        self.wrapped(self.details, job.get('company') or 'Company not provided', 11, fg=PURPLE).pack(fill='x', pady=(10, 16))
        tk.Frame(self.details, height=1, bg=EDGE).pack(fill='x', pady=(0, 13))
        for name, value in [('LOCATION',job.get('location')),('POSTED',job.get('date_posted')),
                            ('INTERNSHIP CHECK',core.internship_evidence(job) or 'Not verified — review the original')]:
            self.label(self.details, name, 8, True, MUTED).pack(fill='x', pady=(7, 4))
            self.wrapped(self.details, value or 'Not provided', 10).pack(fill='x')
        self.label(self.details, 'ABOUT THIS ROLE', 8, True, MUTED).pack(fill='x', pady=(18, 9))
        text = tk.Text(self.details, bg=SURFACE, fg=MUTED, font=('Segoe UI',10), wrap='word',
                       width=20,height=3,bd=0,highlightthickness=0,insertbackground=PURPLE)
        text.insert('1.0',job.get('description') or 'Open the original posting for the full requirements, description and application details.')
        text.configure(state='disabled')
        text.pack(fill='both',expand=True)
        text.bind('<MouseWheel>',lambda e:(text.yview_scroll(-int(e.delta/120),'units'),'break')[1])

    def open_selected(self):
        if self.selected:
            self.open_job(self.selected)

    def show_notes(self):
        win = tk.Toplevel(self)
        win.title('JobSpy / Search details')
        win.geometry('710x420')
        win.configure(bg=BG)
        self.label(win,'Search details',20,True).pack(fill='x',padx=24,pady=(22,14))
        text = tk.Text(win,wrap='word',bg=SURFACE,fg=TEXT,padx=18,pady=18,
                       font=('Segoe UI',10),bd=0,highlightthickness=0)
        text.pack(fill='both',expand=True,padx=24,pady=(0,24))
        text.insert('1.0','\n\n'.join(self.notes) or 'No search warnings. Each job board may return fewer listings than requested. Check each original posting before applying.')
        text.configure(state='disabled')

    def close(self):
        self.motion.close()
        super().close()

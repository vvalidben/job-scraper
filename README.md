# JobSpy Studio

A local Windows desktop workspace for finding and tracking opportunities in Morocco.
Double-click **Open JobSpy**, or run `jobspy_desktop.py` with the project virtual environment.

## Setup

Use Python 3.11 or newer with Tk support. From this folder in PowerShell:

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements.txt
& .\.venv\Scripts\pythonw.exe .\jobspy_desktop.py
```

Dependencies pin the existing JobSpy, Requests and Beautiful Soup versions. PDF CV import uses
`pypdf`; DOCX and text import work locally. Windows notifications use the built-in Windows
notification API through PowerShell; no separate notification package is needed.

## Search and filters

Select search terms, cities, boards and posting age. Separate search terms with `;`.
**Advanced filters** adds work arrangement, experience level, annual salary limits, excluded
keywords/phrases and company names, and an option to hide confirmed closed postings.
Filters affect the shortlist and new-job alerts; fetched jobs remain in the library.
Filters are saved with search presets.

Work mode and experience use source labels and keyword clues. Missing information appears
as **Unknown**. A false remote flag alone does not prove a job is on-site. Internship matching
still follows the internship and unverified-match toggles.

Salary filtering compares overlapping ranges in the selected currency. Monthly amounts are
multiplied by 12. No currency conversion or hourly-to-yearly estimate is made. Missing salary,
hourly/daily/weekly pay and other currencies count as unknown. Turn off **Include salaries that
cannot be compared** for strict filtering. Salaries display in their original units.

## Applications and reminders

Select a job and choose **Track / notes**. Save status, favorite, notes, application date,
recruiter name/contact, interview date/time and follow-up date. Applied status fills an empty
application date with today. Dates use `YYYY-MM-DD`; interviews use `YYYY-MM-DD HH:MM`
in the computer's local time. The view menu includes follow-ups due and upcoming interviews.

Notifications remind you of deadlines within 3 days, interviews today/tomorrow, and follow-ups
due or overdue. Each reminder is delivered at most once per local day; changing its date resets
it. Archived and rejected applications are excluded. Clear completed follow-up dates to stop
their reminders. CSV export includes the new tracking and verification fields.

## Background searches and notifications

Save a search with an interval of at least 1 hour, then open **Background & notifications** and
choose **Enable background mode**. This registers a task for the current Windows user and starts
it immediately. The task checks every 5 minutes and at sign-in. It works with Studio closed,
while the user is signed in and the computer is awake. Internet is required for searches and
verification. It does not wake the computer. Missed searches run once when next available.

An OS file lock and atomic schedule claims prevent overlapping searches. Logs and history
include failures; failed source tasks remain available through **Retry failed sources**.
Windows notification/Focus settings can suppress toast display. If native delivery fails while
Studio is open, Studio shows a local alert. Disable background mode to stop and remove this
project's scheduled task. The dialog shows the last run and offers a test notification and
automatic listing-check toggle.

```powershell
& .\.venv\Scripts\python.exe .\jobspy_background.py --enable
& .\.venv\Scripts\python.exe .\jobspy_background.py --status
& .\.venv\Scripts\python.exe .\jobspy_background.py --disable
```

## Listing checks and deadlines

Use **Check posting** for a selected job, or **Check visible postings (25)** for the first 25
jobs in the current filtered/sorted view. Checks are paced and stop on HTTP 403/429 responses.
Background mode optionally checks up to 5 listings per run after 7 days without verification.

Each listing shows **Open**, **Closed** or **Unknown**, last check time and a reason. A 404/410,
explicit closure message or matching structured posting with a past `validThrough` date confirms
closure. Blocks, CAPTCHA pages, failures, generic redirects and unverifiable content remain
unknown. Open means matching structured posting data was found; review the original before
applying. Source deadlines are extracted during import or verification. Manually entered dates
take precedence over source updates.

RSS/Atom and pages containing JobPosting JSON-LD can be imported under **Sources & imports**.
JSON-LD imports retain deadlines, remote labels and base salary fields. Other pages can be
imported using CSV. Board coverage varies; sources may block requests or return no results.

## Backup and restore

**Backup & restore** saves the entire SQLite workspace: jobs and URL aliases, tracking, notes,
CV text, settings, saved searches, history and reminders. This is more complete than CSV export.
Automatic snapshots live under `data/backups`; the latest 7 daily snapshots from days of use
are retained. Copy backups to a separate drive for protection against disk loss.

Restore validates the database, shows the number of jobs for confirmation, and creates a
`pre-restore-...sqlite3` safety copy first. It replaces the workspace rather than merging it.
Finish active operations before restoring. Backups contain CV text and contact details.
Restoring background preferences does not register a Windows task on another computer;
enable background mode there separately.

The database is `data/studio.sqlite3`. Version 1 databases migrate automatically, preserving notes
and manually entered deadlines. Original CSV files remain untouched. Logs rotate in `data/studio.log`.

## Verification

Tests use temporary databases and mocked responses without live board requests:

```powershell
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
& .\.venv\Scripts\python.exe .\tests\smoke_ui.py
& .\.venv\Scripts\python.exe .\tests\smoke_ui.py --preview
```

Tests cover deduplication, tracking preservation, migration, backup/restore, reminders, locking,
filters, imports, availability, cancellation, retries, cooldowns and background scheduling.
The Tk smoke check opens the new dialogs using sample data without changing the main workspace.

References: [Windows Task Scheduler](https://learn.microsoft.com/en-us/windows-server/administration/windows-commands/schtasks-create)
and [JobPosting structured data](https://developers.google.com/search/docs/appearance/structured-data/job-posting).

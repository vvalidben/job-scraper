"""Offline Tk smoke check. --preview leaves sample data open for inspection."""
import argparse
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from jobspy_services import Store
from jobspy_workspace import WorkspaceApp


class PreviewWorkspace(WorkspaceApp):
    def load_previous(self):
        self.reload_jobs()
        self.status.set('Feature preview using temporary sample data; no live searches scheduled.')

    def schedule_tick(self):
        pass


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true')
    args = parser.parse_args()
    with tempfile.TemporaryDirectory(prefix='studio-ui-') as directory:
        database = Path(directory) / 'preview.sqlite3'
        store = Store(database)
        store.set('notifications', False)
        store.upsert([
            dict(title='SOC Analyst Internship', company='Atlas Security', location='Rabat',
                 job_url='https://example.com/1', site='sample', date_posted='2026-11-13',
                 description='Python, Linux and SIEM. Hybrid internship with incident response training.',
                 work_mode='Hybrid', min_amount='5000', max_amount='7000', currency='MAD', interval='monthly', valid_through='2026-12-01'),
            dict(title='Cybersecurity Intern', company='Example Labs', location='Casablanca',
                 job_url='https://example.com/2', site='sample', date_posted='2026-11-12',
                 description='Remote internship. Networking, Python, Nmap.', is_remote=True),
        ])
        app = PreviewWorkspace(database)
        app.title('JobSpy Studio - Feature Preview')
        try:
            app.update()
            app.load_previous()
            app.update()
            assert len(app.visible) == 2
            app.filters = {'work_mode': 'Remote'}
            app.render()
            assert len(app.visible) == 1
            app.filters = {}
            app.render()
            for method in (app.filters_dialog, app.tracker_dialog, app.backup_dialog, app.background_dialog):
                before = set(app.winfo_children())
                method()
                app.update()
                dialogs = set(app.winfo_children()) - before
                assert dialogs, method.__name__
                for dialog in dialogs:
                    dialog.destroy()
            print('Tk smoke check passed: startup, filtering, tracker, backup and background dialogs.', flush=True)
            if args.preview:
                app.mainloop()
        finally:
            try:
                if app.winfo_exists():
                    app.close()
            except Exception:
                pass


if __name__ == '__main__':
    main()

import os
from pathlib import Path
import shutil
import subprocess


class Cancelled(Exception):
    pass


class Prompt:
    def __init__(self, no_gui=False):
        self.gui = not no_gui and bool(os.environ.get('DISPLAY') or os.environ.get('WAYLAND_DISPLAY'))
        self.kdialog = shutil.which('kdialog') if self.gui else None
        self.zenity = shutil.which('zenity') if self.gui else None

    def choose_vendor(self, url):
        text = 'HT1008_Software.zip was not found in the local folders.\n\nLocate your existing ZIP, or download it to the build working folder.\n\n' + url
        if self.kdialog:
            result = subprocess.run([self.kdialog, '--title', 'TraceForge source builder', '--yesnocancel', text,
                                     '--yes-label', 'Locate ZIP…', '--no-label', 'Download', '--cancel-label', 'Cancel'])
            if result.returncode == 0:
                return self.locate_zip()
            if result.returncode == 1:
                return 'download'
            raise Cancelled('Build cancelled; no vendor ZIP selected.')
        if self.zenity:
            result = subprocess.run([self.zenity, '--list', '--title=TraceForge source builder', '--text='+text,
                                     '--column=Action', 'Locate ZIP', 'Download', '--width=620', '--height=320'],
                                    capture_output=True, text=True)
            if result.returncode != 0:
                raise Cancelled('Build cancelled.')
            return self.locate_zip() if result.stdout.strip() == 'Locate ZIP' else 'download'
        if self.gui:
            try:
                import tkinter as tk
            except ImportError:
                pass
            else:
                root = tk.Tk()
                root.title('TraceForge source builder')
                choice = []
                tk.Label(root, text=text, wraplength=580, padx=20, pady=20).pack()
                def select(value):
                    choice.append(value)
                    root.destroy()
                buttons = tk.Frame(root)
                buttons.pack(pady=15)
                for label, value in [('Locate ZIP…', 'locate'), ('Download', 'download'), ('Cancel', 'cancel')]:
                    tk.Button(buttons, text=label, command=lambda v=value: select(v)).pack(side='left', padx=8)
                root.mainloop()
                if not choice or choice[0] == 'cancel':
                    raise Cancelled('Build cancelled.')
                return self.locate_zip() if choice[0] == 'locate' else 'download'
        print(text, flush=True)
        choice = input('[L]ocate ZIP / [D]ownload / [C]ancel: ').strip().lower()
        if choice == 'd':
            return 'download'
        if choice == 'l':
            return self.locate_zip()
        raise Cancelled('Build cancelled.')

    def locate_zip(self):
        if self.kdialog:
            result = subprocess.run([self.kdialog, '--getopenfilename', str(Path.cwd()), '*.zip|ZIP archives',
                                     '--title', 'Locate HT1008_Software.zip'], capture_output=True, text=True)
            value = result.stdout.strip() if result.returncode == 0 else ''
        elif self.zenity:
            result = subprocess.run([self.zenity, '--file-selection', '--title=Locate HT1008_Software.zip',
                                     '--file-filter=ZIP archives | *.zip'], capture_output=True, text=True)
            value = result.stdout.strip() if result.returncode == 0 else ''
        elif self.gui:
            try:
                import tkinter as tk
                from tkinter.filedialog import askopenfilename
                root = tk.Tk(); root.withdraw()
                value = askopenfilename(title='Locate HT1008_Software.zip', filetypes=[('ZIP archives', '*.zip')])
                root.destroy()
            except ImportError:
                value = input('ZIP path: ').strip()
        else:
            value = input('ZIP path: ').strip()
        if not value:
            raise Cancelled('No ZIP selected.')
        path = Path(value).expanduser().resolve()
        if not path.is_file():
            raise ValueError(f'ZIP not found: {path}')
        return path

    def confirm_install(self, commands):
        text = 'The build needs these system packages. Accept opens the normal sudo prompt in the build terminal.\n\n' + '\n'.join(commands)
        if self.kdialog:
            return subprocess.run([self.kdialog, '--yesno', text, '--yes-label', 'Install', '--no-label', 'Cancel']).returncode == 0
        if self.zenity:
            return subprocess.run([self.zenity, '--question', '--text='+text, '--ok-label=Install', '--cancel-label=Cancel']).returncode == 0
        print(text, flush=True)
        return input('Install required packages? [y/N] ').strip().lower() == 'y'

    def notify(self, text, error=False):
        if self.kdialog:
            subprocess.run([self.kdialog, '--error' if error else '--msgbox', text, '--title', 'TraceForge source builder'])
        elif self.zenity:
            subprocess.run([self.zenity, '--error' if error else '--info', '--text='+text, '--title=TraceForge source builder'])

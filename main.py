#!/usr/bin/env python3
"""
Deja-Dup Backup Monitor
A GTK4/Libadwaita status monitor for Deja-Dup backups (restic backend).
"""

import gi
gi.require_version('Gtk', '4.0')
gi.require_version('Adw', '1')

from gi.repository import Gtk, GLib, Adw, Pango

import datetime
import json
import shlex
import subprocess
from pathlib import Path

LOG_FILE = Path.home() / '.cache' / 'deja-dup' / 'restic.log'
POLL_INTERVAL_MS = 1000


# ── Helpers ──────────────────────────────────────────────────────────────────

def format_bytes(n: float) -> str:
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if abs(n) < 1024:
            return f"{n:.1f} {unit}"
        n /= 1024
    return f"{n:.1f} PB"


def format_duration(seconds: float) -> str:
    if seconds < 0:
        return "?"
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    if h > 0:
        return f"{h}h {m}m {s}s"
    elif m > 0:
        return f"{m}m {s}s"
    return f"{s}s"


def get_restic_proc_info() -> dict | None:
    """Return {'subcommand': str, 'repo': str, 'pid': int} for the running
    restic binary (not the bash wrapper), or None."""
    try:
        result = subprocess.run(
            ['pgrep', '-fa', 'restic'],
            capture_output=True, text=True
        )
        if result.returncode != 0:
            return None
        for line in result.stdout.splitlines():
            if '--repo' not in line:
                continue
            try:
                parts = shlex.split(line)
            except ValueError:
                parts = line.split()
            # Extract PID
            pid = None
            if parts and parts[0].isdigit():
                pid = int(parts[0])
                parts = parts[1:]
            # Only match when the executable itself is restic (skip bash wrapper)
            if not parts or not parts[0].endswith('restic'):
                continue
            args = parts[1:]
            repo = None
            subcommand = None
            i = 0
            while i < len(args):
                if args[i] == '--repo' and i + 1 < len(args):
                    repo = args[i + 1]; i += 2
                elif args[i].startswith('--repo='):
                    repo = args[i][7:]; i += 1
                elif not args[i].startswith('-') and subcommand is None:
                    subcommand = args[i]; i += 1
                else:
                    i += 1
            if repo:
                return {'subcommand': subcommand or 'unknown', 'repo': repo, 'pid': pid}
    except Exception:
        pass
    return None


def get_process_state(pid: int) -> str:
    """Return the one-letter process state from /proc/<pid>/status, e.g. 'T', 'S', 'R'."""
    try:
        with open(f'/proc/{pid}/status') as f:
            for line in f:
                if line.startswith('State:'):
                    return line.split()[1]
    except OSError:
        pass
    return ''


def get_lock_files(repo: str) -> list[Path]:
    """Return list of lock file Paths in the restic repository."""
    try:
        return list((Path(repo) / 'locks').iterdir())
    except OSError:
        return []


def is_backup_running() -> bool:
    info = get_restic_proc_info()
    return info is not None


# ── Application ───────────────────────────────────────────────────────────────

class DejaMonitor(Adw.Application):
    def __init__(self):
        super().__init__(application_id='dev.gerry.deja-dup-monitor')
        self.connect('activate', self._on_activate)

    def _on_activate(self, _app):
        win = MonitorWindow(application=self)
        win.present()


# ── Window ────────────────────────────────────────────────────────────────────

class MonitorWindow(Adw.ApplicationWindow):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.set_title("Deja-Dup Monitor")
        self.set_default_size(520, 500)

        # State
        self._log_pos: int = 0
        self._was_running: bool = False
        self._is_paused: bool = False
        self._prev_bytes: float = 0
        self._prev_elapsed: float = 0  # restic-reported seconds_elapsed

        self._build_ui()
        GLib.timeout_add(POLL_INTERVAL_MS, self._poll)
        self._poll()

    # ── UI Construction ───────────────────────────────────────────────────────

    def _build_ui(self):
        toolbar_view = Adw.ToolbarView()
        self.set_content(toolbar_view)

        # Header
        header = Adw.HeaderBar()
        toolbar_view.add_top_bar(header)

        refresh_btn = Gtk.Button(icon_name="view-refresh-symbolic")
        refresh_btn.set_tooltip_text("Aktualisieren")
        refresh_btn.connect("clicked", lambda _: self._poll())
        header.pack_end(refresh_btn)

        # Start / Pause button
        self._action_btn = Gtk.Button()
        self._action_btn.set_icon_name("media-playback-start-symbolic")
        self._action_btn.set_tooltip_text("Backup starten")
        self._action_btn.connect("clicked", self._toggle_backup)
        self._action_btn.add_css_class("suggested-action")
        header.pack_start(self._action_btn)

        # Lock warning banner
        self._lock_banner = Adw.Banner()
        self._lock_banner.set_title("Repository ist gesperrt (stale locks)")
        self._lock_banner.set_button_label("Locks löschen & neu starten")
        self._lock_banner.set_revealed(False)
        self._lock_banner.connect("button-clicked", self._clear_locks)
        toolbar_view.add_top_bar(self._lock_banner)

        # Toast overlay wraps the scroll
        self._toast_overlay = Adw.ToastOverlay()
        toolbar_view.set_content(self._toast_overlay)

        # Scroll container
        scroll = Gtk.ScrolledWindow(vexpand=True)
        scroll.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._toast_overlay.set_child(scroll)

        clamp = Adw.Clamp()
        clamp.set_maximum_size(600)
        clamp.set_margin_top(24)
        clamp.set_margin_bottom(24)
        clamp.set_margin_start(16)
        clamp.set_margin_end(16)
        scroll.set_child(clamp)

        page_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=20)
        clamp.set_child(page_box)

        # ── Status Card ───────────────────────────────────────────────────────
        status_group = Adw.PreferencesGroup()
        status_group.set_title("Status")
        page_box.append(status_group)

        status_row = Adw.ActionRow()
        status_row.set_title("Backup")
        self._status_row = status_row

        self._status_icon = Gtk.Image.new_from_icon_name("emblem-synchronizing-symbolic")
        self._status_icon.set_pixel_size(20)
        status_row.add_suffix(self._status_icon)

        self._status_badge = Gtk.Label(label="…")
        self._status_badge.add_css_class("caption")
        status_row.add_suffix(self._status_badge)

        status_group.add(status_row)

        # ── Progress Card ─────────────────────────────────────────────────────
        progress_group = Adw.PreferencesGroup()
        progress_group.set_title("Fortschritt")
        page_box.append(progress_group)

        progress_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        progress_box.set_margin_top(12)
        progress_box.set_margin_bottom(12)
        progress_box.set_margin_start(16)
        progress_box.set_margin_end(16)

        prog_row = Adw.PreferencesRow()
        prog_row.set_child(progress_box)
        progress_group.add(prog_row)

        pct_box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        pct_box.set_margin_bottom(4)
        self._pct_label = Gtk.Label(label="—")
        self._pct_label.set_hexpand(True)
        self._pct_label.set_halign(Gtk.Align.START)
        self._pct_label.add_css_class("heading")

        self._eta_badge = Gtk.Label(label="")
        self._eta_badge.add_css_class("caption")
        self._eta_badge.add_css_class("dim-label")
        self._eta_badge.set_halign(Gtk.Align.END)

        pct_box.append(self._pct_label)
        pct_box.append(self._eta_badge)
        progress_box.append(pct_box)

        self._progress_bar = Gtk.ProgressBar()
        self._progress_bar.set_show_text(False)
        progress_box.append(self._progress_bar)

        # ── Stats Card ────────────────────────────────────────────────────────
        stats_group = Adw.PreferencesGroup()
        stats_group.set_title("Statistiken")
        page_box.append(stats_group)

        self._stat_rows = {}
        stats_def = [
            ("files",   "Dateien",          "document-multiple-symbolic"),
            ("bytes",   "Übertragen",        "drive-harddisk-symbolic"),
            ("speed",   "Geschwindigkeit",   "network-transmit-symbolic"),
            ("elapsed", "Laufzeit",          "alarm-symbolic"),
        ]
        for key, title, icon in stats_def:
            row = Adw.ActionRow()
            row.set_title(title)
            row.set_icon_name(icon)
            val_lbl = Gtk.Label(label="—")
            val_lbl.add_css_class("body")
            val_lbl.set_selectable(True)
            row.add_suffix(val_lbl)
            stats_group.add(row)
            self._stat_rows[key] = val_lbl

        # ── Current File Card ─────────────────────────────────────────────────
        file_group = Adw.PreferencesGroup()
        file_group.set_title("Aktuelle Datei")
        page_box.append(file_group)

        file_row = Adw.ActionRow()
        file_row.set_subtitle_selectable(True)
        file_row.set_title("Pfad")
        self._current_file_row = file_row
        file_group.add(file_row)

        # ── Summary Card ──────────────────────────────────────────────────────
        self._summary_group = Adw.PreferencesGroup()
        self._summary_group.set_title("Letztes Ergebnis")
        self._summary_group.set_visible(False)
        page_box.append(self._summary_group)

        self._summary_row = Adw.ActionRow()
        self._summary_row.set_subtitle_selectable(True)
        self._summary_group.add(self._summary_row)

    # ── Polling & Log Reading ─────────────────────────────────────────────────

    def _poll(self) -> bool:
        info = get_restic_proc_info()
        running = info is not None
        stuck_on_unlock = (
            info is not None
            and info.get('subcommand') == 'unlock'
            and bool(get_lock_files(info['repo']))
        )
        paused = (
            info is not None
            and info.get('pid') is not None
            and get_process_state(info['pid']) == 'T'
        )
        self._is_paused = paused

        if stuck_on_unlock:
            self._set_locked()
            self._lock_banner.set_revealed(True)
            self._lock_banner._repo = info['repo']
            self._action_btn.set_sensitive(False)
        else:
            self._lock_banner.set_revealed(False)
            self._action_btn.set_sensitive(True)
            if paused:
                self._set_paused()
                self._action_btn.set_icon_name("media-playback-start-symbolic")
                self._action_btn.set_tooltip_text("Backup fortsetzen")
                self._action_btn.remove_css_class("destructive-action")
                self._action_btn.add_css_class("suggested-action")
            elif running:
                self._set_running()
                self._action_btn.set_icon_name("media-playback-pause-symbolic")
                self._action_btn.set_tooltip_text("Backup anhalten")
                self._action_btn.remove_css_class("suggested-action")
                self._action_btn.add_css_class("destructive-action")
            else:
                self._action_btn.set_icon_name("media-playback-start-symbolic")
                self._action_btn.set_tooltip_text("Backup starten")
                self._action_btn.remove_css_class("destructive-action")
                self._action_btn.add_css_class("suggested-action")
                if self._was_running:
                    self._set_idle()
                    self._progress_bar.set_fraction(1.0)
                    self._pct_label.set_text("100 %")
                    self._eta_badge.set_text("")
                else:
                    self._set_idle()

        self._was_running = running
        self._read_log()

        if running and not stuck_on_unlock and not paused and self._prev_bytes == 0:
            self._progress_bar.pulse()

        return True  # keep timer alive

    def _read_log(self):
        if not LOG_FILE.exists():
            self._log_pos = 0
            self._prev_bytes = 0
            self._prev_time = 0
            return
        try:
            with open(LOG_FILE, 'r', errors='replace') as f:
                f.seek(self._log_pos)
                lines = f.readlines()
                self._log_pos = f.tell()
        except OSError:
            return

        for line in lines:
            line = line.strip()
            if line.startswith('{'):
                try:
                    self._handle_json(json.loads(line))
                except json.JSONDecodeError:
                    pass

    def _handle_json(self, data: dict):
        msg_type = data.get('message_type', '')

        if msg_type == 'status':
            pct = data.get('percent_done', 0.0)
            self._progress_bar.set_fraction(min(pct, 1.0))
            self._pct_label.set_markup(f"<b>{pct * 100:.1f} %</b>")

            remaining = data.get('seconds_remaining', -1)
            if remaining >= 0:
                eta_abs = datetime.datetime.now() + datetime.timedelta(seconds=remaining)
                self._eta_badge.set_text(
                    f"noch {format_duration(remaining)}  ·  fertig ~{eta_abs.strftime('%H:%M')}"
                )
            else:
                self._eta_badge.set_text("")

            files_done = data.get('files_done', 0)
            total_files = data.get('total_files', 0)
            self._stat_rows['files'].set_text(f"{files_done:,} / {total_files:,}")

            bytes_done = data.get('bytes_done', 0)
            total_bytes = data.get('total_bytes', 0)
            self._stat_rows['bytes'].set_text(
                f"{format_bytes(bytes_done)} / {format_bytes(total_bytes)}"
            )

            elapsed = data.get('seconds_elapsed', 0)
            self._stat_rows['elapsed'].set_text(format_duration(elapsed))

            # Speed from restic's own elapsed-time deltas (avoids wall-clock issues)
            delta_elapsed = elapsed - self._prev_elapsed
            if self._prev_elapsed > 0 and delta_elapsed > 0:
                speed = (bytes_done - self._prev_bytes) / delta_elapsed
                self._stat_rows['speed'].set_text(f"{format_bytes(max(speed, 0))}/s")
            self._prev_bytes = bytes_done
            self._prev_elapsed = elapsed

            cur_files = data.get('current_files', [])
            if cur_files:
                self._current_file_row.set_subtitle(
                    GLib.markup_escape_text(cur_files[-1])
                )

        elif msg_type == 'summary':
            snap_id = data.get('snapshot_id', '')
            new_f = data.get('files_new', 0)
            changed_f = data.get('files_changed', 0)
            added = data.get('data_added', 0)
            duration = data.get('total_duration', 0)

            self._summary_row.set_title(
                f"Snapshot {snap_id[:12] if snap_id else '?'}"
            )
            self._summary_row.set_subtitle(GLib.markup_escape_text(
                f"Fertig in {format_duration(duration)}  ·  "
                f"Neu: {new_f}  ·  Ge\u00e4ndert: {changed_f}  ·  "
                f"Hinzugef\u00fcgt: {format_bytes(added)}"
            ))
            self._summary_group.set_visible(True)
            self._set_idle()
            self._prev_bytes = 0
            self._prev_elapsed = 0

        elif msg_type == 'exit_error':
            msg = data.get('message', 'Unbekannter Fehler')
            self._set_error(msg)

    # ── Status Helpers ─────────────────────────────────────────────────

    def _set_running(self):
        self._status_row.set_subtitle("Sicherung wird erstellt …")
        self._status_icon.set_from_icon_name("media-playback-start-symbolic")
        self._status_badge.set_markup('<span color="#3584e4">Läuft</span>')

    def _set_idle(self):
        self._status_row.set_subtitle("Kein aktives Backup")
        self._status_icon.set_from_icon_name("emblem-ok-symbolic")
        self._status_badge.set_markup('<span color="#26a269">Bereit</span>')

    def _set_paused(self):
        self._status_row.set_subtitle("Backup angehalten")
        self._status_icon.set_from_icon_name("media-playback-pause-symbolic")
        self._status_badge.set_markup('<span color="#9141ac">Pausiert</span>')

    def _set_locked(self):
        self._status_row.set_subtitle("Stale locks blockieren das Backup")
        self._status_icon.set_from_icon_name("changes-prevent-symbolic")
        self._status_badge.set_markup('<span color="#e5a50a">Gesperrt</span>')

    def _set_error(self, message: str):
        self._status_row.set_subtitle(GLib.markup_escape_text(message))
        self._status_icon.set_from_icon_name("dialog-error-symbolic")
        self._status_badge.set_markup('<span color="#e01b24">Fehler</span>')
        self._summary_row.set_title("Fehler")
        self._summary_row.set_subtitle(GLib.markup_escape_text(message))
        self._summary_group.set_visible(True)

    # ── Backup Control ────────────────────────────────────────────────

    def _toggle_backup(self, _btn):
        info = get_restic_proc_info()
        if info is None:
            # Idle → Start
            subprocess.Popen(
                ['deja-dup', '--backup'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        elif self._is_paused:
            # Paused → Resume (SIGCONT on restic + its children)
            pid = info.get('pid')
            if pid:
                subprocess.run(['kill', '-CONT', str(pid)], capture_output=True)
                subprocess.run(['pkill', '-CONT', '-P', str(pid)], capture_output=True)
        else:
            # Running → Pause (SIGSTOP)
            pid = info.get('pid')
            if pid:
                subprocess.run(['kill', '-STOP', str(pid)], capture_output=True)
        GLib.timeout_add(200, lambda: self._poll() and False)

    # ── Lock Management

    def _clear_locks(self, _banner):
        repo = getattr(self._lock_banner, '_repo', None)
        if not repo:
            return

        # Kill stuck restic process(es)
        subprocess.run(['pkill', '-f', 'restic.*unlock'], capture_output=True)

        # Delete lock files
        locks = get_lock_files(repo)
        deleted = 0
        failed = 0
        for lock in locks:
            try:
                lock.unlink()
                deleted += 1
            except OSError:
                failed += 1

        # Hide banner
        self._lock_banner.set_revealed(False)

        # Feedback toast
        if failed == 0:
            msg = f"{deleted} Lock(s) gelöscht — starte Backup neu …"
        else:
            msg = f"{deleted} gelöscht, {failed} fehlgeschlagen"
        toast = Adw.Toast.new(msg)
        toast.set_timeout(4)
        self._toast_overlay.add_toast(toast)

        # Restart backup after a short delay
        if deleted > 0 and failed == 0:
            GLib.timeout_add(800, self._restart_backup)

    def _restart_backup(self) -> bool:
        try:
            subprocess.Popen(
                ['deja-dup', '--backup'],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL
            )
        except Exception:
            pass
        return False  # don't repeat


# ── Entry Point ───────────────────────────────────────────────────────────────

if __name__ == '__main__':
    try:
        app = DejaMonitor()
        app.run()
    except KeyboardInterrupt:
        pass

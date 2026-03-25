# Deja-Dup Monitor

GTK4/Libadwaita App zur Echtzeitüberwachung von Deja-Dup Backups (restic Backend).

## Features

- Live-Fortschrittsbalken mit Prozentanzeige
- Übertragene Dateien & Bytes
- Instantane Geschwindigkeit
- ETA (verbleibende Zeit)
- Aktuell gesicherte Datei
- Backup-Zusammenfassung (Snapshot-ID, neue/geänderte Dateien)
- Fehleranzeige bei Problemen

## Starten

```bash
python3 main.py
# oder
./run.sh
```

## Abhängigkeiten

- Python 3.x
- GTK 4
- libadwaita ≥ 1.0
- PyGObject (`python-gobject`)

Auf Manjaro bereits vorhanden, wenn Deja-Dup installiert ist.

## Wie es funktioniert

Die App liest sekündlich `~/.cache/deja-dup/restic.log` und parst die
JSON-Statuszeilen, die restic mit `--json` ausgibt:

- `message_type: "status"` → Fortschritt, Dateien, Bytes, ETA
- `message_type: "summary"` → Abschlussbericht mit Snapshot-ID
- `message_type: "exit_error"` → Fehlerdarstellung

Zusätzlich wird per `pgrep` geprüft, ob ein restic-Prozess aktiv ist,
um den Idle/Running-Status unabhängig vom Log anzuzeigen.

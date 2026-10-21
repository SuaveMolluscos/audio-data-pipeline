#!/bin/sh
set -eu

PROJECT_DIR=${PROJECT_DIR:-/opt/music-crawler}
DB_PATH=${DB_PATH:-/var/lib/music-crawler/tracks.db}
VENV="$PROJECT_DIR/.venv/bin/python"

fail() {
    printf 'PREFLIGHT ERROR: %s\n' "$1" >&2
    exit 1
}

[ -x "$VENV" ] || fail "Python virtualenv missing: $VENV"
[ -f "$PROJECT_DIR/crawler.py" ] || fail "crawler.py missing"
[ -f "$PROJECT_DIR/db.py" ] || fail "db.py missing"
[ -f "$DB_PATH" ] || fail "database missing: $DB_PATH"

cd "$PROJECT_DIR"
"$VENV" -m py_compile analyzer.py crawler.py db.py main.py seeds.py monitor.py
"$VENV" -m unittest discover -s tests -q
"$VENV" -c "import db; ok, issues = db.check_integrity('$DB_PATH'); raise SystemExit(0 if ok else '\\n'.join(issues))"

if command -v systemctl >/dev/null 2>&1; then
    systemctl cat music-crawler.service >/dev/null 2>&1 || fail "systemd unit missing"
fi

printf 'PREFLIGHT OK: code, tests and SQLite integrity passed.\n'

"""
Central paths + tiny .env loader (no dependency).

Importing this module (a) resolves the project's data/ and secrets/ folders and
(b) loads key=value pairs from secrets/.env into the environment without
overwriting anything already set (so a real environment variable always wins).
Every entry-point script imports this first, so local runs "just work" while any
other setup can keep using real environment variables.

Layout (all under the project root, one level above this package):
    network-crm/
        crm/        ← this package (all application code)
        data/       ← crm.db, archive.db, *.npz, exclude_list.txt  (gitignored)
        secrets/    ← .env, gmail_credentials_*.json, gmail_token_*.json (gitignored, 700)
        static/     ← the desktop-app front-end

For packaged .app installs the data/ default moves out of the (replaceable)
bundle to ~/Library/Application Support/NetworkCRM — see _default_data_dir().
An explicit CRM_DATA_DIR env var overrides either default.
"""

from __future__ import annotations  # allow `str | None` hints on older Pythons (3.8)

import os
import stat
import sys

PKG_DIR = os.path.dirname(os.path.abspath(__file__))   # …/network-crm/crm
ROOT = os.path.dirname(PKG_DIR)                          # …/network-crm

# ~/Library/Application Support/NetworkCRM — the per-user data home for packaged
# .app installs (a bundle gets replaced/deleted on every update, so user data
# must live outside it). Dev checkouts keep using repo_root/data instead.
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/NetworkCRM")


def _running_from_app_bundle() -> bool:
    """True when this code is executing from inside a macOS .app bundle.

    packaging/appify.sh --embed copies crm/ to
    'Network CRM.app/Contents/Resources/app/crm', so this file's own path then
    contains a '.app/' component. A PyInstaller-style freeze would instead set
    sys.frozen — we honour that too so the detection keeps working if the
    packaging ever moves to a frozen binary.
    """
    if getattr(sys, "frozen", False):
        return True
    return (".app" + os.sep) in (PKG_DIR + os.sep)


def _default_data_dir() -> str:
    """Where crm.db / archive.db / *.npz live when nothing overrides it.

    Packaged .app runs default to ~/Library/Application Support/NetworkCRM;
    dev checkouts (and CI/tests) default to the repo's data/ folder. In both
    cases an explicit CRM_DATA_DIR env var still wins (set below) — the app
    launcher relies on that to point at its own support subfolder.
    """
    if _running_from_app_bundle():
        return APP_SUPPORT_DIR
    return os.path.join(ROOT, "data")

# secrets/ first (it holds .env). Override with a real env var only.
SECRETS_DIR = os.environ.get("CRM_SECRETS_DIR", os.path.join(ROOT, "secrets"))


def load_dotenv(path: str | None = None) -> None:
    path = path or os.path.join(SECRETS_DIR, ".env")
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_dotenv()

# data dir resolved after .env loads, so .env may set CRM_DATA_DIR if desired.
# Precedence: explicit CRM_DATA_DIR env var > packaged-.app default
# (~/Library/Application Support/NetworkCRM) > repo data/ (dev).
DATA_DIR = os.environ.get("CRM_DATA_DIR", _default_data_dir())
STATIC_DIR = os.path.join(ROOT, "static")
os.makedirs(DATA_DIR, exist_ok=True)  # created on first run if missing


def data_path(name: str) -> str:
    """Absolute path to a file inside data/."""
    return os.path.join(DATA_DIR, name)


def secret_path(name: str) -> str:
    """Absolute path to a file inside secrets/."""
    return os.path.join(SECRETS_DIR, name)


def harden(path: str) -> bool:
    """Make one credential file owner-only. Never raises; returns whether it
    worked.

    NC-150. Swallowing the OSError is right — the secret is already written by
    then, and failing the save would lose it. Going SILENT about it is not,
    because this file holds the Anthropic key and the calendar URLs, and
    "saved" with no further comment is indistinguishable from "saved, and left
    readable by every account on this computer".

    `open(path, "w")` leaves a file 0644 — readable by every account on the Mac.
    NC-133 fixed the copies of that line it found; NC-147 found another one in
    a different file with the same six lines, so the six lines now live here
    instead of being retyped.

    secrets/ is already 0700, which is what actually stops another account
    reading these today. This is the second wall, for the moment a file leaves
    that directory still carrying its mode: a `cp -p` into a backup, a drag onto
    the Desktop, a sync client's copy.
    """
    try:
        os.chmod(path, 0o600)
    except OSError:
        return False
    return True


DB_SUFFIXES = (".db", ".db-wal", ".db-shm")


def harden_databases() -> int:
    """Tighten the databases in DATA_DIR, and SQLite's two side files beside each,
    to 0600. Returns how many changed.

    NC-321. A source checkout keeps data/ inside the checkout, and a database
    made there under the usual umask came out 0644: readable by every account on
    the Mac that can list the folder. The packaged app's folder sits under
    ~/Library, which other accounts cannot enter, so this matters for running
    from source. The engine now starts with umask 077, which covers every file
    made from here on; this catches the ones made before. Only ever tightens,
    and only regular files directly inside DATA_DIR.
    """
    changed = 0
    try:
        names = os.listdir(DATA_DIR)
    except OSError:
        return 0
    for name in names:
        if not name.endswith(DB_SUFFIXES):
            continue
        path = os.path.join(DATA_DIR, name)
        try:
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                os.chmod(path, mode & 0o700)
                changed += 1
        except OSError:
            continue
    return changed


def harden_secrets_dir() -> int:
    """Tighten every regular file in secrets/ to 0600. Returns how many changed.

    The files this project writes are hardened where they are written. The ones
    it does NOT write are the reason this exists: a Google OAuth client file is
    downloaded from Google Cloud and dropped into secrets/ by hand, arriving
    0644 with a client id and secret in it, and no line of code ever touches it.

    Only ever tightens, never loosens, and only regular files directly inside
    the directory.
    """
    changed = 0
    try:
        names = os.listdir(SECRETS_DIR)
    except OSError:
        return 0
    for name in names:
        path = os.path.join(SECRETS_DIR, name)
        try:
            if not os.path.isfile(path) or os.path.islink(path):
                continue
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode & 0o077:
                os.chmod(path, mode & 0o700)
                changed += 1
        except OSError:
            continue
    return changed

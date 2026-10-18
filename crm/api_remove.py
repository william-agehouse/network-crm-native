"""Remove everything this app keeps on this Mac — NC-314.

A route-registry module: GET /api/remove/plan says what "Remove everything"
would delete and whether this copy of the app may do it; POST
/api/remove/everything does it.

WHY THIS EXISTS
Putting the app in the Trash removed almost nothing. The copying helper (the
small program that holds Full Disk Access) is copied OUT of the app into the
app's own folder by its installer, and macOS is told to start it at login and
every couple of minutes. Nothing inside the app owns it, so it kept running,
with that permission, after the app was gone. The people, messages, saved keys,
downloaded Python packages and logs stayed too, and nothing on screen said how
to remove any of it.

WHAT IT DOES, IN THIS ORDER
  1. Turns the copying helper off by running packaging/uninstall_imessage_sync.sh,
     found the same way Turn off automatic copying finds it (importers, never a
     path from the request). If that fails it stops there and deletes nothing: a
     folder deleted under a helper macOS still starts would leave a job behind
     that fails every two minutes.
  2. Deletes the app's own folder: people, notes, messages, photos, settings
     and saved keys, the private Python packages, and the helper's copies.
  3. Deletes the app's log folder.
  4. Answers with what only the person can do: drag the app to the Trash, and
     remove the helper's Full Disk Access entry in System Settings. macOS lets
     no program do either.
  5. Stops this engine a moment after answering, because its folders are gone.

WHAT IT REFUSES
  * Anything but the app's own data home. Every path comes from the app's own
    constants (config.APP_SUPPORT_DIR, config.DATA_DIR, config.SECRETS_DIR, and
    the helper's log in importers), and the data and settings folders this
    engine is using must both sit inside that home. A source checkout keeps
    data/ and secrets/ inside the checkout: that is a developer's copy, and it
    is never removed from here. So are the test suite's temporary folders.
  * A home or log folder not called NetworkCRM, so a constant that ever pointed
    one level too high could not take its parent with it.
  * Any request without the word the page asks the person to type.
"""

import os
import shutil
import signal
import subprocess
import threading
from http import HTTPStatus
from typing import Any, Dict, List

import config
import importers
from api_core import ApiError

ROUTES = {
    "GET": {"/api/remove/plan": "handle_remove_plan"},
    "POST": {"/api/remove/everything": "handle_remove_everything"},
}

# The word the person types to confirm. Sent to the page in the plan, so the
# page and this check cannot ask for different words.
CONFIRM_WORD = "remove"

# The name both folders this deletes must have. A guard, not a setting.
FOLDER_NAME = "NetworkCRM"

# How long this engine waits after answering before it stops, so the answer
# reaches the page first. Plumbing, not a setting.
STOP_AFTER_SECONDS = 2.0


def _norm(path: str) -> str:
    """Absolute and tidied, but NOT with links followed: a checkout whose data/
    is a link into the app's folder is still a checkout, and must be refused."""
    return os.path.normpath(os.path.abspath(os.path.expanduser(str(path or ""))))


def _inside(path: str, folder: str) -> bool:
    return path == folder or path.startswith(folder + os.sep)


def _home() -> str:
    return _norm(config.APP_SUPPORT_DIR)


def _logs() -> str:
    return _norm(os.path.dirname(importers.IMSG_HELPER_LOG))


def _refusal() -> str:
    """Why this copy of the app must not remove anything, or "" when it may."""
    home, logs = _home(), _logs()
    if os.path.basename(home) != FOLDER_NAME or os.path.basename(logs) != FOLDER_NAME:
        return ("This copy of the app keeps its files somewhere unexpected, so it "
                "removes nothing rather than guess.")
    for path in (_norm(config.DATA_DIR), _norm(config.SECRETS_DIR)):
        if not _inside(path, home):
            return ("This copy runs from a source folder, and its data and settings "
                    "live in that folder, not in the app's own. Nothing is removed "
                    "from here: delete that folder yourself if you mean to.")
    return ""


def _parts() -> List[Dict[str, str]]:
    """What goes, in the person's words, for the parts that exist now. All of
    it is inside the two folders deleted; the list is what the person reads."""
    copies = [p for p in (importers.IMSG_INBOX, importers.WA_INBOX, importers.AB_INBOX,
                          importers.CAL_INBOX) if os.path.lexists(p)]
    parts = [
        (config.DATA_DIR, "Everything the app knows: people, notes, saved messages and photos"),
        (config.SECRETS_DIR, "Your settings and saved keys"),
        (os.path.join(_home(), "venv"), "The extra Python packages the app downloaded"),
        (os.path.dirname(importers.IMSG_HELPER_BIN), "The copying helper"),
        (copies[0] if copies else "", "The copies automatic copying made"),
        (_logs(), "The app's logs"),
    ]
    return [{"name": name, "path": _norm(path)} for path, name in parts
            if path and os.path.lexists(path)]


def _app_bundle() -> str:
    """Where this copy of the app is, when it runs from one: the folder whose
    name ends in .app above this file."""
    if not config._running_from_app_bundle():
        return ""
    head, sep, _ = (config.PKG_DIR + os.sep).partition(".app" + os.sep)
    return head + ".app" if sep else ""


def _by_hand() -> List[str]:
    """The two steps no program is allowed to do."""
    where = _app_bundle()
    helper = os.path.basename(importers.IMSG_HELPER_BIN)
    return [
        "Quit the app, then drag Network CRM from your Applications folder to "
        "the Trash, with any other Network CRM app an older version left there, "
        "such as Quit Network CRM." + (f" This copy is at {where}." if where else ""),
        "Open System Settings, then Privacy & Security, then Full Disk Access. "
        f"If {helper} is in the list, select it and press the minus button. It "
        "does nothing once the helper is gone, but macOS keeps the entry until "
        "you remove it.",
    ]


def handle_remove_plan() -> Dict[str, Any]:
    """GET /api/remove/plan — what Remove everything would delete, and whether
    this copy may. Reads nothing but whether a few folders exist."""
    import api_fda
    why_not = _refusal()
    return {"allowed": not why_not, "why_not": why_not, "word": CONFIRM_WORD,
            "removes": [] if why_not else _parts(), "by_hand": _by_hand(),
            # How long the page should wait: the helper's uninstaller may run
            # up to the same limit Turn off automatic copying gives it.
            "waits": {"uninstall": api_fda.INSTALL_TIMEOUT}}


def _turn_off_helper() -> Dict[str, Any]:
    """Run the helper's own uninstaller, as Turn off automatic copying does.

    Run whenever this copy carries it, installed or not: it is safe with nothing
    installed, and it also stops a job macOS still has loaded after its file
    went. Without the script, a helper that is set up cannot be turned off from
    here, and that stops the whole removal."""
    script = importers.imsg_helper_uninstaller()
    if not script:
        if importers.imsg_helper_installed():
            return {"ok": False, "ran": False,
                    "output": "The copying helper is set up, but this copy of the "
                              "app has no way to turn it off."}
        return {"ok": True, "ran": False, "output": ""}
    import api_fda   # its time limit for the same script, so the two agree
    try:
        proc = subprocess.run(["/bin/bash", script], capture_output=True, text=True,
                              timeout=api_fda.INSTALL_TIMEOUT)
    except subprocess.TimeoutExpired:
        return {"ok": False, "ran": True,
                "output": f"Turning the helper off did not finish within "
                          f"{api_fda.INSTALL_TIMEOUT} seconds."}
    except OSError as exc:
        return {"ok": False, "ran": False, "output": f"Could not run it: {exc}"}
    output = "\n".join(p for p in (proc.stdout, proc.stderr) if p).strip()
    return {"ok": proc.returncode == 0, "ran": True, "output": output}


def _delete(folder: str) -> None:
    """Delete one folder, tolerating files that vanish or appear meanwhile (a
    background import can still be writing). Never follows a link out of it:
    shutil.rmtree removes a link inside the folder, not what it points at."""
    for _ in range(2):
        if not os.path.lexists(folder):
            return
        shutil.rmtree(folder, ignore_errors=True)


def _stop_engine_soon() -> None:
    """Stop this engine shortly after the answer is sent, as Quit would."""
    timer = threading.Timer(STOP_AFTER_SECONDS, os.kill, (os.getpid(), signal.SIGTERM))
    timer.daemon = True
    timer.start()


def handle_remove_everything(body: Dict[str, Any]) -> Dict[str, Any]:
    """POST /api/remove/everything {confirm} — turn the helper off, delete the
    app's folders, say what is left to do by hand, then stop."""
    word = str((body or {}).get("confirm") or "").strip().lower()
    if word != CONFIRM_WORD:
        raise ApiError(HTTPStatus.BAD_REQUEST,
                       f'Type the word "{CONFIRM_WORD}" to confirm. Nothing was removed.')
    why_not = _refusal()
    if why_not:
        raise ApiError(HTTPStatus.CONFLICT, why_not)

    helper = _turn_off_helper()
    if not helper["ok"]:
        return {"ok": False, "removed": [], "left": [], "helper": helper,
                "by_hand": [], "stopping": False,
                "says": "The copying helper could not be turned off, so nothing "
                        "was removed. What it said is below."}

    parts = _parts()
    folders = ((_home(), "The app's folder"), (_logs(), "The app's logs"))
    for folder, _name in folders:
        _delete(folder)
    left = [{"name": name, "path": folder} for folder, name in folders
            if os.path.lexists(folder)]
    _stop_engine_soon()
    return {
        "ok": not left,
        "removed": [p for p in parts if not os.path.lexists(p["path"])],
        "left": left,
        "helper": helper,
        "by_hand": _by_hand(),
        "stopping": True,
        "says": ("Everything this app kept on this Mac is gone, and the copying "
                 "helper is off." if not left else
                 "Most of it is gone, but some files could not be removed. They "
                 "are listed below, to delete in Finder."),
    }

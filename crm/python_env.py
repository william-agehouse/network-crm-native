"""Makes the private Python environment the app's optional parts run in.

The app carries its own Python (Contents/Resources/python), and the core runs on it
with nothing added. Ask, Gmail, Google photos and the LinkedIn messages import need a
few packages from the Python Package Index on top. This builds a private environment
for them (a venv, in ~/Library/Application Support/NetworkCRM/venv), installs exactly
the versions listed in requirements-lock.txt, and says which Python the engine should
run on. The app runs it before it starts its engine (native/Sources/Engine.swift):

    python3 crm/python_env.py check   --venv V --app-home H
        exit 0 and print the Python to use when nothing needs doing; exit 3 when
        setting up is needed (about a minute, and it needs the internet).
    python3 crm/python_env.py prepare --venv V --app-home H --data D --log L
        build or repair the environment, install, and print the Python to use as
        the last line. Never fails the start: the core still runs on the bare
        Python, and a file in the data folder says what did not install.

Until NC-330 (2026-10-17) the same steps were bash functions in the app's launcher
script (packaging/src/launcher.sh.tmpl). The app's window is a native program now and
starts the engine itself, so the steps moved here, where they can be tested directly.

Standard library only: this runs on the bundled Python before any package exists.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from typing import IO, Any, List, Optional

# The file the Settings page reads to say optional parts did not install
# (api_instance.deps_status). Absent is the healthy case.
MARKER_NAME = "deps_status.json"
# Inside the venv, so a rebuilt environment forgets both.
BASE_STAMP = ".nc_base"      # which Python built this environment
DEPS_STAMP = ".nc_deps_ok"   # which package list was last installed in full
# Internal: how long one install may run before it is stopped and retried on the
# next start. A first install on a slow line takes about a minute; this is only
# there so a stuck download cannot hold the app on "Setting up" for good.
PIP_LIMIT_SEC = 15 * 60
NEEDS_SETUP = 3


def venv_python(venv: str) -> str:
    return os.path.join(venv, "bin", "python3")


def base_origin(base: str) -> str:
    """"bundled" for the Python inside an app, "system" for any other.

    Why it matters: a venv points back at the Python that built it. One built from
    a Python that happens to have the same version as the bundled one would look
    current, and the app would keep running on it (NC-021 in miniature)."""
    real = os.path.realpath(base)
    return "bundled" if "/Contents/Resources/" in real else "system"


def base_id(base: str = sys.executable) -> str:
    """Which Python an environment belongs to: its version and where it came from.
    Not its path: the same app run from Applications and from a build folder carries
    the same Python, and keying on the path would reinstall on every switch."""
    try:
        out = subprocess.run([base, "-c", "import sys; print(sys.version.split()[0])"],
                             capture_output=True, text=True, timeout=30)
        version = out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError):
        version = "unknown"
    return f"{version} {base_origin(base)}"


def _read(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as fh:
            return fh.read().strip()
    except OSError:
        return ""


def requirements_files(app_home: str) -> List[str]:
    files = [os.path.join(app_home, "requirements.txt")]
    lock = os.path.join(app_home, "requirements-lock.txt")
    if os.path.isfile(lock):
        files.append(lock)
    return files


def requirements_hash(app_home: str) -> str:
    """Changes when the package list does (a new version of the app), so the install
    runs again then and not on every start. "" when there is no list."""
    files = requirements_files(app_home)
    if not os.path.isfile(files[0]):
        return ""
    digest = hashlib.sha256()
    for path in files:
        try:
            with open(path, "rb") as fh:
                digest.update(fh.read())
        except OSError:
            return ""
    return digest.hexdigest()


def runs(python: str) -> bool:
    """A venv is usable only if its Python starts. This one check catches a
    half-built venv from a crashed first run and a venv whose Python pointed into an
    app that has since been moved or deleted."""
    if not os.access(python, os.X_OK):
        return False
    try:
        return subprocess.run([python, "-c", "import sys"], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def is_ready(venv: str, app_home: str, base: str = sys.executable) -> bool:
    """True when prepare() would find nothing to do. Only reads."""
    want = requirements_hash(app_home)
    return (bool(want) and runs(venv_python(venv))
            and _read(os.path.join(venv, BASE_STAMP)) == base_id(base)
            and _read(os.path.join(venv, DEPS_STAMP)) == want)


def lock_has_fingerprints(app_home: str) -> bool:
    lock = os.path.join(app_home, "requirements-lock.txt")
    text = _read(lock)
    return "--hash=sha256:" in text


def install_command(python: str, app_home: str) -> List[str]:
    """pip, limited to the exact versions in requirements-lock.txt. With the
    packages' fingerprints in that file (NC-316), pip refuses any download whose
    contents differ from what was checked when the list was made, and takes only
    ready-built packages, so nothing is compiled on the person's Mac."""
    cmd = [python, "-m", "pip", "install", "--disable-pip-version-check", "-q"]
    lock = os.path.join(app_home, "requirements-lock.txt")
    if lock_has_fingerprints(app_home):
        return cmd + ["--require-hashes", "--only-binary=:all:", "-r", lock]
    cmd += ["-r", os.path.join(app_home, "requirements.txt")]
    if os.path.isfile(lock):
        cmd += ["-c", lock]
    return cmd


def clear_marker(data_dir: str) -> None:
    if not data_dir:
        return
    try:
        os.remove(os.path.join(data_dir, MARKER_NAME))
    except OSError:
        pass


def write_marker(data_dir: str, phase: str, message: str, *, app_home: str, venv: str,
                 log_file: str) -> None:
    if not data_dir:
        return
    os.makedirs(data_dir, exist_ok=True)
    marker = {
        "status": "error",
        "phase": phase,
        "message": message,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "requirements": os.path.join(app_home, "requirements.txt"),
        "log_file": log_file,
        "retry": ("Open Network CRM again while online and it tries once more by "
                  f"itself, or delete the folder {venv} to set it up from scratch."),
    }
    tmp = os.path.join(data_dir, MARKER_NAME + ".tmp")
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(marker, fh, indent=2)
    os.replace(tmp, os.path.join(data_dir, MARKER_NAME))


def prepare(venv: str, app_home: str, data_dir: str, log_file: str,
            base: str = sys.executable, log: Optional[IO[Any]] = None) -> str:
    """Build or repair the environment and install the packages. Returns the Python
    the engine should run on: the venv's, or the bare one when even the venv could
    not be made. Never raises for a failed install."""
    out: IO[Any] = log if log is not None else sys.stderr
    vpy = venv_python(venv)
    wanted_base = base_id(base)

    if os.path.isdir(venv) and not runs(vpy):
        shutil.rmtree(venv, ignore_errors=True)
    # A new Python in a new version of the app must be adopted, not left unused
    # behind an environment built from the old one (NC-021).
    if os.path.isdir(venv) and _read(os.path.join(venv, BASE_STAMP)) != wanted_base:
        shutil.rmtree(venv, ignore_errors=True)

    if not runs(vpy):
        made = subprocess.run([base, "-m", "venv", venv], stdout=out, stderr=out)
        if made.returncode != 0 or not runs(vpy):
            write_marker(data_dir, "venv_create",
                         "Could not make the Python environment for the optional parts. "
                         "The rest of the app works.",
                         app_home=app_home, venv=venv, log_file=log_file)
            return base
    with open(os.path.join(venv, BASE_STAMP), "w", encoding="utf-8") as fh:
        fh.write(wanted_base)

    want = requirements_hash(app_home)
    if not want:
        clear_marker(data_dir)
        return vpy
    if _read(os.path.join(venv, DEPS_STAMP)) == want:
        clear_marker(data_dir)
        return vpy

    try:
        done = subprocess.run(install_command(vpy, app_home), stdout=out, stderr=out,
                              timeout=PIP_LIMIT_SEC).returncode == 0
    except (OSError, subprocess.SubprocessError):
        done = False
    if done:
        with open(os.path.join(venv, DEPS_STAMP), "w", encoding="utf-8") as fh:
            fh.write(want)
        clear_marker(data_dir)
    else:
        # No stamp, so the next start tries again (an offline first start, say).
        write_marker(data_dir, "pip_install",
                     "The optional parts (Ask with AI, Gmail, Google photos, LinkedIn "
                     "messages) could not finish installing. The rest of the app works, "
                     "and it tries again the next time it opens.",
                     app_home=app_home, venv=venv, log_file=log_file)
    return vpy


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="crm/python_env.py")
    parser.add_argument("action", choices=("check", "prepare"))
    parser.add_argument("--venv", required=True)
    parser.add_argument("--app-home", required=True)
    parser.add_argument("--data", default="")
    parser.add_argument("--log", default="")
    args = parser.parse_args(argv)
    # NC-321: the environment and the marker open for this account only.
    os.umask(0o077)
    venv = os.path.abspath(os.path.expanduser(args.venv))
    app_home = os.path.abspath(os.path.expanduser(args.app_home))
    if args.action == "check":
        if is_ready(venv, app_home):
            print(venv_python(venv))
            return 0
        return NEEDS_SETUP
    log = None
    if args.log:
        os.makedirs(os.path.dirname(args.log), exist_ok=True)
        log = open(args.log, "a", encoding="utf-8")
    try:
        python = prepare(venv, app_home, args.data, args.log or "", log=log)
    finally:
        if log is not None:
            log.close()
    print(python)
    return 0


if __name__ == "__main__":
    sys.exit(main())

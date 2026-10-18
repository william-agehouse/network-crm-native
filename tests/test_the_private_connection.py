"""NC-330. The packaged app reaches its engine through a private file, not a port.

The window used to be a web page
served on 127.0.0.1:8765, which any browser on the Mac could also open. The app
now starts the engine as its own child with `--socket <file> --watch-stdin`, and
these tests start a real engine that way, on throwaway data, and check what that
promises:

- it answers through the file, and says so on /api/status;
- the file and its folder open for this account only;
- it listens on no network port at all;
- it stops when the app goes (its standard input closes) or is told to stop,
  and removes its file;
- a second engine never starts beside a live one;
- the import programs it started stop with it.
"""

import http.client
import json
import os
import shutil
import signal
import socket
import stat
import subprocess
import sys
import tempfile
import time
import unittest

import tests.helpers  # noqa: F401 — import order contract; see tests/helpers.py

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "crm", "app.py")
START_WAIT = 30


class _UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self._path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self._path)


def ask(path, route, method="GET", body=None):
    conn = _UnixConnection(path)
    try:
        headers = {"Content-Type": "application/json"} if body is not None else {}
        conn.request(method, route, body=None if body is None else json.dumps(body),
                     headers=headers)
        resp = conn.getresponse()
        return resp.status, json.loads(resp.read() or b"{}")
    finally:
        conn.close()


class _Engine:
    """One engine on throwaway data, started the way the app starts it."""

    def __init__(self, root):
        self.root = root
        self.sock = os.path.join(root, "run", "engine.sock")
        env = dict(os.environ)
        env.update({
            "CRM_DATA_DIR": os.path.join(root, "data"),
            "CRM_SECRETS_DIR": os.path.join(root, "secrets"),
            "NC_AUTOSYNC": "0",
            "NC_NO_BROWSER": "1",
            "PYTHONDONTWRITEBYTECODE": "1",
        })
        env.pop("APP_PORT", None)
        env.pop("ANTHROPIC_API_KEY", None)
        os.makedirs(env["CRM_DATA_DIR"])
        os.makedirs(env["CRM_SECRETS_DIR"])
        self.log = open(os.path.join(root, "engine.log"), "w+")
        self.proc = subprocess.Popen(
            [sys.executable, APP, "--socket", self.sock, "--watch-stdin"],
            cwd=REPO, env=env, stdin=subprocess.PIPE, stdout=self.log,
            stderr=subprocess.STDOUT)

    def wait_until_answering(self):
        deadline = time.time() + START_WAIT
        while time.time() < deadline:
            if self.proc.poll() is not None:
                raise AssertionError("the engine stopped while starting:\n" + self.output())
            if os.path.exists(self.sock):
                try:
                    return ask(self.sock, "/api/status")
                except OSError:
                    pass
            time.sleep(0.2)
        raise AssertionError("the engine did not answer in time:\n" + self.output())

    def output(self):
        self.log.flush()
        self.log.seek(0)
        return self.log.read()[-3000:]

    def wait_for_exit(self, seconds=15):
        try:
            return self.proc.wait(timeout=seconds)
        except subprocess.TimeoutExpired:
            return None

    def close(self):
        if self.proc.poll() is None:
            self.proc.kill()
            self.proc.wait(timeout=10)
        if self.proc.stdin and not self.proc.stdin.closed:
            self.proc.stdin.close()
        self.log.close()


@unittest.skipUnless(sys.platform == "darwin" or hasattr(socket, "AF_UNIX"),
                     "needs socket files")
class TestThePrivateConnection(unittest.TestCase):

    def setUp(self):
        # A short folder: a socket file's whole path must fit in about 100 bytes.
        self.root = tempfile.mkdtemp(prefix="ncsock-", dir="/tmp")
        self.addCleanup(shutil.rmtree, self.root, True)
        self.engine = _Engine(self.root)
        self.addCleanup(self.engine.close)

    def test_it_answers_through_the_file_and_says_so(self):
        status, body = self.engine.wait_until_answering()
        self.assertEqual(status, 200)
        self.assertTrue(body["running"])
        self.assertIs(body["private"], True)
        self.assertEqual(body["port"], 0)
        self.assertEqual(body["counts"]["total"], 0, "throwaway data, not anyone's")

    def test_the_file_opens_for_this_account_only(self):
        self.engine.wait_until_answering()
        self.assertEqual(stat.S_IMODE(os.stat(self.engine.sock).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.dirname(self.engine.sock)).st_mode), 0o700)

    @unittest.skipUnless(shutil.which("lsof"), "needs lsof")
    def test_it_listens_on_no_network_port(self):
        self.engine.wait_until_answering()
        out = subprocess.run(["lsof", "-nP", "-a", "-p", str(self.engine.proc.pid),
                              "-iTCP", "-sTCP:LISTEN"], capture_output=True, text=True)
        self.assertEqual(out.stdout.strip(), "",
                         "the engine opened a network port a browser could reach")

    def test_it_stops_when_the_app_goes(self):
        self.engine.wait_until_answering()
        self.engine.proc.stdin.close()     # what Cmd-Q, a crash or a force quit does
        self.assertIsNotNone(self.engine.wait_for_exit(),
                             "the engine kept running after the app went away")
        self.assertFalse(os.path.lexists(self.engine.sock), "it left its file behind")

    def test_it_stops_when_told_to(self):
        self.engine.wait_until_answering()
        self.engine.proc.send_signal(signal.SIGTERM)
        self.assertEqual(self.engine.wait_for_exit(), 0)
        self.assertFalse(os.path.lexists(self.engine.sock))

    def test_a_second_engine_never_starts_beside_a_live_one(self):
        self.engine.wait_until_answering()
        second = subprocess.run(
            [sys.executable, APP, "--socket", self.engine.sock],
            cwd=REPO, capture_output=True, text=True, timeout=60,
            env=dict(os.environ, CRM_DATA_DIR=os.path.join(self.root, "data"),
                     CRM_SECRETS_DIR=os.path.join(self.root, "secrets"),
                     NC_AUTOSYNC="0", PYTHONDONTWRITEBYTECODE="1"))
        self.assertNotEqual(second.returncode, 0)
        self.assertIn("already running", second.stderr + second.stdout)
        status, _ = ask(self.engine.sock, "/api/status")
        self.assertEqual(status, 200, "the first engine must keep answering")


class TestALeftoverFileIsNotALiveEngine(unittest.TestCase):

    def test_a_dead_engines_file_is_replaced(self):
        root = tempfile.mkdtemp(prefix="ncsock-", dir="/tmp")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "run", "engine.sock")
        os.makedirs(os.path.dirname(path))
        dead = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        dead.bind(path)
        dead.close()                       # the file stays; nothing answers it
        engine = _Engine(root)
        self.addCleanup(engine.close)
        status, _ = engine.wait_until_answering()
        self.assertEqual(status, 200)

    def test_something_that_is_not_a_socket_is_left_alone(self):
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import app
        root = tempfile.mkdtemp(prefix="ncsock-", dir="/tmp")
        self.addCleanup(shutil.rmtree, root, True)
        path = os.path.join(root, "engine.sock")
        with open(path, "w") as fh:
            fh.write("someone's file")
        with self.assertRaises(SystemExit):
            app.open_private_socket(path)
        with open(path) as fh:
            self.assertEqual(fh.read(), "someone's file")


def _kill_quietly(pid):
    try:
        os.kill(pid, signal.SIGKILL)
    except OSError:
        pass


class TestTheProgramsItStartedStopWithIt(unittest.TestCase):

    def test_children_in_its_group_are_stopped(self):
        script = (
            "import os, subprocess, sys, time\n"
            f"sys.path.insert(0, {os.path.join(REPO, 'crm')!r})\n"
            "import app\n"
            "os.setpgrp()\n"
            "child = subprocess.Popen(['sleep', '60'], stdout=subprocess.DEVNULL,\n"
            "                         stderr=subprocess.DEVNULL)\n"
            "print(child.pid, flush=True)\n"
            "time.sleep(0.3)\n"
            "app._stop_children()\n"
            "time.sleep(0.5)\n"
        )
        root = tempfile.mkdtemp(prefix="ncgrp-")
        self.addCleanup(shutil.rmtree, root, True)
        out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                             timeout=60, env=dict(os.environ, CRM_DATA_DIR=root,
                                                  PYTHONDONTWRITEBYTECODE="1"))
        child = int(out.stdout.split()[0])
        self.addCleanup(_kill_quietly, child)
        time.sleep(0.3)
        try:
            os.kill(child, 0)
            alive = True
        except ProcessLookupError:
            alive = False
        except PermissionError:
            alive = True
        if alive:
            # A stopped child the engine did not reap is a zombie, not a live process.
            state = subprocess.run(["ps", "-o", "stat=", "-p", str(child)],
                                   capture_output=True, text=True).stdout.strip()
            alive = bool(state) and not state.startswith("Z")
        self.assertFalse(alive, "an import program kept running after the engine stopped")

    def test_it_never_signals_a_group_it_does_not_lead(self):
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import app
        if os.getpgrp() == os.getpid():
            self.skipTest("this test process leads its own group")
        # Must return without signalling the test runner's own group.
        app._stop_children()


if __name__ == "__main__":
    unittest.main()

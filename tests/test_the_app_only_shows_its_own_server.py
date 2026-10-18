"""NC-202. The app's window only ever shows its own engine.

The launcher used to ask one question of whatever held 127.0.0.1:8765: does it
answer? A stray copy of crm/app.py, started by hand from a scratch folder and
serving an empty data folder, can keep answering there indefinitely. Every launch
while it runs would open the window onto it: none of the person's contacts,
and none of the fixes made since.

Then the launcher started its server with a fingerprint of the code folder and
data folder it chose, /api/status reported that fingerprint back, and the window
opened only onto a server whose fingerprint matched. Those tests ran the launcher's
own shell functions against a stand-in server.

NC-330 (2026-10-17) removed the thing they were about. The engine is no longer a
server on a network address that anything can start or leave behind: the app starts
it as its own child, reaches it through a private file in a folder only this account
can open, and stops it when it quits. So the question "is the one answering mine?"
has no stranger to be asked about, and the launcher, its fingerprint comparison and
its 'refuse a foreign server' dialog are gone with the tests that ran them
(2026-10-17: TestTheLauncherKnowsItsOwnServer, seven tests, and the three launcher
wiring tests).

What is left to hold, and where it is held now:

  * the engine still reports the fingerprint it was started with on /api/status
    (run for real, below);
  * the app still starts its engine with that fingerprint, worked out the way the
    launcher worked it out, so the value an engine reports keeps its meaning across
    the switch (native/Sources/Engine.swift, read below);
  * the app shows only an engine it started itself: one already answering on its
    private file (another copy of the app) is refused, never shown or stopped;
  * the two ways to point the app at an engine somebody else started
    (NC_NATIVE_PORT, NC_NATIVE_SOCKET) can only be set from a shell, and a copy
    opened from Finder, which passes no environment, always starts its own.

tests/test_the_private_connection.py runs a real engine on its private file and
checks the file, the folder and the network port; this file does not repeat that.
"""

import os
import re
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_SWIFT = os.path.join(REPO, "native", "Sources", "Engine.swift")


def _engine_swift():
    with open(ENGINE_SWIFT, encoding="utf-8") as fh:
        return fh.read()


def _body_of(src, signature):
    """The text of one Swift function, from its signature to the closing brace that
    sits at the same indent as the signature."""
    start = src.index(signature)
    line_start = src.rfind("\n", 0, start) + 1
    indent = src[line_start:start]
    end = src.index("\n" + indent + "}\n", start)
    return src[start:end]


class TestTheServerReportsIt(Base):

    def test_status_reports_the_fingerprint_it_was_started_with(self):
        with mock.patch.dict(os.environ, {"NC_INSTANCE": "0123456789abcdef"}):
            self.assertEqual(app.handle_status(self.conn)["instance"], "0123456789abcdef")

    def test_a_server_started_by_hand_reports_none(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NC_INSTANCE", None)
            self.assertEqual(app.handle_status(self.conn)["instance"], "")


class TestTheAppStartsItsEngineWithTheFingerprint(unittest.TestCase):
    """Moved 2026-10-17 (NC-330) from the launcher's functions (status_url,
    instance_id, and the `nohup ... crm/app.py` line that exported NC_INSTANCE) to
    EngineLaunch.environment and EngineController.fingerprint in Engine.swift. Text
    checks: the program is Swift and is not run from this suite."""

    def setUp(self):
        self.swift = _engine_swift()

    def test_the_engine_is_started_with_it(self):
        env = _body_of(self.swift, "func environment(code: URL)")
        self.assertRegex(
            env, r'env\["NC_INSTANCE"\]\s*=\s*EngineController\.fingerprint\('
                 r'appHome: code\.path, dataFolder: dataFolder\.path\)',
            "the engine is started without its fingerprint, so /api/status could "
            "never say which code and data folder it belongs to")

    def test_the_fingerprint_is_sixteen_hex_characters_of_both_folders(self):
        """The recipe the launcher used, kept so the value /api/status reports means
        the same thing it did before the switch: the first 16 hex characters of the
        SHA-256 hash (a fixed-length fingerprint of some text) of
        "<code folder>\\n<data folder>\\n". A fingerprint of the two folders rather than
        the folders themselves, because a path with an accent comes back in the engine's
        reply as an escape sequence and would never compare as plain text."""
        fn = _body_of(self.swift, "nonisolated static func fingerprint(")
        self.assertIn('SHA256.hash(data: Data("\\(appHome)\\n\\(dataFolder)\\n".utf8))', fn)
        self.assertIn('String(format: "%02x"', fn)
        self.assertRegex(fn, r"\.prefix\(16\)")


class TestTheAppShowsOnlyAnEngineItStarted(unittest.TestCase):
    """The descendant of NC-202's rule. Before NC-330 the check was a fingerprint
    comparison against whatever answered on the port; now the app never attaches to
    an engine it did not start, so there is nothing to compare."""

    def setUp(self):
        self.swift = _engine_swift()

    def test_a_live_engine_already_on_the_file_is_refused_not_shown(self):
        """Another copy of the app is open on the same data. Its engine is not this
        app's to show or stop, and a second one must never open the same databases."""
        start = _body_of(self.swift, "private func startEngine(")
        refuse = start.index("if PrivateConnection.answers(launch.socketPath) {")
        run = start.index("try p.run()")
        self.assertLess(refuse, run, "the check comes after the app has already started its own")
        block = start[refuse:start.index("await Task.detached")]
        self.assertIn("state = .failed(", block)
        self.assertIn("return", block, "after refusing, the app goes on to start another beside it")
        self.assertNotIn("state = .ready(", block, "the app shows an engine it did not start")

    def test_attaching_to_an_engine_nobody_here_started_is_for_a_shell_only(self):
        """NC_NATIVE_PORT and NC_NATIVE_SOCKET let a developer point the window at the
        scratch engine the self-tests start. Only a shell sets them; Finder passes no
        environment, so an app opened from Finder always takes the last line."""
        fn = _body_of(self.swift, "static func fromEnvironment()")
        self.assertEqual(fn.count("launch: nil"), 2,
                         "a third way to attach without starting an engine appeared")
        for name in ("NC_NATIVE_PORT", "NC_NATIVE_SOCKET"):
            branch = fn[fn.index(f'env["{name}"]'):]
            self.assertIn("launch: nil", branch.split("}")[0], f"{name} no longer joins")
        self.assertTrue(fn.rstrip().endswith("launch: launch)"),
                        "the app no longer ends by starting its own engine")

    def test_joining_an_engine_is_only_reachable_when_there_is_nothing_to_start(self):
        run = _body_of(self.swift, "private func runConnect()")
        guard = run.index("guard let launch else {")
        join = run.index("await joinDevelopmentEngine()")
        self.assertLess(guard, join)
        self.assertIn("return", run[guard:run.index("// This app's engine has not answered yet")],
                      "joining falls through into starting an engine as well")
        self.assertEqual(len(re.findall(r"joinDevelopmentEngine\(\)", self.swift)), 2,
                         "joinDevelopmentEngine is called from somewhere other than runConnect")


if __name__ == "__main__":
    unittest.main()

"""NC-203. Opening the app after a rebuild shows the new build, not the old one.

NC-202 made the window open only onto this app's own server, recognised by a
fingerprint of its code and data folders. A rebuild leaves the bundle where it
was, so a server started before the rebuild still matched, and the window opened
onto the old code with nothing on screen to say so. A build in packaging/dist can
be rebuilt while its server is still running.

Then appify.sh stamped a build id into the bundle, the server reported the build it
loaded, and the launcher restarted its own server when that server was an older
build, using the same stop that "Quit Network CRM" used, so the restart could never
stop anything Quit would not.

NC-330 (2026-10-17) made that restart unnecessary. There is no server left over
from an earlier open to restart: the app starts its own engine, from its own bundle,
every time it opens, and stops it when it quits (or, by itself, when the app is gone
without saying so). The launcher, its restart check, the stop that found a server by
the process number in a file or by its port, and the "Quit Network CRM" app were
removed that day, with the tests that ran their shell functions (2026-10-17:
TestTheBuildIsRead, three tests; TestTheOldServerIsStopped, five; and four of the six
launcher wiring tests).

What is left to hold, and where it is held now:

  * appify.sh still stamps the build into the bundle, inside the seal, and the engine
    still reports it on /api/status (Settings shows it);
  * the app still hands that stamp to the engine it starts
    (native/Sources/Engine.swift: EngineController.buildID and EngineLaunch.environment);
  * the one older engine that can still be running, one an app from before NC-330
    left behind, is stopped once and only when its command line proves it is this
    app's engine, the same "never touch a stranger" rule the old stop had;
  * quitting stops only the engine this app started.

The Swift is read, not run: this suite does not compile or start the window.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ENGINE_SWIFT = os.path.join(REPO, "native", "Sources", "Engine.swift")
APPIFY = os.path.join(REPO, "packaging", "appify.sh")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _body_of(src, signature):
    """One Swift function, from its signature to the closing brace at the same indent."""
    start = src.index(signature)
    line_start = src.rfind("\n", 0, start) + 1
    indent = src[line_start:start]
    return src[start:src.index("\n" + indent + "}\n", start)]


class TestTheBuildIsStampedAndHandedOn(unittest.TestCase):

    def test_appify_stamps_the_build_before_it_signs(self):
        """The stamp is written into the bundle before the seal is made, so it is part
        of what the signature covers (and a rewritten stamp would show as a broken
        seal). Moved 2026-10-17: this used to check the stamp came before the launcher
        was written from its template."""
        src = _read(APPIFY)
        self.assertRegex(src, r'echo "\$BUILD_ID" > "\$APP_DIR/Contents/Resources/build_id\.txt"')
        self.assertLess(src.index("build_id.txt"),
                        src.index('codesign --force --sign - --identifier "$BUNDLE_ID" "$APP_DIR"'))
        self.assertRegex(src, r'BUILD_ID="\$\(date -u \+%Y%m%dT%H%M%SZ\)-')

    def test_the_app_hands_the_stamp_to_the_engine_it_starts(self):
        """Was: the launcher exported NC_BUILD on the line before `nohup crm/app.py`."""
        swift = _read(ENGINE_SWIFT)
        env = _body_of(swift, "func environment(code: URL)")
        self.assertRegex(env, r'env\["NC_BUILD"\]\s*=\s*EngineController\.buildID\(resources: resources\)')
        # The environment it was given never decides it: every NC_ variable the app
        # itself inherited is dropped before the engine's own are added.
        self.assertIn('key.hasPrefix("NC_")', env)
        self.assertLess(env.index('key.hasPrefix("NC_")'), env.index('env["NC_BUILD"]'))

    def test_the_stamp_is_read_from_the_file_and_kept_to_plain_characters(self):
        fn = _body_of(_read(ENGINE_SWIFT), "nonisolated static func buildID(")
        self.assertIn('resources.appending(path: "build_id.txt")', fn)
        self.assertIn('?? ""', fn, "a bundle with no stamp must give an empty build, not stop")
        for allowed in ('("A"..."Z")', '("a"..."z")', '("0"..."9")', '$0 == "-"'):
            self.assertIn(allowed, fn)


class TestAnOldEngineIsStoppedOnlyIfItIsOurs(unittest.TestCase):
    """Versions before NC-330 started the engine on 127.0.0.1:8765, left its process
    number in networkcrm.pid, and kept it running after the window closed. One still
    running would keep that address open and write to the same databases as the new
    app's own engine, so the app stops it, once.

    Was: the launcher's stop_our_server, run against stand-in servers: it stopped the
    server whose command line was crm/app.py and left alone a stranger on the port.
    That second half is the one that must survive: a process number from an old file
    can belong to anything by now."""

    def setUp(self):
        self.swift = _read(ENGINE_SWIFT)
        self.stop = _body_of(self.swift, "func stopEngineFromBeforeNC330()")

    def test_a_process_is_signalled_only_when_its_command_line_is_the_engine(self):
        guard = ('guard kill(pid, 0) == 0, Self.commandLine(of: pid).contains("crm/app.py") '
                 'else { return }')
        self.assertIn(guard, self.stop,
                      "the old engine's process is no longer checked before it is signalled")
        self.assertLess(self.stop.index(guard), self.stop.index("kill(pid, SIGTERM)"),
                        "the process is signalled before its command line is checked")

    def test_the_old_file_is_always_removed_so_it_is_asked_once(self):
        self.assertRegex(self.stop, r"defer \{ try\? FileManager\.default\.removeItem\(at: pidFile\) \}")
        self.assertIn("pid > 1", self.stop, "process 1 (launchd) must never be a candidate")

    def test_it_runs_before_the_new_engine_starts_and_never_after(self):
        start = _body_of(self.swift, "private func startEngine(")
        self.assertLess(start.index("stopEngineFromBeforeNC330()"), start.index("try p.run()"))


class TestQuittingStopsOnlyTheEngineThisAppStarted(unittest.TestCase):
    """Was: Quit used the same stop as the restart, so it could never stop anything the
    restart would not. Now the app holds its own engine's process and nothing else."""

    def test_the_stop_is_guarded_by_having_started_it(self):
        fn = _body_of(_read(ENGINE_SWIFT), "func stopEngineIfOwned()")
        first = fn.split("{", 1)[1].lstrip()
        self.assertTrue(first.startswith("guard startedByThisApp, let p = process else { return }"),
                        "stopping no longer begins by checking the engine is this app's own")
        # Asked politely first (its input closes, then it is told to stop), and stopped
        # outright only after a wait.
        self.assertLess(fn.index("closeLifeline()"), fn.index("p.terminate()"))
        self.assertLess(fn.index("p.terminate()"), fn.index("killpg(p.processIdentifier, SIGKILL)"))

    def test_the_wait_before_stopping_outright_is_the_documented_three_seconds(self):
        swift = _read(ENGINE_SWIFT)
        self.assertRegex(swift, r"static let stopWait: TimeInterval = 3\b")


class TestTheServerReportsIt(Base):

    def test_status_reports_the_build_it_was_started_with(self):
        with mock.patch.dict(os.environ, {"NC_BUILD": "20250301T120000Z-abc1234"}):
            self.assertEqual(app.handle_status(self.conn)["build"], "20250301T120000Z-abc1234")

    def test_a_server_started_without_one_reports_none(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NC_BUILD", None)
            self.assertEqual(app.handle_status(self.conn)["build"], "")


if __name__ == "__main__":
    unittest.main()

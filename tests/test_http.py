"""The HTTP layer itself: LAN bearer-token auth and cross-origin write rejection."""

import os
import sys
import unittest
from http import HTTPStatus

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    app,
)


class TestLanAuth(unittest.TestCase):
    """app._require_api_auth: loopback is open; off-loopback needs the token."""

    class _Stub:
        """Minimal stand-in for CrmHandler carrying just the header lookup."""
        def __init__(self, auth=""):
            self._auth = auth
        @property
        def headers(self):
            return {"Authorization": self._auth}

    def _check(self, host, token, path, auth="", query_token=None):
        """Call the real guard with app globals patched; True == allowed."""
        stub = self._Stub(auth)
        # headers is a dict here; .get works the same as http.client's message.
        query = {"token": [query_token]} if query_token is not None else {}
        old_host, old_token = app.HOST, app.APP_TOKEN
        app.HOST, app.APP_TOKEN = host, token
        try:
            app.CrmHandler._require_api_auth(stub, path, query)
            return True
        except app.ApiError as exc:
            self.assertEqual(exc.status, 401)
            return False
        finally:
            app.HOST, app.APP_TOKEN = old_host, old_token

    def test_loopback_never_requires_token(self):
        self.assertTrue(self._check("127.0.0.1", "secret", "/api/status"))
        self.assertTrue(self._check("localhost", "secret", "/api/contacts"))

    def test_lan_requires_valid_token(self):
        self.assertFalse(self._check("0.0.0.0", "secret", "/api/status"))
        self.assertFalse(self._check("0.0.0.0", "secret", "/api/status", auth="Bearer wrong"))
        self.assertTrue(self._check("0.0.0.0", "secret", "/api/status", auth="Bearer secret"))
        self.assertTrue(self._check("0.0.0.0", "secret", "/api/status", query_token="secret"))

    def test_lan_without_a_token_configured_refuses_everything(self):
        """NC-140. An unset token must refuse, not switch the lock off.

        Asserting "off-loopback but APP_TOKEN unset -> no auth enforced (banner
        warns instead)" would describe what a careless guard does, and that is
        the bug: the lock is switched off rather than refusing, so anyone on the
        Wi-Fi could read and rewrite the whole CRM, and the only thing standing
        between them and it would be a print() in a terminal the packaged .app
        does not have.

        A test that writes the bug down as the rule is worse than no test: it
        is what a later fix has to argue with.
        """
        self.assertFalse(self._check("0.0.0.0", "", "/api/status"))
        self.assertFalse(self._check("0.0.0.0", "", "/api/contacts", auth="Bearer anything"))
        self.assertFalse(self._check("0.0.0.0", "   ", "/api/status"),
                         "a token of spaces is not a token")

    def test_the_refusal_says_which_setting_is_missing(self):
        """Whoever hits this 401 is the person who can fix it, and they are
        standing at a different machine from the one that needs changing."""
        stub = self._Stub()
        old_host, old_token = app.HOST, app.APP_TOKEN
        app.HOST, app.APP_TOKEN = "0.0.0.0", ""
        try:
            with self.assertRaises(app.ApiError) as caught:
                app.CrmHandler._require_api_auth(stub, "/api/status", {})
        finally:
            app.HOST, app.APP_TOKEN = old_host, old_token
        self.assertIn("APP_TOKEN", caught.exception.message)
        self.assertIn("secrets/.env", caught.exception.message)

    def test_the_page_itself_still_loads_with_no_token_configured(self):
        """The counter-test to the one above. `/` and the static assets stay
        open so the browser can load and render the overlay in api.js that says
        where the token goes. Gate those too and the person gets a bare 401 and
        no way to read what to do about it."""
        self.assertTrue(self._check("0.0.0.0", "", "/"))
        self.assertTrue(self._check("0.0.0.0", "", "/app/api.js"))

    def test_non_api_paths_are_never_gated(self):
        self.assertTrue(self._check("0.0.0.0", "secret", "/"))
        self.assertTrue(self._check("0.0.0.0", "secret", "/css/ui.css"))


class TestItRefusesToServeTheWifiWithNoLockOnTheDoor(unittest.TestCase):
    """NC-140, the other half. `app._open_lan_refusal` and where main() calls it.

    The guard above is the lock; this is the part that stops the app opening the
    door at all. Setting APP_HOST without APP_TOKEN is not someone choosing a
    weaker setting — it is someone following the home-server instructions in
    README and missing a line. Serving anyway and printing a warning put the
    only notice on a surface the packaged .app does not have.

    What the real process does:
      APP_HOST=0.0.0.0 APP_TOKEN= python3 crm/app.py  ->  exit 2, nothing bound
      APP_HOST=127.0.0.2 APP_TOKEN=x                  ->  walked past this check
                                                          to the socket bind
      no APP_HOST at all                              ->  served, / and
                                                          /api/status both 200
    """

    APP_PY = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                          "crm", "app.py")

    # ── when it must refuse ──────────────────────────────────────────────────
    def test_off_loopback_with_no_token_is_refused(self):
        for token in ("", "   ", "\t\n"):
            self.assertTrue(app._open_lan_refusal("0.0.0.0", token),
                            f"{token!r} is not a token, and serving anyway is NC-140")
        self.assertTrue(app._open_lan_refusal("192.0.2.42", ""))

    def test_the_refusal_says_what_to_do_about_it(self):
        """A refusal nobody can act on just moves the problem. It has to name
        both settings, the file the token goes in, and a way to make one."""
        msg = app._open_lan_refusal("0.0.0.0", "")
        for needed in ("APP_HOST", "APP_TOKEN", ".env", "openssl rand"):
            self.assertIn(needed, msg, f"the refusal never mentions {needed}")
        self.assertIn("0.0.0.0", msg, "it should quote back the value that caused this")

    # ── when it must NOT fire ────────────────────────────────────────────────
    def test_the_zero_config_default_is_untouched(self):
        """The case that matters most. Every copy of this app that anyone runs
        is this one: loopback, no token, no settings. If this ever returns a
        message, the app stops starting for everybody."""
        for host in sorted(app.LOOPBACK_HOSTS):
            self.assertEqual(app._open_lan_refusal(host, ""), "",
                             f"{host} is this machine talking to itself — no token needed")

    def test_a_configured_home_server_starts_normally(self):
        self.assertEqual(app._open_lan_refusal("0.0.0.0", "a-real-token"), "")
        self.assertEqual(app._open_lan_refusal("192.0.2.42", "a-real-token"), "")

    # ── where the check sits ─────────────────────────────────────────────────
    def test_it_refuses_before_it_opens_the_socket(self):
        """Order is the whole point. A check that runs after
        ThreadingHTTPServer() has already published the port on the network has
        already lost — and one that runs after init_db and the autosync thread
        has touched the database and started importing on behalf of a server
        that is not going to run."""
        with open(self.APP_PY, encoding="utf-8") as fh:
            src = fh.read()
        body = src[src.index("\ndef main() -> None:"):]
        checked = body.index("_open_lan_refusal")
        self.assertLess(checked, body.index("ThreadingHTTPServer("),
                        "main() binds the socket before deciding whether it should")
        self.assertLess(checked, body.index("_init_db_or_recover("),
                        "main() opens the database before deciding whether to run")
        self.assertLess(checked, body.index("threading.Thread("),
                        "main() starts the autosync thread before deciding whether to run")

    def test_starting_it_for_real_exits_instead_of_serving(self):
        """The observable claim, run rather than reasoned about: `APP_HOST` set,
        `APP_TOKEN` empty, and the process is gone.

        A timeout here is the failure that matters — it means the server came up
        and sat there serving, which is exactly the bug."""
        import subprocess
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ,
                       APP_HOST="0.0.0.0", APP_TOKEN="", APP_PORT="8793",
                       CRM_DATA_DIR=os.path.join(tmp, "data"),
                       CRM_SECRETS_DIR=os.path.join(tmp, "secrets"),
                       NC_AUTOSYNC="0", NC_NO_BROWSER="1")
            try:
                done = subprocess.run([sys.executable, self.APP_PY], env=env,
                                      capture_output=True, text=True, timeout=45)
            except subprocess.TimeoutExpired:
                self.fail("it served the network with no token instead of refusing")
        self.assertNotEqual(done.returncode, 0,
                            "it exited cleanly, so a launcher would think all was well")
        self.assertIn("APP_TOKEN", done.stderr + done.stdout)


class TestCrossOriginWrites(unittest.TestCase):
    """Loopback needs no bearer token, so every write endpoint is reachable by
    anything running in a browser on this machine — including a page on an
    unrelated domain. Two gates stop that, and both are load-bearing:

    A cross-origin POST with Content-Type text/plain is a CORS *simple request* —
    no preflight, so the side effect lands even though the attacker can't read the
    reply. Chaining a settings write that accepted a server address into a route
    that uploaded the contacts table would have posted it to a server of the
    caller's choosing. No such pair of routes exists now; every write endpoint
    still needs these gates.

    These assert the header logic directly rather than over a socket, so they stay
    fast and need no server; the endpoints they protect are covered above."""

    class _Fake:
        """Minimal stand-in exposing just what _reject_cross_origin reads."""

        def __init__(self, headers):
            self.headers = headers

    def _check(self, **headers):
        app.CrmHandler._reject_cross_origin(self._Fake(headers))

    def test_a_page_on_another_site_cannot_write(self):
        with self.assertRaises(app.ApiError) as caught:
            self._check(Origin="https://totally-unrelated-blog.example")
        self.assertEqual(caught.exception.status, HTTPStatus.FORBIDDEN)

    def test_sec_fetch_site_cross_site_is_refused_even_without_an_origin(self):
        with self.assertRaises(app.ApiError):
            self._check(**{"Sec-Fetch-Site": "cross-site"})

    def test_the_apps_own_origin_is_allowed(self):
        for origin in sorted(app.ALLOWED_ORIGINS):
            self._check(Origin=origin, **{"Sec-Fetch-Site": "same-origin"})

    def test_a_non_browser_caller_sending_no_origin_is_allowed(self):
        """curl and crm/crm.py send no Origin; refusing that would break the CLI."""
        self._check()

    def test_allowed_origins_covers_every_loopback_spelling(self):
        """127.0.0.1 and localhost are the same server; a page served from one
        must not be treated as foreign by the other."""
        hosts = {o.split("://")[1].rsplit(":", 1)[0] for o in app.ALLOWED_ORIGINS}
        self.assertIn("127.0.0.1", hosts)
        self.assertIn("localhost", hosts)


class TestTheHomeServerCanBeWrittenTo(unittest.TestCase):
    """NC-268. The home-server setup binds APP_HOST=0.0.0.0, and a second device
    opens the machine's real address. A fixed list holding only 0.0.0.0 would
    refuse every save from that device as cross-origin while reading worked."""

    def _check(self, bound, **headers):
        old = app.HOST
        app.HOST = bound
        try:
            app.CrmHandler._reject_cross_origin(TestCrossOriginWrites._Fake(headers))
        finally:
            app.HOST = old

    def test_a_page_from_the_address_the_request_came_to_can_write(self):
        self._check("0.0.0.0", Origin="http://192.0.2.20:8765", Host="192.0.2.20:8765",
                    **{"Sec-Fetch-Site": "same-origin"})

    def test_a_page_from_another_address_still_cannot(self):
        with self.assertRaises(app.ApiError) as caught:
            self._check("0.0.0.0", Origin="http://192.0.2.99:8765", Host="192.0.2.20:8765")
        self.assertEqual(caught.exception.status, HTTPStatus.FORBIDDEN)

    def test_a_save_over_the_socket_goes_through(self):
        # The whole write path, token and all: served on loopback here, with the
        # app told it is bound to the LAN, so nothing is opened to the network.
        import json
        import threading
        import urllib.request
        from http.server import ThreadingHTTPServer
        app.db.init_db()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.CrmHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        old = app.HOST, app.APP_TOKEN
        app.HOST, app.APP_TOKEN = "0.0.0.0", "t0k3n"
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{server.server_address[1]}/api/contacts",
                data=json.dumps({"name": "Ada Lovelace"}).encode(), method="POST",
                headers={"Content-Type": "application/json", "Authorization": "Bearer t0k3n",
                         "Host": "192.0.2.20:8765", "Origin": "http://192.0.2.20:8765",
                         "Sec-Fetch-Site": "same-origin"})
            with urllib.request.urlopen(req, timeout=10) as resp:
                self.assertEqual(resp.status, 200)
                new_id = json.loads(resp.read())["id"]
            conn = app.db.get_db()
            try:
                row = conn.execute("SELECT name FROM contacts WHERE id = ?", (new_id,)).fetchone()
            finally:
                conn.close()
            self.assertEqual(row["name"], "Ada Lovelace", "the save did not land")
        finally:
            app.HOST, app.APP_TOKEN = old
            server.shutdown()
            server.server_close()

    def test_loopback_keeps_its_fixed_list(self):
        # No token stands behind loopback, so a page whose name happens to point
        # here must not be let in by matching the Host header.
        with self.assertRaises(app.ApiError):
            self._check("127.0.0.1", Origin="http://evil.example:8765", Host="evil.example:8765")


class TestOnlyThisMacCanRead(unittest.TestCase):
    """NC-309. Reads used to answer any Host. A web page that points its own name
    at 127.0.0.1 (DNS rebinding) is, to the browser, still its own site, so it
    could read the reply: every contact, note and message excerpt through
    /api/export. The write gate never covered this, because reads leave no side
    effect for it to refuse. Now anything not addressed to this Mac is refused
    before routing."""

    def test_only_this_macs_names_on_the_bound_port_pass(self):
        ok = app._host_is_this_machine
        self.assertTrue(ok("127.0.0.1:8765", 8765))
        self.assertTrue(ok("localhost:8765", 8765))
        self.assertTrue(ok("LOCALHOST:8765", 8765))
        self.assertTrue(ok("[::1]:8765", 8765))
        self.assertFalse(ok("evil.example:8765", 8765))
        self.assertFalse(ok("127.0.0.1.evil.example:8765", 8765))
        self.assertFalse(ok("127.0.0.1:9999", 8765), "another port is another program")
        self.assertFalse(ok("localhost", 8765), "a missing port means port 80")

    def test_a_caller_that_sends_no_host_is_not_a_browser(self):
        self.assertTrue(app._host_is_this_machine("", 8765))

    def test_served_on_a_network_the_token_decides_instead(self):
        old = app.HOST
        app.HOST = "0.0.0.0"
        try:
            self.assertTrue(app._host_is_this_machine("192.0.2.20:8765", 8765))
        finally:
            app.HOST = old

    def _get(self, port, path, host):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers={"Host": host})
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status, resp.read()
        except urllib.error.HTTPError as exc:
            return exc.code, exc.read()

    def test_a_rebinding_page_gets_nothing_and_the_window_still_gets_everything(self):
        import threading
        from http.server import ThreadingHTTPServer
        app.db.init_db()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.CrmHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            for path in ("/api/export?format=json", "/api/contacts", "/api/settings", "/"):
                status, body = self._get(port, path, f"evil.example:{port}")
                self.assertEqual(status, 403, f"{path} answered a page from another site")
                self.assertNotIn(b"contacts", body.lower().replace(b"addressed", b""))
            for path in ("/api/export?format=json", "/api/contacts", "/"):
                status, _ = self._get(port, path, f"127.0.0.1:{port}")
                self.assertEqual(status, 200, f"{path} refused the app's own window")
        finally:
            server.shutdown()
            server.server_close()

    def test_a_write_from_a_rebinding_page_is_refused_at_the_door(self):
        # The write gate already refused this by its Origin; the Host check now
        # refuses it first, so a page that sends no Origin is covered too.
        import threading
        import urllib.error
        import urllib.request
        from http.server import ThreadingHTTPServer
        app.db.init_db()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.CrmHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            req = urllib.request.Request(
                f"http://127.0.0.1:{port}/api/contacts", method="POST",
                data=b'{"name": "Ada Lovelace"}',
                headers={"Content-Type": "application/json", "Host": f"evil.example:{port}"})
            with self.assertRaises(urllib.error.HTTPError) as caught:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(caught.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()


class TestOtherSitesCannotLoadTheData(unittest.TestCase):
    """NC-323. With the right Host, a page on another site could still make the
    browser load an /api address as an image: dismiss the recovery notice, or
    learn which contacts have photos. Browsers mark those loads cross-site."""

    def _get(self, port, path, **headers):
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"http://127.0.0.1:{port}{path}", headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=10) as resp:
                return resp.status
        except urllib.error.HTTPError as exc:
            return exc.code

    def test_cross_site_loads_are_refused_and_the_window_is_not(self):
        import threading
        from http.server import ThreadingHTTPServer
        app.db.init_db()
        server = ThreadingHTTPServer(("127.0.0.1", 0), app.CrmHandler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        port = server.server_address[1]
        try:
            for path in ("/api/photo/1", "/api/db-recovery-notice/dismiss",
                         "/api/contacts", "/api/export?format=json"):
                for site in ("cross-site", "same-site"):
                    self.assertEqual(self._get(port, path, **{"Sec-Fetch-Site": site}), 403,
                                     f"{path} answered a {site} load")
            for site in ("same-origin", "none"):
                self.assertEqual(self._get(port, "/api/contacts", **{"Sec-Fetch-Site": site}), 200)
            self.assertEqual(self._get(port, "/api/contacts"), 200, "the CLI sends no Sec-Fetch-Site")
            self.assertEqual(self._get(port, "/", **{"Sec-Fetch-Site": "cross-site"}), 200,
                             "a link to the app's page from elsewhere still opens it")
        finally:
            server.shutdown()
            server.server_close()


class TestRouteRegistry(unittest.TestCase):
    """`app.ROUTE_MODULES` / `CrmHandler._route_registered`.

    The registry is what lets a new API domain ship without editing app.py's
    route tables or its re-export block. It exists because the old
    edit-app.py-for-everything arrangement left crm/api_intake.py finished,
    tested and unreachable: a whole module behind a handful of URLs that could
    not be added without editing app.py.

    The security property under test is the one any route registry must keep:
    the registry is consulted at the END of _route_get/_route_write, both of
    which run after _require_api_auth. So it can neither shadow a built-in route
    nor see an unauthenticated request.
    """

    class _Stub:
        """Stands in for CrmHandler, carrying just the conn helper."""
        def __init__(self):
            self.conn = object()
            self.used_conn = False

        def _with_conn(self, fn):
            self.used_conn = True
            return fn(self.conn)

    class _FakeModule:
        """One module, one handler per calling convention."""
        __name__ = "fake_api_module"
        ROUTES = {
            "GET": {"/api/fake/bare": "h_bare", "/api/fake/conn": "h_conn"},
            "POST": {"/api/fake/both": "h_both", "/api/fake/payload": "h_payload",
                     "/api/fake/missing": "h_does_not_exist"},
        }

        @staticmethod
        def h_bare():
            return {"got": "nothing"}

        @staticmethod
        def h_conn(conn):
            return {"got": "conn", "conn_is": conn}

        @staticmethod
        def h_both(conn, body):
            return {"got": "conn+body", "conn_is": conn, "body": body}

        @staticmethod
        def h_payload(body):
            return {"got": "body", "body": body}

    def _dispatch(self, method, path, payload, modules=None):
        stub = self._Stub()
        old = app.ROUTE_MODULES
        app.ROUTE_MODULES = [self._FakeModule] if modules is None else modules
        try:
            return stub, app.CrmHandler._route_registered(stub, method, path, payload)
        finally:
            app.ROUTE_MODULES = old

    # ── the signature contract ───────────────────────────────────────────────
    def test_a_handler_taking_nothing_is_called_with_nothing(self):
        stub, out = self._dispatch("GET", "/api/fake/bare", {"q": ["x"]})
        self.assertEqual(out, {"got": "nothing"})
        self.assertFalse(stub.used_conn, "opened a db connection for a handler that takes none")

    def test_a_first_parameter_named_conn_receives_the_connection(self):
        stub, out = self._dispatch("GET", "/api/fake/conn", {})
        self.assertEqual(out["got"], "conn")
        self.assertIs(out["conn_is"], stub.conn)
        self.assertTrue(stub.used_conn)

    def test_conn_and_payload_are_passed_in_that_order(self):
        stub, out = self._dispatch("POST", "/api/fake/both", {"name": "Ada"})
        self.assertEqual(out["got"], "conn+body")
        self.assertIs(out["conn_is"], stub.conn)
        self.assertEqual(out["body"], {"name": "Ada"})

    def test_a_payload_only_handler_gets_no_connection(self):
        """The case that matters: handle_intake_lookup must not hold a crm.db
        handle open while it waits on a socket."""
        stub, out = self._dispatch("POST", "/api/fake/payload", {"url": "https://x"})
        self.assertEqual(out, {"got": "body", "body": {"url": "https://x"}})
        self.assertFalse(stub.used_conn)

    # ── failure modes ────────────────────────────────────────────────────────
    def test_an_unclaimed_path_returns_none_so_the_caller_404s(self):
        _, out = self._dispatch("GET", "/api/fake/nope", {})
        self.assertIsNone(out)

    def test_a_method_the_module_does_not_declare_is_not_matched(self):
        _, out = self._dispatch("DELETE", "/api/fake/bare", {})
        self.assertIsNone(out)

    def test_a_routes_entry_naming_a_missing_handler_is_a_500_not_a_404(self):
        """A typo in ROUTES is a bug in that module. A silent 404 would send
        whoever wrote it hunting through app.py instead."""
        with self.assertRaises(app.ApiError) as caught:
            self._dispatch("POST", "/api/fake/missing", {})
        self.assertEqual(caught.exception.status, HTTPStatus.INTERNAL_SERVER_ERROR)
        # The message has to name both, or the reader cannot tell which module
        # carries the typo.
        self.assertIn(self._FakeModule.__name__, caught.exception.message)
        self.assertIn("h_does_not_exist", caught.exception.message)

    def test_a_module_without_routes_is_skipped_not_crashed_on(self):
        class _NoRoutes:
            __name__ = "no_routes"
        _, out = self._dispatch("GET", "/api/fake/bare", {}, modules=[_NoRoutes, self._FakeModule])
        self.assertEqual(out, {"got": "nothing"})

    # ── the real wiring ──────────────────────────────────────────────────────
    def test_intake_is_actually_registered(self):
        """The regression guard for the failure this whole mechanism prevents:
        a finished intake module with no route registered."""
        self.assertIn(app.api_intake, app.ROUTE_MODULES)
        self.assertEqual(sorted(app.api_intake.ROUTES["GET"]),
                         ["/api/intake/batch", "/api/intake/status"])

    def test_every_registered_route_names_a_callable_handler(self):
        for mod in app.ROUTE_MODULES:
            for method, table in getattr(mod, "ROUTES", {}).items():
                for path, name in table.items():
                    self.assertTrue(callable(getattr(mod, name, None)),
                                    f"{mod.__name__}: {method} {path} -> {name}")

    def test_no_registered_route_shadows_a_built_in_path(self):
        """Built-ins win because the registry runs last; this asserts no module
        even tries, so nobody debugs a route that silently never fires."""
        import re as _re
        app_py = os.path.join(os.path.dirname(os.path.dirname(
            os.path.abspath(__file__))), "crm", "app.py")
        with open(app_py, encoding="utf-8") as fh:
            source = fh.read()
        builtin = set(_re.findall(r'path == "(/api/[^"]+)"', source))
        for mod in app.ROUTE_MODULES:
            for method, table in getattr(mod, "ROUTES", {}).items():
                for path in table:
                    self.assertNotIn(path, builtin,
                                     f"{mod.__name__} declares {method} {path}, "
                                     "which a literal branch already handles")

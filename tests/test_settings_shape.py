"""NC-003 gate: Settings shows identity, credentials/connections and on/off
switches — never a raw numeric tuning field, but for ONE named, tested
exception. ~34 of the ~35 numeric tuning fields are kept out of the rendered UI
entirely; only identity, credentials/connections and switches are open on the
page. Nothing here should ever render a field whose label states a raw unit —
"in tokens", "in seconds", "hours between" are the three example shapes
(NC-003) that must never come back — except NC_DATES_LOOKAHEAD_DAYS
(DELIBERATE_NUMERIC_EXCEPTION below), which NC-040 keeps as a real field
because it drives the visible upcoming-dates strip above People, and which
tests/test_birthdays.py already guards from the other side. Rather than
loosen the rule to let it through quietly, it is named explicitly, so a
SECOND numeric field sneaking in anywhere else still fails loudly.

NC-236: the old Settings page held its fields in JavaScript
arrays; the rebuilt Settings and Data pages draw every box from what
crm/api_abilities.py and crm/api_ledger.py send. So this reads the boxes out of
those two files (comment lines dropped first, so prose that merely mentions
"seconds" cannot trip it). The one numeric exception is a row of its own on the
rebuilt page (NC-236), so the exception is named again.
"""
import io
import os
import re
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, js_code  # noqa: E402,F401 — CRM_DATA_DIR bootstrap

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_JS = os.path.join(REPO, "static", "app", "settings.js")
SETTINGS_PY = os.path.join(REPO, "crm", "settings.py")

ABILITIES_PY = os.path.join(REPO, "crm", "api_abilities.py")
LEDGER_PY = os.path.join(REPO, "crm", "api_ledger.py")

DELIBERATE_NUMERIC_EXCEPTION = "NC_DATES_LOOKAHEAD_DAYS"
# NC-364: money a person can feel gets a box (the limits people can feel are
# settings, money above all). The most one paid run may send to Anthropic, and
# the two prices Ask and the paste reader use to say what something cost. They
# come with the Anthropic key on the AI row, each with its own range.
MONEY_BOXES = {"ENRICH_LLM_MAX", "NC_PRICE_IN_PER_MTOK", "NC_PRICE_OUT_PER_MTOK"}

# A unit PHRASE in a label is a numeric tuning field wearing a human label —
# the shape NC-003 removed, with the exact phrasing: "in tokens", "in seconds",
# "hours between". Anchored on phrase shape rather than the bare word, on purpose: "token" alone is also this
# app's word for a credential ("LinkedIn token", "Device token"), and "Sync
# every 30 minutes" describes a fixed backend cadence, not a number the user
# picks — a bare-word match flagged both as false positives. "in <unit>",
# "<unit> between", and "days back/ahead" cover every removed field's label;
# see crm/settings.py's NC-003 comment for the full removed list.
UNIT_PHRASES = re.compile(
    r"\bin (tokens?|seconds?|characters?|chars?|kb|mb)\b"
    r"|\b(hours?|minutes?) between\b"
    r"|\bdays? (back|ahead)\b"
    r"|\b(seconds?|characters?|chars?) (before|kept|the)\b",
    re.I)


def _read_settings_js():
    with io.open(SETTINGS_JS, encoding="utf-8") as fh:
        return fh.read()


def _server_code(path):
    """A server file with its comment lines dropped."""
    with io.open(path, encoding="utf-8") as fh:
        return "\n".join(ln for ln in fh.read().splitlines()
                         if not ln.lstrip().startswith("#"))


_PIECES = r'((?:f?"(?:[^"\\]|\\.)*"\s*)+)'
_BOX = re.compile(r'\b(_text|_secret|_switch_act|_switch)\(\s*"([A-Z][A-Z0-9_]*)"\s*,\s*' + _PIECES)
_KIND = {"_text": "text", "_secret": "secret", "_switch_act": "toggle", "_switch": "toggle"}


def _joined(pieces):
    return "".join(re.findall(r'"((?:[^"\\]|\\.)*)"', pieces))


def _specs_in_file(path):
    """Every box the page is told to draw by one server file: its key, its
    label, and what kind of control it is. Read from the calls that build
    them, so a box added later is seen without anyone editing this list."""
    src = _server_code(path)
    out = [{"key": m.group(2), "label": _joined(m.group(3)), "type": _KIND[m.group(1)]}
           for m in _BOX.finditer(src)]
    out += [{"key": m.group(1), "label": "Model", "type": "select"}
            for m in re.finditer(r'"kind": "model",\s*"key": "([A-Z_]+)"', src)]
    # The two calendar addresses are built with a variable key, so name them.
    out += [{"key": k, "label": "calendar address", "type": "secret"}
            for k in ("NC_ICS_GOOGLE_URL", "NC_ICS_ICLOUD_URL") if '"%s"' % k in src]
    return out


def _all_rendered_specs():
    return _specs_in_file(ABILITIES_PY) + _specs_in_file(LEDGER_PY)


def _default_of(key):
    sys.path.insert(0, os.path.join(REPO, "crm"))
    import settings as settings_mod
    return settings_mod.MANAGED_KEYS[key]["default"]


class TestSettingsShowsNoNumericTuningField(unittest.TestCase):
    """The NC-003 gate itself, from two independent angles so a mislabelled
    field and a differently-labelled raw-number field are both caught:

      1. structural: no rendered text or secret box is for a key whose value
         is a bare number (its registry default is digits). A switch is a yes
         or no decision whatever its help text says, so switches are exempt.
      2. textual, exempting switches and pickers: no label names a raw unit.
    """

    def setUp(self):
        self.specs = _all_rendered_specs()
        # If this is empty every check below passes vacuously. Keep the gap
        # small: the point is to catch a parser returning almost nothing.
        self.assertGreater(len(self.specs), 8,
                            "far fewer rendered boxes than expected — the "
                            "parser of the server files likely broke silently")

    def test_no_box_takes_a_bare_number_except_the_one_exception(self):
        offenders = [s["key"] for s in self.specs
                     if s["type"] in ("text", "secret")
                     and _default_of(s["key"]).strip().isdigit()
                     and s["key"] != DELIBERATE_NUMERIC_EXCEPTION
                     and s["key"] not in MONEY_BOXES]
        self.assertEqual(offenders, [],
                          f"these rendered boxes still take a raw number: {offenders}")

    def test_exactly_one_box_is_the_documented_numeric_exception(self):
        """From the other direction: the named one is the ONLY numeric box, so
        a second one landing beside it fails here, not only above."""
        numeric = [s["key"] for s in self.specs
                   if s["type"] in ("text", "secret") and _default_of(s["key"]).strip().isdigit()]
        self.assertEqual(sorted(numeric), sorted([DELIBERATE_NUMERIC_EXCEPTION, *MONEY_BOXES]))

    # NC-236: "the old check.num validator no longer exists" went with the old
    # Settings page; the validator object was that page's own code. The box's
    # own check is ledger.js FORMAT, pinned in test_the_settings_ledger_is_one_answer.

    def test_no_label_carries_a_unit_unless_it_is_a_switch_or_the_exception(self):
        offenders = []
        for s in self.specs:
            if s["type"] in ("toggle", "select"):
                continue                      # a switch/picker, not a number
            if s["key"] == DELIBERATE_NUMERIC_EXCEPTION:
                continue                      # named, tested exception
            if UNIT_PHRASES.search(s["label"]):
                offenders.append(f"{s['key']}: {s['label']!r}")
        self.assertEqual(offenders, [], "\n".join([
            "these rendered field labels name a raw unit — the exact shape",
            'NC-003 banned ("in tokens", "in seconds", "hours between"):',
            *(f"  {o}" for o in offenders),
        ]))

    def test_every_on_off_key_renders_as_a_switch_not_a_type_a_digit_box(self):
        """Every managed key whose default is 0 or 1 is an on/off decision, and
        the page must draw it as a switch (On/Off buttons), never as a text box
        asking you to type 1 or 0."""
        switches = [s for s in self.specs if s["type"] == "toggle"]
        self.assertTrue(switches, "no switch was found, so this would pass by "
                                  "looking at nothing")
        offenders = [s["key"] for s in self.specs
                     if s["type"] != "toggle" and _default_of(s["key"]) in ("0", "1")]
        self.assertEqual(offenders, [],
                          f"on/off keys drawn as something other than a switch: {offenders}")
        js = _read_settings_js()
        self.assertRegex(js, r'case "switch":\s*return html`<\$\{Switch\}',
                         "the Settings page no longer draws a switch act as a Switch")


class TestSettingsHoldsOneLinkedInFieldNotThree(unittest.TestCase):
    """NC-073. Settings keeps only what is useful, because every extra row is
    clutter that hides the ones that matter.

    NC-068 rendered LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET here to buy one
    thing: an expiry date instead of "expiry unknown". The cost was two more
    credential rows, one a secret to store and rotate, for something that only
    matters when the token expires — and three near-identical LinkedIn rows with nothing
    saying which the import needed. Sorting them out risked the token: clearing
    the wrong one stops message collection until it is pasted back.

    So this is a clutter gate, not a copy gate. The keys stay in settings.py, so
    the capability is not deleted — it is just not on the screen, which is
    NC-003's own move for the ~34 numeric fields it removed.
    """

    def test_the_token_and_the_apps_own_id_and_secret_are_rendered(self):
        # NC-236: the token's box is on the Data page's LinkedIn messages row now.
        # NC-371 (2026-10-17) reversed NC-073's "token only": the app's own client
        # ID and secret sit beside the token on the same row, shown only once a
        # token is connected, because without them the token's expiry date can
        # never be read, and the old way in was editing a file by hand.
        keys = {s["key"] for s in _all_rendered_specs() if s["key"].startswith("LINKEDIN_")}
        self.assertEqual(
            keys, {"LINKEDIN_TOKEN", "LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"},
            f"Settings renders {len(keys)} LinkedIn fields: {sorted(keys)}. The "
            f"token and the app's own client ID and secret belong on the screen "
            f"(NC-371); any other LinkedIn setting stays in the registry.")

    def test_the_two_client_values_stay_in_the_registry(self):
        """The boxes write through the registry, and the expiry check reads the
        values from it; a box with no registry key behind it saves nothing."""
        import settings as st
        for key in ("LINKEDIN_CLIENT_ID", "LINKEDIN_CLIENT_SECRET"):
            self.assertIn(
                key, st.MANAGED_KEYS,
                f"{key} left the registry. The expiry check in "
                f"crm/linkedin_token.py reads it from the environment, and its box "
                f"on the Data page writes through the registry.")

    def test_the_token_copy_says_what_clearing_it_costs(self):
        """A cleared token must say what will stop: nothing on the row may leave
        that unsaid. The words live in the LinkedIn messages row's help
        (api_ledger.py)."""
        src = _server_code(LEDGER_PY)
        at = src.index("def _row_linkedin_messages(")
        block = src[at:src.index("\ndef ", at + 10)]
        help_text = " ".join(_joined(m.group(0)) for m in re.finditer(
            r'(?:f?"(?:[^"\\]|\\.)*"\s*)+', block)).lower()
        self.assertTrue(help_text, "the token row has no help text")
        self.assertTrue(
            "stops" in help_text or "stop " in help_text,
            "the token row's help does not say that removing the token stops "
            "message collection")

    def test_no_linkedin_copy_claims_a_sixty_day_token(self):
        """NC-069. LinkedIn's access token lasts 60 days for an app with no
        products approved, and 31,536,000 seconds — a year — for an app with the
        data-portability product approved.
        A wrong figure in user-facing copy can survive the module comment being
        corrected, because comments and copy are fixed separately. This checks the
        copy, which is the half people read.

        Every page file plus the server's help text, because it is one fact told
        in several places (NC-236: the old two files were settings.js and
        linkedin.js; the copy now lives in the pages and in api_ledger.py).

        Refined by NC-079 from "the string must not appear" to "the claim must
        not be made about the user's own token". See the comment at the match below.
        """
        here = os.path.dirname(os.path.abspath(__file__))
        offenders = []
        for rel in ("static/app/settings.js", "static/app/data.js",
                    "static/app/ledger.js", "static/app/gmail.js",
                    "crm/api_ledger.py", "crm/api_abilities.py"):
            path = os.path.join(here, "..", rel)
            with io.open(path, encoding="utf-8") as fh:
                code = fh.read() if rel.endswith(".py") else js_code(fh.read())
            for m in re.finditer(r"60[\s-]+days?", code, flags=re.I):
                # NC-079 refined this. The rule is not "never write the number" —
                # it is "never tell the user THEIR token lasts 60 days". 60 days
                # is the true life of a token from an app with no products
                # approved, and saying so is what makes "use an app with the
                # data-portability product" a step worth following rather than a
                # superstition. A blanket ban would forbid the app from telling
                # the user the very fact this test's own failure message cites
                # as ground truth.
                #
                # So: the figure is allowed only where the sentence it sits in
                # marks it as the app that does NOT have the product. Anywhere
                # else it is a claim about the user's token and still fails.
                start = max(0, m.start() - 240)
                sentence = code[start:m.start()]
                cut = max(sentence.rfind("."), sentence.rfind("<li>"))
                if cut != -1:
                    sentence = sentence[cut:]
                attributed = re.search(
                    r"without it|without that|no products|not approved|"
                    r"an app without", sentence, flags=re.I)
                if attributed:
                    continue
                lo = max(0, m.start() - 70)
                offenders.append(f"{rel}: …{code[lo:m.end() + 40].strip()}…")
        self.assertEqual(offenders, [], "\n".join([
            "", "LinkedIn copy still tells the user their token lasts 60 days. Documented:",
            "  an app with no products approved   5,184,000 s =  60 days",
            "  an app with data portability      31,536,000 s = 365 days",
            "and the app this CRM's guide has people create is the second kind.",
            *offenders]))


class TestNoHelpCopySendsAnyoneToASettingThatIsNotThere(unittest.TestCase):
    """NC-074.

    Every source's `export_help` in importers.py is rendered verbatim on the Data
    page. Several of them named environment keys in backticks and said the words
    "in Settings" — and the style row's said "Seven limits are tunable in
    Settings" followed by seven key names, **none of which had rendered since
    NC-003 pulled the numeric fields off that page.** That sentence stayed false.
    Nothing checked it, because the copy lives in Python and the fields
    live in JavaScript, and no test read both.

    That is the whole failure mode: telling someone to go to a screen and change
    a thing that is not on it. Worse than saying nothing, because they will go
    and look, and then doubt the rest of the page.

    The rule: help copy may name a `BACKTICKED_KEY` only if a Settings field
    actually renders it. Anything that is really env-only gets described by what
    it does, or by the on-screen label of the switch that governs it — never by
    its variable name.
    """

    def _rendered_keys(self):
        # NC-236: a key with a box on the Settings page or the Data page.
        return {sp["key"] for sp in _all_rendered_specs()}

    def _help_blobs(self):
        """(source key, help text) for every registry card, read from the module
        rather than re-parsed out of the file, so a card that moves is followed."""
        sys.path.insert(0, os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
        import importers
        out = []
        for key, spec in sorted(importers.REGISTRY.items()):
            for attr in ("export_help", "help"):
                text = getattr(spec, attr, "") or ""
                if text:
                    out.append((f"{key}.{attr}", text))
        return out

    def test_no_backticked_key_names_a_field_that_does_not_render(self):
        rendered = self._rendered_keys()
        offenders = []
        for where, text in self._help_blobs():
            for key in re.findall(r"`([A-Z][A-Z0-9_]{3,})`", text):
                if key not in rendered:
                    offenders.append(f"{where} names `{key}`, which no box on the Settings or Data page renders")
        self.assertEqual(offenders, [], "\n".join([
            "",
            "Help copy points at settings that are not on the Settings screen.",
            "Describe what the thing does, or name the on-screen label of the",
            "switch that governs it — never a variable the reader cannot find:",
            *[f"  {o}" for o in offenders]]))

    def test_no_panel_copy_sends_the_owner_to_a_removed_field_either(self):
        """The same bug, in the file this gate could not see.

        The check above reads importers.py. It passed while `static/app/linkedin.js`
        said, in two live places, "Adding your app's client ID and secret in
        Settings would let the app tell you the exact expiry date" and "Add the
        app's client ID and secret in Settings to track the expiry" — after
        NC-073 had removed both of those fields from the Settings screen.

        So a gate written against a failure mode, scoped to one file, missed an
        instance of it in another. This one reads the frontend
        panels too, and matches on the PROSE rather than on backticked keys,
        because panel copy names things the way a person would say them.

        REMOVED_FROM_SETTINGS is deliberately a hand-kept list. There is no way to
        derive "a field that used to be here" from a file that no longer mentions
        it, and the moment to add an entry is the moment you delete a row — which
        is exactly when this test's failure message will find you.
        """
        # Human phrasings of controls that are NOT on the Settings screen, each
        # with the change that removed it. Extend when you remove a field.
        REMOVED_FROM_SETTINGS = [
            ("client ID", "NC-073 — LINKEDIN_CLIENT_ID"),
            ("client id", "NC-073 — LINKEDIN_CLIENT_ID"),
            ("client secret", "NC-073 — LINKEDIN_CLIENT_SECRET"),
        ]
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        app = os.path.join(here, "static", "app")
        offenders = []
        for name in sorted(os.listdir(app)):
            if not name.endswith(".js"):
                continue
            with io.open(os.path.join(app, name), encoding="utf-8") as fh:
                code = js_code(fh.read())
            # Sentence-ish windows, so "Settings" three paragraphs away is not a
            # match. Template literals are newline-heavy, so normalise first.
            flat = " ".join(code.split())
            for phrase, why in REMOVED_FROM_SETTINGS:
                for m in re.finditer(re.escape(phrase), flat):
                    window = flat[max(0, m.start() - 120):m.end() + 120]
                    if re.search(r"\bSettings\b", window):
                        offenders.append(
                            f"static/app/{name}: {phrase!r} offered as a Settings "
                            f"field ({why}) — …{window[40:200].strip()}…")
        # Deduplicate: one string can match "client ID" and "client id" both.
        seen, unique = set(), []
        for o in offenders:
            k = o[:150]
            if k not in seen:
                seen.add(k)
                unique.append(o)
        self.assertEqual(unique, [], "\n".join([
            "",
            "Panel copy tells the owner to change something in Settings that is not on",
            "the Settings screen. Say what the app can and cannot tell the owner, or",
            "name a control that renders — never one that was removed:",
            *[f"  {o}" for o in unique]]))

    def test_no_help_offers_to_adjust_something_it_does_not_name(self):
        """NC-120. The gate that was green while the bug was on two screens.

        Two help blobs said "(adjustable in Settings)" for IMSG_GROUP_MAX and
        WA_GROUP_MAX, which NC-003 removed from the registry. Both are rendered
        verbatim on the Data page AND in the first-run wizard. Neither of the
        two gates that exist for this could see them: one matches only
        BACKTICKED key names, the other reads static/app/*.js, and these strings
        live in crm/importers.py and arrive in the page at runtime.

        So this one matches the promise instead of the spelling. Saying a thing
        is adjustable in Settings is fine — every legitimate case does it while
        naming WHAT to change, either as a `KEY` (which the backtick gate then
        checks renders) or as a **bold on-screen label**. Naming nothing is the
        defect: the reader goes to look, finds no such control, and from then on
        doubts the rest of the page.
        """
        verb = (r"adjust\w*|chang\w*|configur\w*|customi[sz]\w*|tun(?:e|ed|able|ing)"
                r"|rais\w*|lower\w*|edit\w*")
        near = re.compile(rf"(?:{verb})[^.]{{0,80}}\bSettings\b"
                          rf"|\bSettings\b[^.]{{0,80}}(?:{verb})", re.I)
        offenders = []
        for where, text in self._help_blobs():
            for m in near.finditer(text):
                start = text.rfind(".", 0, m.start()) + 1
                end = text.find(".", m.end())
                sentence = text[start:end if end != -1 else len(text)]
                names_a_key = re.search(r"`[A-Z][A-Z0-9_]{3,}`", sentence)
                names_a_label = re.search(r"\*\*[^*]+\*\*", sentence)
                if not names_a_key and not names_a_label:
                    offenders.append(f"{where}: {sentence.strip()[:110]!r}")
        self.assertEqual(offenders, [], "\n".join([
            "",
            "Help copy says something can be adjusted in Settings without saying",
            "WHAT to adjust. Name the `KEY` or the **on-screen label**, or state",
            "the limit as a plain fact and promise nothing:",
            *[f"  {o}" for o in offenders]]))

    def test_no_help_claims_a_count_of_tunable_settings(self):
        """The specific shape that went wrong: "Seven limits are tunable in
        Settings", then a list. A number makes it verifiable and it was wrong;
        prose about behaviour cannot rot the same way."""
        offenders = []
        pat = re.compile(r"\b(two|three|four|five|six|seven|eight|nine|ten|\d+)\s+"
                         r"(limits?|settings?|fields?|options?|knobs?)\s+"
                         r"(are|is)\b[^.]*\bSettings\b", re.I)
        for where, text in self._help_blobs():
            for m in pat.finditer(text):
                offenders.append(f"{where}: {m.group(0)[:80]!r}")
        self.assertEqual(offenders, [], "\n".join([
            "", "Help copy counts the settings on a screen it cannot see:",
            *[f"  {o}" for o in offenders],
            "A count goes stale the moment a field is added or removed. Say what",
            "the feature does instead."]))


class TestRenderedFieldsAgreeWithTheRegistry(unittest.TestCase):
    """The other half of the NC-003 contract (crm/settings.py's own comment):
    every rendered field must be a real MANAGED_KEYS entry (an unrendered spec
    is not this test's business — read_all() simply wouldn't carry it — but a
    RENDERED spec for an absent key is a field that posts and silently 404s,
    the exact defect NC-004 already fixed once elsewhere)."""

    def _managed_keys(self):
        sys.path.insert(0, os.path.join(REPO, "crm"))
        import settings as settings_mod
        return set(settings_mod.MANAGED_KEYS)

    def test_every_rendered_key_is_a_real_managed_key(self):
        managed = self._managed_keys()
        specs = _all_rendered_specs()
        offenders = sorted({s["key"] for s in specs} - managed)
        self.assertEqual(offenders, [],
                          f"rendered but not in settings.MANAGED_KEYS: {offenders}")

    def test_no_rendered_key_is_duplicated_within_a_page(self):
        """One place to set a thing per page. (NC_AUTOSYNC is on both pages on
        purpose: a switch in the Data page's "Needs you" list and its own row on
        Settings; that is one per page, not two on one.)"""
        for name, path in (("Settings", ABILITIES_PY), ("Data", LEDGER_PY)):
            keys = [s["key"] for s in _specs_in_file(path)]
            dupes = sorted({k for k in keys if keys.count(k) > 1})
            with self.subTest(page=name):
                self.assertEqual(dupes, [], f"drawn more than once on the {name} page: {dupes}")


class TestHiddenContactsLeftSettingsForPeople(unittest.TestCase):
    """NC-003's fourth piece: "Hidden contacts" was never a setting — it hides
    names in the network, which is data management — so it moved to People.
    Checked from both ends: Settings must carry none of the exclude-list
    machinery any more, and People must carry all of it."""

    def test_settings_js_has_no_exclude_list_code_left(self):
        js = _read_settings_js()
        for needle in ("ExcludeList", "/api/exclude-list", "exclude-list"):
            self.assertNotIn(needle, js,
                              f"{needle!r} still in settings.js — Hidden contacts "
                              "was supposed to move to People entirely")

    def test_people_js_carries_the_hidden_contacts_panel(self):
        people_js = os.path.join(REPO, "static", "app", "people.js")
        with io.open(people_js, encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("HiddenNamesPanel", js)
        self.assertIn("/api/exclude-list", js)
        # mounted, not merely defined — a component nobody renders is the
        # exact defect NC-040's SoonStrip regression test elsewhere guards.
        self.assertIn("<${HiddenNamesPanel}", js)


if __name__ == "__main__":
    unittest.main()

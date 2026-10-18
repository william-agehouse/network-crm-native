"""NC-174 gate: a line you can edit that changes nothing is worse than no line.

Two of Ask's tool schemas declared ``queries`` twice. Python keeps the last one
and throws the first away without a word, so the first description — the one at
the top, the one anybody tuning the wording would find and edit — was never sent
to the model and never would be.

It changed no behaviour, because both descriptions happened to say the same
thing. That is not what makes it worth a gate. What makes it worth a gate is
what it does to the next person: they edit the visible wording, ship it, measure
no difference, and conclude the wording does not matter. Replace the first
description with a marker and the schema the model receives still reads:

    search_messages      SEVERAL literal phrases, matched as any-of, in ONE ...
    people_in_messages   SEVERAL literal phrases, matched as any-of, in ONE ...

— the marker nowhere in sight. With the duplicate gone, the same edit lands.

The interpreter cannot help here: a duplicate key in a literal is legal Python.
``ast`` can see it, because the syntax tree keeps both keys and only the dict
that gets built loses one. So the check is over the source, not the object.
"""
import ast
import collections
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base  # noqa: E402,F401

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import chat  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# packaging/dist is a build artefact — a copy of crm/ plus a whole CPython,
# whose own stdlib has deliberate duplicates (encodings/mac_arabic.py).
SEARCHED = ("crm", "tools", "tests")


def python_files():
    for top in SEARCHED:
        for dirpath, dirnames, files in os.walk(os.path.join(REPO, top)):
            dirnames[:] = [d for d in dirnames if d != "__pycache__"]
            for name in sorted(files):
                if name.endswith(".py"):
                    yield os.path.join(dirpath, name)


def duplicate_keys(path):
    """Every constant key a dict literal in this file declares more than once."""
    with open(path, encoding="utf-8") as fh:
        tree = ast.parse(fh.read(), filename=path)
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        keys = [k.value for k in node.keys
                if isinstance(k, ast.Constant) and isinstance(k.value, (str, int))]
        for key, times in collections.Counter(keys).items():
            if times > 1:
                found.append((node.lineno, key, times))
    return found


class TestNothingIsWrittenTwiceAndThrownAway(unittest.TestCase):

    def test_no_dict_literal_declares_the_same_key_twice(self):
        offenders = ["%s:%d  %r written %d times"
                     % (os.path.relpath(path, REPO), line, key, times)
                     for path in python_files()
                     for line, key, times in duplicate_keys(path)]
        self.assertEqual(
            offenders, [],
            "A duplicate key is legal Python and silently discards the earlier "
            "value. Whatever was written there can be edited forever with no "
            "effect:\n  " + "\n  ".join(offenders))

    def test_the_sweep_covers_the_code_that_ships(self):
        looked_at = set(os.path.relpath(f, REPO) for f in python_files())
        for must in ("crm/chat.py", "crm/api_ask.py", "crm/app.py", "tests/helpers.py"):
            self.assertIn(must, looked_at,
                          "a sweep that skips the file the bug was in reports "
                          "a clean repo for the wrong reason")

    def test_the_check_can_see_a_duplicate(self):
        """Proof the sweep above is looking, rather than finding nothing."""
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
            fh.write('D = {"a": 1, "b": 2, "a": 3}\n')
            path = fh.name
        self.addCleanup(os.remove, path)
        self.assertEqual(duplicate_keys(path), [(1, "a", 2)])


class TestEveryToolDescriptionReachesTheModel(unittest.TestCase):
    """The specific consequence: what is written is what gets sent."""

    def schemas_in_the_source(self):
        with open(os.path.join(REPO, "crm", "chat.py"), encoding="utf-8") as fh:
            tree = ast.parse(fh.read())
        out = {}
        for node in ast.walk(tree):
            if not isinstance(node, ast.Dict):
                continue
            pairs = {k.value: v for k, v in zip(node.keys, node.values)
                     if isinstance(k, ast.Constant) and isinstance(k.value, str)}
            if "name" not in pairs or "input_schema" not in pairs:
                continue
            name = pairs["name"]
            if not isinstance(name, ast.Constant):
                continue
            schema = {k.value: v for k, v in zip(pairs["input_schema"].keys,
                                                 pairs["input_schema"].values)
                      if isinstance(k, ast.Constant)}
            props = schema.get("properties")
            if props is None:
                continue
            out[name.value] = [k.value for k in props.keys
                               if isinstance(k, ast.Constant)]
        return out

    @staticmethod
    def losses(written, sent):
        """Tools whose built schema carries fewer properties than were typed."""
        out = []
        for name, keys in sorted(written.items()):
            if name in sent and len(keys) != len(sent[name]):
                out.append("%s: %d written, %d sent (%s)"
                           % (name, len(keys), len(sent[name]),
                              sorted(set(k for k in keys if keys.count(k) > 1))))
        return out

    def test_every_property_written_into_a_tool_schema_survives(self):
        written = self.schemas_in_the_source()
        self.assertIn("search_messages", written, "the source scan found no tools")
        sent = {t["name"]: sorted(t["input_schema"].get("properties", {}))
                for t in chat.TOOLS}

        self.assertEqual(self.losses(written, sent), [],
                         "\n".join(self.losses(written, sent)))

    def test_the_comparison_can_see_a_loss(self):
        """Proof the comparison above reports nothing because there is nothing."""
        self.assertEqual(self.losses({"t": ["a", "b"]}, {"t": ["a", "b"]}), [])
        self.assertEqual(
            self.losses({"t": ["a", "a", "b"]}, {"t": ["a", "b"]}),
            ["t: 3 written, 2 sent (['a'])"])

    def test_the_tool_list_is_actually_being_read(self):
        written = self.schemas_in_the_source()
        self.assertGreaterEqual(len(written), 5)
        self.assertIn("queries", written["search_messages"],
                      "this is the key NC-174 was about; if it is gone, the "
                      "scan is looking at the wrong dicts")


if __name__ == "__main__":
    unittest.main()

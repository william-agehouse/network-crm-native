"""NC-124 gate: a figure labelled "on disk" has to mean the disk.

The Data page rendered `{ label: "on disk", value: humanBytes(diag.disk_bytes) }`
over `crm_db_bytes + archive_db_bytes` — the two databases and nothing else in
the folder. The figure left out most of the folder, including the photos and the
message-search vectors, which are exactly the two things a person weighs up
before deciding whether to keep them. A storage figure that leaves out the
storage you might reclaim is worse than no figure.

Nothing was wrong with the arithmetic. The label was a claim the sum did not
make, which is the NC-076 shape: a true number under a sentence about something
else.

Two decisions are pinned here beyond the total.

  * ALLOCATED blocks, not `st_size`. "On disk" is what `du` reports. Apparent
    size comes out megabytes lighter on a folder of many small files, because
    thousands of photo files each round up to a block. `st_blocks * 512` matches
    `du -skL` to the byte.

  * Symlinks counted, never followed. `data/` can itself be a symlink to a
    folder kept elsewhere, and a walk that followed links could wander out of
    the folder it claims to be measuring, or loop forever.
"""
import os
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, app, code_of, js_code_of  # noqa: E402,F401

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "crm"))
import api_instance  # noqa: E402
import config  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
APP = os.path.join(REPO, "static", "app")


def du_bytes(path):
    """What `du` says, which is the standard the label is held to."""
    out = subprocess.run(["du", "-skL", path], capture_output=True, text=True,
                         check=True).stdout.split()[0]
    return int(out) * 1024


class TestTheFigureCountsTheWholeFolder(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="crm_disk_")
        self.patch = mock.patch.object(config, "DATA_DIR", self.dir)
        self.patch.start()
        self.addCleanup(self.patch.stop)

    def write(self, rel, size):
        path = os.path.join(self.dir, rel)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as fh:
            fh.write(b"x" * size)
        return path

    def test_it_agrees_with_du_to_the_byte(self):
        """Within a megabyte of what du reports would do. Counting blocks rather
        than apparent size makes it exact, so that is what it is held to."""
        self.write("crm.db", 300_000)
        self.write("archive.db", 1_200_000)
        self.write("photos/1.jpg", 40_000)
        self.write("photos/2.jpg", 40_000)
        self.write("chunk_vectors.npz", 900_000)
        total, _parts = api_instance.data_dir_bytes()
        self.assertEqual(total, du_bytes(self.dir))

    def test_the_files_the_old_sum_left_out_are_counted(self):
        self.write("crm.db", 10_000)
        self.write("archive.db", 10_000)
        databases, _ = api_instance.data_dir_bytes()
        self.write("photos/a.jpg", 5_000_000)
        self.write("chunk_vectors.npz", 3_000_000)
        self.write("linkedin_messages.db", 1_000_000)
        after, parts = api_instance.data_dir_bytes()
        self.assertGreater(after, databases + 8_000_000,
                           "photos and the search vectors are missing from the "
                           "figure again — most of the folder")
        for name in ("photos", "chunk_vectors.npz", "linkedin_messages.db"):
            self.assertIn(name, parts,
                          f"{name} is not in the breakdown, so nothing can say "
                          "what the total is made of")

    def test_it_counts_blocks_and_not_apparent_size(self):
        """Fifty one-byte files occupy fifty blocks, not fifty bytes. On a
        folder of thousands of photos the difference is megabytes."""
        for i in range(50):
            self.write(f"photos/{i}.jpg", 1)
        total, _ = api_instance.data_dir_bytes()
        self.assertGreater(total, 50,
                           "the figure is summing st_size, so it under-reports "
                           "a folder full of small files — which is precisely "
                           "what the photos folder is")
        self.assertEqual(total, du_bytes(self.dir))

    def test_a_symlink_is_counted_as_a_link_and_never_followed(self):
        outside = tempfile.mkdtemp(prefix="crm_outside_")
        with open(os.path.join(outside, "huge.bin"), "wb") as fh:
            fh.write(b"y" * 4_000_000)
        os.symlink(outside, os.path.join(self.dir, "elsewhere"))
        total, parts = api_instance.data_dir_bytes()
        self.assertLess(total, 1_000_000,
                        "the walk followed a symlink out of the data folder and "
                        "billed the user for a directory somewhere else on the "
                        "disk")
        self.assertNotIn("huge.bin", parts)

    def test_a_loop_of_symlinks_does_not_hang_the_data_page(self):
        os.symlink(self.dir, os.path.join(self.dir, "self"))
        total, _ = api_instance.data_dir_bytes()   # must return, not recurse
        self.assertIsInstance(total, int)

    def test_an_unreadable_folder_is_skipped_rather_than_raising(self):
        self.write("crm.db", 10_000)
        locked = os.path.join(self.dir, "locked")
        os.makedirs(locked)
        self.write("locked/secret.bin", 10_000)
        os.chmod(locked, 0o000)
        try:
            total, _ = api_instance.data_dir_bytes()
            self.assertGreater(total, 0,
                               "one unreadable folder took the whole figure "
                               "down with it — a diagnostic must not be the "
                               "thing that breaks the page it is on")
        finally:
            os.chmod(locked, 0o700)

    def test_a_missing_data_folder_answers_zero(self):
        with mock.patch.object(config, "DATA_DIR",
                               os.path.join(self.dir, "not-there")):
            self.assertEqual(api_instance.data_dir_bytes(), (0, {}))


class TestTheLabelAndTheNumberStillAgree(Base):
    def test_the_endpoint_reports_a_file_that_is_neither_database(self):
        """Behavioural, not a grep.

        A gate that asserted the source does not contain the literal
        `disk_bytes = crm_db_bytes + archive_db_bytes`, and that the payload has
        a `disk_parts` key, is walked past by a mutation that restores the old
        sum as `disk_bytes, disk_parts = crm_db_bytes + archive_db_bytes, {}` —
        different spelling, and the key still there, just empty. Grep for a
        spelling and you test the spelling.
        """
        with tempfile.TemporaryDirectory(prefix="crm_disk_") as d:
            with open(os.path.join(d, "chunk_vectors.npz"), "wb") as fh:
                fh.write(b"v" * 2_000_000)
            with mock.patch.object(config, "DATA_DIR", d):
                diag = app.handle_diagnostics_get(self.conn)
        self.assertGreaterEqual(
            diag["disk_bytes"], 2_000_000,
            "the vectors file is missing from the 'on disk' figure, and it can "
            "be one of the largest files in the folder")
        self.assertIn("chunk_vectors.npz", diag["disk_parts"],
                      "the breakdown does not name the file it counted")

    def test_the_figure_follows_the_folder_and_nothing_else(self):
        """Empty folder, zero on disk — whatever the databases weigh.

        Under this suite the test database deliberately lives outside the data
        folder, which is what makes this readable: the figure answers for the
        folder it was asked about and is not a function of `crm_db_bytes` at
        all. Restore the old sum and this returns the size of crm.db instead.
        """
        with tempfile.TemporaryDirectory(prefix="crm_disk_") as d:
            with mock.patch.object(config, "DATA_DIR", d):
                empty = app.handle_diagnostics_get(self.conn)
            with open(os.path.join(d, "photos_placeholder.bin"), "wb") as fh:
                fh.write(b"p" * 3_000_000)
            with mock.patch.object(config, "DATA_DIR", d):
                filled = app.handle_diagnostics_get(self.conn)
        self.assertEqual(empty["disk_bytes"], 0,
                         "an empty data folder is reporting bytes from files "
                         "that are not in it")
        self.assertGreaterEqual(filled["disk_bytes"], 3_000_000)

    def test_the_page_still_calls_it_on_disk(self):
        """If the figure ever narrows again, this label is the thing that has to
        change with it — 'on disk' is the honest question, and the answer now
        matches it. The Data page's "On disk" line draws the ledger's disk
        total, and the ledger takes it from the whole-folder walk."""
        js = js_code_of(os.path.join(APP, "data.js"))
        i = js.index('label="On disk"')
        self.assertIn("install.disk?.bytes", js[i:i + 120],
                      "the 'On disk' figure is being fed from something other "
                      "than the whole-folder total again")
        py = code_of(os.path.join(REPO, "crm", "api_ledger.py"))
        self.assertIn("api_instance.data_dir_bytes", py,
                      "the ledger no longer gets its disk total from the "
                      "whole-folder walk")


if __name__ == "__main__":
    unittest.main()

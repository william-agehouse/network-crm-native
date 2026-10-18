"""The export builders (crm/export_data.py)."""

import csv
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import (  # noqa: E402
    Base, db, ex,
)


class TestExport(Base):
    NOTE_TEXT = "Met at a chess meetup in Leeds."

    def _seed(self):
        rich = db.create_contact(self.conn, "Ada Lovelace", source="linkedin")
        db.set_field(self.conn, rich, "company", "Analytical Engines")
        db.set_field(self.conn, rich, "category", "professional")
        self.conn.execute(
            "UPDATE contacts SET msg_count=12, imsg_count=4, wa_count=1, "
            "last_contact='2026-05-01 00:00:00' WHERE id=?",
            (rich,),
        )
        db.add_note(self.conn, rich, self.NOTE_TEXT)
        plain = db.create_contact(self.conn, "Bob Plain")
        self.conn.commit()
        return rich, plain

    def test_markdown_has_names_and_note(self):
        self._seed()
        md = ex.export_markdown(self.conn)
        self.assertIn("Ada Lovelace", md)
        self.assertIn("Bob Plain", md)
        self.assertIn(self.NOTE_TEXT, md)

    def test_csv_header_and_one_row_per_contact(self):
        self._seed()
        rows = list(csv.reader(io.StringIO(ex.export_csv(self.conn))))
        self.assertEqual(rows[0], ex.CSV_COLUMNS)
        self.assertEqual(len(rows) - 1, 2)  # header + one row per contact
        names = {r[0] for r in rows[1:]}
        self.assertEqual(names, {"Ada Lovelace", "Bob Plain"})

    def test_json_parses_with_nested_notes(self):
        self._seed()
        data = json.loads(ex.export_json(self.conn))
        self.assertIn("exported_at", data)
        self.assertEqual(len(data["contacts"]), 2)
        ada = next(c for c in data["contacts"] if c["name"] == "Ada Lovelace")
        self.assertEqual(len(ada["notes"]), 1)
        self.assertEqual(ada["notes"][0]["body"], self.NOTE_TEXT)
        self.assertIn("identifiers", ada)

    def test_excluded_contacts_skipped_by_default(self):
        _, plain = self._seed()
        db.set_excluded(self.conn, plain, 1)
        self.conn.commit()
        self.assertNotIn("Bob Plain", ex.export_markdown(self.conn))
        self.assertIn("Bob Plain", ex.export_markdown(self.conn, include_excluded=True))

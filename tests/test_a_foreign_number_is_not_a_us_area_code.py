"""NC-252. A foreign number does not give anyone a US city.

The offline pass read a US area code from the first three digits of any number
of ten or more digits, whatever its country code: a Dutch landline "+31 20..."
became Chicago (312). Only a number written with +1, or with no country code at
all, can be read that way now, and the cities the old reading wrote are cleared
once.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base, db  # noqa: E402

import enrich_facts as ef  # noqa: E402

DUTCH = "+31201234567"    # read as 312, Chicago, until NC-252


class TestTheAreaCodeReader(unittest.TestCase):

    def test_a_foreign_number_has_no_us_area_code(self):
        for number in (DUTCH, "+46701740605", "+33612345678", "+41791234567"):
            with self.subTest(number=number):
                self.assertIsNone(ef._extract_area_code(number))

    def test_a_us_number_still_does(self):
        self.assertEqual(ef._extract_area_code("+1 212 555 0100"), 212)
        self.assertEqual(ef._extract_area_code("+12125550100"), 212)

    def test_a_number_without_a_country_code_keeps_the_old_reading(self):
        # Ambiguous, and a US number stored this way is common.
        self.assertEqual(ef._extract_area_code("650 555 0100"), 650)


class TestAForeignCityIsForgotten(Base):

    def _person(self, name, phones, *, metro="Chicago", source="offline", manual=""):
        cid = db.create_contact(self.conn, name)
        self.conn.execute(
            "UPDATE contacts SET home_metro = ?, home_city = ?, home_country = 'US', "
            "facts_source = ?, manual_fields = ? WHERE id = ?",
            (metro, metro, source, manual, cid))
        for p in phones:
            db.add_identifier(self.conn, cid, "phone", p, source="contacts")
        self.conn.commit()
        return cid

    def _metro(self, cid):
        return db.get_contact(self.conn, cid)["home_metro"]

    def test_the_offline_pass_gives_a_foreign_number_no_city(self):
        cid = db.create_contact(self.conn, "Dutch Only")
        db.add_identifier(self.conn, cid, "phone", DUTCH, source="contacts")
        self.conn.commit()
        self.assertNotIn("home_metro", ef.enrich_pass_a(self.conn, cid))

    def test_a_city_read_from_a_foreign_number_is_cleared(self):
        cid = self._person("Dutch", [DUTCH])
        self.assertEqual(ef.forget_cities_from_foreign_numbers(self.conn), 1)
        r = db.get_contact(self.conn, cid)
        self.assertEqual((r["home_metro"], r["home_city"], r["home_country"]), ("", "", ""))

    def test_everything_else_is_left_alone(self):
        kept = {
            "also has a real Chicago number": self._person("Both", [DUTCH, "+13125550100"]),
            "typed in": self._person("Typed", [DUTCH], manual='["home_metro"]'),
            "from the AI pass": self._person("Llm", [DUTCH], source="llm"),
            "no country code": self._person("Local", ["3125550100"]),
        }
        self.assertEqual(ef.forget_cities_from_foreign_numbers(self.conn), 0)
        for why, cid in kept.items():
            with self.subTest(why=why):
                self.assertEqual(self._metro(cid), "Chicago")

    def test_it_runs_when_the_app_starts_and_once(self):
        cid = self._person("Dutch", [DUTCH])
        self.conn.close()
        db.init_db(self.path)
        self.conn = db.get_db(self.path)
        self.assertEqual(self._metro(cid), "")
        self.assertIn('"cleared": 1', db.state_get(self.conn, ef.CITY_FROM_FOREIGN_NUMBER_KEY, ""))
        self.assertEqual(ef.forget_cities_from_foreign_numbers(self.conn), 0)


if __name__ == "__main__":
    unittest.main()

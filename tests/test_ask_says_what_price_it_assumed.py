"""NC-195. The cost under an Ask answer names the prices it assumed, and a
corrected price applies without a restart.

`chat.PRICE_IN` / `PRICE_OUT` were read once, at import. If Anthropic's prices
moved, every cost the app showed was wrong, nothing on screen said which price it
had used, and fixing secrets/.env did nothing until the app restarted.
"""

import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from tests.helpers import Base  # noqa: E402,F401 — CRM_DATA_DIR bootstrap

import chat  # noqa: E402

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def one_million_each():
    spend = chat._Spend()
    spend.add(mock.Mock(usage=mock.Mock(input_tokens=1_000_000, output_tokens=1_000_000,
                                        cache_read_input_tokens=0,
                                        cache_creation_input_tokens=0)))
    return spend


class TestThePriceIsReadWhenTheAnswerIsCosted(unittest.TestCase):

    def test_the_defaults(self):
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("NC_PRICE_IN_PER_MTOK", None)
            os.environ.pop("NC_PRICE_OUT_PER_MTOK", None)
            report = one_million_each().report()
        self.assertEqual(report["usd"], 24.0)
        self.assertEqual((report["price_in_per_mtok"], report["price_out_per_mtok"]),
                         (4.0, 20.0))

    def test_a_changed_price_applies_without_a_restart(self):
        with mock.patch.dict(os.environ, {"NC_PRICE_IN_PER_MTOK": "3",
                                          "NC_PRICE_OUT_PER_MTOK": "15"}):
            report = one_million_each().report()
        self.assertEqual(report["usd"], 18.0)
        self.assertEqual(report["price_in_per_mtok"], 3.0)
        self.assertEqual(report["price_out_per_mtok"], 15.0)

    def test_a_nonsense_price_falls_back_rather_than_poisoning_every_cost(self):
        for bad in ("five", "-2", "inf", "nan", "99999"):
            with self.subTest(bad=bad), mock.patch.dict(
                    os.environ, {"NC_PRICE_IN_PER_MTOK": bad}):
                self.assertEqual(chat.price_in(), chat.PRICE_IN_DEFAULT)

    def test_nothing_captures_the_price_at_import_any_more(self):
        self.assertFalse(hasattr(chat, "PRICE_IN"))
        self.assertFalse(hasattr(chat, "PRICE_OUT"))


class TestTheAnswerSaysWhichPriceItUsed(unittest.TestCase):
    def test_the_detail_line_prints_both_prices(self):
        with open(os.path.join(REPO, "static", "app", "ask.js"), encoding="utf-8") as fh:
            js = fh.read()
        self.assertIn("spend.price_in_per_mtok", js)
        self.assertIn("spend.price_out_per_mtok", js)
        self.assertIn("per million tokens", js)


if __name__ == "__main__":
    unittest.main()

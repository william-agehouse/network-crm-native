"""Put the crm/ package dir on sys.path so tests can `import db`, `import app`, etc.

Auto-loaded by pytest only; the per-file shim covers unittest runs. Importing
tests.helpers here does the real work — it sets CRM_DATA_DIR to a temp dir before
any crm module resolves a path under data/. See the warning in tests/helpers.py.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "crm"))

from tests import helpers  # noqa: E402,F401 — imported for the CRM_DATA_DIR bootstrap

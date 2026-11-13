import copy
import json
import os
import shutil
import tempfile

from wfdp import build_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CONFIGS = os.path.join(ROOT, "configs")


def load_config(name):
    with open(os.path.join(CONFIGS, name + ".json"), encoding="utf-8") as fh:
        return json.load(fh)


def loan_config():
    return copy.deepcopy(load_config("loan_approval"))


def loan(**overrides):
    payload = {"applicant_id": "cust000", "age": 34, "income": 90000,
               "loan_amount": 150000, "employment": "salaried"}
    payload.update(overrides)
    return payload


def vendor(**overrides):
    payload = {"vendor_id": "VND-100", "vendor_name": "Acme Logistics", "country": "IN",
               "category": "logistics", "years_in_business": 8,
               "annual_revenue": 5000000, "contract_value": 600000}
    payload.update(overrides)
    return payload


def make_app(testcase, sleeps=None):
    """A fresh app on a temporary database; backoff sleeps are recorded, not slept."""
    tmp = tempfile.mkdtemp()
    testcase.addCleanup(shutil.rmtree, tmp, ignore_errors=True)
    record = sleeps.append if sleeps is not None else (lambda seconds: None)
    return build_app(db_path=os.path.join(tmp, "test.db"), sleep=record)

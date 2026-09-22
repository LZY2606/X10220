import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from nmrwb import db
from nmrwb.service import ProjectService
from app import set_clock


@pytest.fixture
def clock():
    return db.Clock(start=1_700_000_000.0)


@pytest.fixture
def svc(tmp_path, clock):
    conn = db.connect(str(tmp_path / "test.db"))
    service = ProjectService(conn, clock)
    service.create_project("P1", "case")
    conn.commit()
    set_clock(clock)
    return service


CASE_DIR = ROOT / "data" / "case001"

CASE_FILES = {
    "S1H": ("1h", "1h_ethylbenzene.txt"),
    "S13C": ("13c", "13c_ethylbenzene.txt"),
    "COSY": ("cosy", "cosy.txt"),
    "HSQC": ("hsqc", "hsqc.txt"),
    "HMBC": ("hmbc", "hmbc.txt"),
    "SKEL": ("skeleton", "skeleton_ethylbenzene.txt"),
    "RULES": ("rules", "rules_v1.txt"),
}


def load_case_text():
    return {sid: (CASE_DIR / fn).read_text()
            for sid, (_, fn) in CASE_FILES.items()}


@pytest.fixture
def loaded_svc(svc):
    text = load_case_text()
    for sid, (kind, fn) in CASE_FILES.items():
        svc.submit_source("P1", sid, kind, fn, text[sid], "importer")
    return svc

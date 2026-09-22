import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

import pytest

from nmrb.repository import Repo
from nmrb.service import ProjectService
from nmrb.storage import connect
from nmrb.timeutil import Clock


@pytest.fixture
def db_path(tmp_path):
    return str(tmp_path / "test_nmrb.db")


@pytest.fixture
def service(db_path):
    conn = connect(db_path)
    repo = Repo(conn, Clock("2026-03-01T00:00:00+00:00"))
    svc = ProjectService(repo)
    repo.create_project("p1", "测试项目")
    yield svc
    conn.close()


@pytest.fixture
def case_dir():
    return pathlib.Path(__file__).resolve().parents[1] / "nmrb" / "case"

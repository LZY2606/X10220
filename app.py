"""演示入口：.venv/bin/uvicorn app:app --host 127.0.0.1 --port 5340"""
import os

from nmrwb.api import create_app
from nmrwb.core import Store

DB_PATH = os.environ.get("NMRWB_DB", "workbench.db")
app = create_app(Store(DB_PATH))

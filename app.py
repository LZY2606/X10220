import os

from nmrwb.api import create_app

app = create_app(os.environ.get("NMRWB_DB", os.path.join(os.path.dirname(__file__), "nmrwb.db")))

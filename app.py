import os

from nmrwb.web import create_app

app = create_app(db_path=os.environ.get("NMRWB_DB", "workbench.db"))

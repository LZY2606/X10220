import os

from nmrworkbench.api import create_app

app = create_app(os.environ.get("NMR_DB", "workbench.db"))

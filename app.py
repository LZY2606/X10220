"""Entry point so the fixed demo command `.venv/bin/uvicorn app:app` works
from the repository root."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from nmrworkbench.app import app  # noqa: E402,F401

"""Repo-local interpreter bootstrap (kept minimal).

The prescribed environment is ``.venv`` (see requirements.txt), where exact,
mutually-compatible versions of pytest/numpy/fastapi are installed.  This hook
only asks an otherwise-broken *global* interpreter to use the pure-Python
protobuf runtime; it has no effect inside the clean virtual environment.
"""
import os

os.environ.setdefault("PROTOCOL_BUFFERS_PYTHON_IMPLEMENTATION", "python")

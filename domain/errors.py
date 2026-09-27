"""Domain-level errors. Format errors always carry input location markers."""

from __future__ import annotations


class WorkbenchError(Exception):
    pass


class FormatErrors(WorkbenchError):
    """One or more parse/validation errors in a submitted source document."""

    def __init__(self, errors: list[dict], doc_id: str | None = None):
        self.errors = errors
        self.doc_id = doc_id
        super().__init__("; ".join(f"{e['code']}@{e['loc']}" for e in errors))


class NotFound(WorkbenchError):
    pass


class Conflict(WorkbenchError):
    pass


class StateError(WorkbenchError):
    pass

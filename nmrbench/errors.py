"""Typed domain errors. They carry row/section coordinates for the importer."""
from __future__ import annotations


class NMRBenchError(Exception):
    pass


class ImportFormatError(NMRBenchError):
    """Hard format error with precise file/section/line coordinates."""

    def __init__(self, message: str, *, file: str | None = None,
                 section: str | None = None, line: int | None = None,
                 raw: str | None = None):
        self.file = file
        self.section = section
        self.line = line
        self.raw = raw
        loc = "".join(
            f" {part}"
            for part in (
                f"file={file}" if file else "",
                f"section={section}" if section else "",
                f"line={line}" if line is not None else "",
            )
        )
        super().__init__(f"{message} [{loc.strip()}]" if loc else message)


class StateError(NMRBenchError):
    """Illegal state-machine transition."""


class ConflictError(NMRBenchError):
    """Two editors reached incompatible decisions from the same base."""

    def __init__(self, message: str, *, conflict_event_id: str | None = None,
                 details: dict | None = None):
        self.conflict_event_id = conflict_event_id
        self.details = details or {}
        super().__init__(message)


class NotFoundError(NMRBenchError):
    pass

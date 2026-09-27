"""NMR assignment workbench: chemical shifts, coupling candidates, evidence room.

Event-sourced NMR peak-assignment workbench. Raw peak lists are immutable,
every derived value carries algorithm version / parameter fingerprint /
direct provenance, and all human decisions fork branches in an append-only
event log that can be replayed to any historical point.
"""

__all__ = ["store", "importer", "engine", "services", "casepack", "clock"]

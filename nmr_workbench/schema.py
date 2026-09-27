SOURCE_SCHEMA = "nmr-source/1"
ALGORITHM_VERSION = "nmr-candidates/2026.09"
ARCHIVE_SCHEMA = "nmr-counterexample-archive/1"

DEFAULT_PARAMETERS = {
    "schema": "nmr-parameters/1",
    "version": "params-v1",
    "shift_tolerance": {"1H": 0.25, "13C": 2.5},
    "reference_offsets": {"1H": 0.0, "13C": 0.0},
    "weights": {"shift": 0.45, "edge": 0.45, "integral": 0.10},
    "minimum_score": 0.30,
    "notes": "Fixed deterministic baseline parameters.",
}

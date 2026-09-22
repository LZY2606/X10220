SOURCE_KINDS = ("peaks_1h", "peaks_13c", "cosy", "hsqc", "hmbc", "molecule")
EDGE_KINDS = ("cosy", "hsqc", "hmbc")

POLICY_OVERLAP_NO_FORCE = "POL-001"
POLICY_SYMMETRY_EQUIVALENCE = "POL-002"
POLICY_MISSING_EDGE_EVIDENCE_ONLY = "POL-003"
POLICY_REFERENCE_BIAS_STALE = "POL-004"
POLICY_TIED_CANDIDATES = "POL-005"
POLICY_EDITOR_CONFLICT = "POL-006"
POLICY_RAW_REPRESENTATION_PRESERVED = "POL-007"
POLICY_LOCAL_DEPENDENCY_STALE = "POL-008"
POLICY_REPLAY_HISTORY = "POL-009"
POLICY_UNCONVERTED_KEEP_IN_PLACE = "POL-010"

POLICIES = {
    POLICY_OVERLAP_NO_FORCE: "Overlapping or missing peaks must not be force-matched to a single assignment; every legal candidate is retained.",
    POLICY_SYMMETRY_EQUIVALENCE: "Atoms in an equivalence group are chemically indistinguishable; tied candidates are kept together.",
    POLICY_MISSING_EDGE_EVIDENCE_ONLY: "A missing COSY/HSQC/HMBC edge is evidence only; it never hard-rejects a candidate.",
    POLICY_REFERENCE_BIAS_STALE: "A global chemical-shift offset caused by solvent/temperature/reference change invalidates only dependent candidates; old decisions remain replayable.",
    POLICY_TIED_CANDIDATES: "Joint-best (tied) candidates are both first-class; no arbitrary tie-break is performed.",
    POLICY_EDITOR_CONFLICT: "Contradictory decisions from two editors are recorded as conflicts and resolved explicitly on a new branch.",
    POLICY_RAW_REPRESENTATION_PRESERVED: "All domain quantities preserve the original file representation and units/reference basis; normalization only happens in the derived layer.",
    POLICY_LOCAL_DEPENDENCY_STALE: "Changing one direct source only invalidates derived results that depend on it.",
    POLICY_REPLAY_HISTORY: "Replaying to a past event restores the dependency versions and visible conclusions of that moment; history is never overwritten by newer computation.",
    POLICY_UNCONVERTIBLE_KEEP_IN_PLACE: "Values that cannot be converted stay at their original position with a diagnostic; they are not dropped for plotting.",
}

STATUS_CURRENT = "current"
STATUS_EXPIRED = "expired"
STATUS_SUPERSEDED = "superseded"
STATUS_UNKNOWN = "unknown"

DECISION_ACCEPT = "accept"
DECISION_REJECT = "reject"
DECISION_PARAM_REVISION = "param_revision"
DECISION_RETRACT = "retract"
DECISION_RESOLVE_CONFLICT = "resolve_conflict"

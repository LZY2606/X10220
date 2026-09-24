"""Offline case pack: "NMR 共振经纬：化学位移、耦合候选与归属证据室".

Compound: 2,4-dimethylpentan-3-ol, (CH3)2CH-CH(OH)-CH(CH3)2.
The C2 symmetry gives equivalent atom pairs (tied candidates), the two
methyl doublets at 0.92/0.96 ppm overlap (no forced unique match), and
one expected HMBC edge is absent (evidence stays unknown, never a
rejection). The scripted scenario then performs one parameter revision
(reference change -> global shift bias), one local source edit and one
pair of conflicting human decisions, plus a second branch that removes
a peak (missing-peak rule).
"""
from __future__ import annotations

from .service import MAIN, Service

CASE_ID = "nmr-resonance-atlas/v1"
TITLE = "NMR 共振经纬：化学位移、耦合候选与归属证据室"

MOLSKEL_V1 = """!nmrwb/molskel v1
# 2,4-dimethylpentan-3-ol skeleton; coordinates are a 2D layout datum (angstrom, arbitrary origin)
atom C1  C pred_ppm=72.0 x=0.0  y=0.0
atom hC1 H pred_ppm=3.30 attaches=C1 x=0.0  y=1.1
atom C2  C pred_ppm=31.0 x=-1.5 y=0.9
atom hC2 H pred_ppm=1.65 attaches=C2 x=-2.3 y=1.7
atom C3  C pred_ppm=18.6 x=-2.7 y=0.1
atom hC3 H pred_ppm=0.91 attaches=C3 x=-3.6 y=0.7
atom C4  C pred_ppm=19.4 x=-1.9 y=2.3
atom hC4 H pred_ppm=0.96 attaches=C4 x=-1.3 y=3.1
atom C2p C pred_ppm=31.0 x=1.5  y=0.9
atom hC2p H pred_ppm=1.65 attaches=C2p x=2.3  y=1.7
atom C3p C pred_ppm=18.6 x=2.7  y=0.1
atom hC3p H pred_ppm=0.91 attaches=C3p x=3.6  y=0.7
atom C4p C pred_ppm=19.4 x=1.9  y=2.3
atom hC4p H pred_ppm=0.96 attaches=C4p x=1.3  y=3.1
bond C1 C2
bond C1 C2p
bond C2 C3
bond C2 C4
bond C2p C3p
bond C2p C4p
symmetry SG1 C2 C2p
symmetry SG2 C3 C3p
symmetry SG3 C4 C4p
symmetry SG4 hC2 hC2p
symmetry SG5 hC3 hC3p
symmetry SG6 hC4 hC4p
"""

H1_V1 = """!nmrwb/peaklist v1
nucleus: 1H
solvent: CDCl3
frequency_mhz: 400.13
temperature_K: 298.0
reference: TMS
peak P1 shift_ppm=3.31 intensity=0.55 integration=1.00 multiplicity=t
peak P2 shift_ppm=1.66 intensity=0.40 integration=2.00 multiplicity=m
peak P3 shift_ppm=0.92 intensity=0.95 integration=6.00 multiplicity=d overlap=OV1
peak P4 shift_ppm=0.96 intensity=0.93 integration=6.00 multiplicity=d overlap=OV1
edge COSY P1 P2
edge COSY P2 P3
edge COSY P2 P4
edge HSQC P1 PC1
edge HSQC P2 PC2
edge HSQC P3 PC3
edge HSQC P4 PC4
edge HMBC P1 PC2
missing HMBC P3 PC1
"""

# local source edit: integration of P1 corrected, previously missing HMBC added
H1_V2 = """!nmrwb/peaklist v1
nucleus: 1H
solvent: CDCl3
frequency_mhz: 400.13
temperature_K: 298.0
reference: TMS
peak P1 shift_ppm=3.31 intensity=0.55 integration=1.02 multiplicity=t
peak P2 shift_ppm=1.66 intensity=0.40 integration=2.00 multiplicity=m
peak P3 shift_ppm=0.92 intensity=0.95 integration=6.00 multiplicity=d overlap=OV1
peak P4 shift_ppm=0.96 intensity=0.93 integration=6.00 multiplicity=d overlap=OV1
edge COSY P1 P2
edge COSY P2 P3
edge COSY P2 P4
edge HSQC P1 PC1
edge HSQC P2 PC2
edge HSQC P3 PC3
edge HSQC P4 PC4
edge HMBC P1 PC2
edge HMBC P3 PC1
"""

# missing-peak branch: P4 was never observed -> hC4/hC4p stay unassigned
H1_V3 = """!nmrwb/peaklist v1
nucleus: 1H
solvent: CDCl3
frequency_mhz: 400.13
temperature_K: 298.0
reference: TMS
peak P1 shift_ppm=3.31 intensity=0.55 integration=1.02 multiplicity=t
peak P2 shift_ppm=1.66 intensity=0.40 integration=2.00 multiplicity=m
peak P3 shift_ppm=0.92 intensity=0.95 integration=6.00 multiplicity=d overlap=OV1
edge COSY P1 P2
edge COSY P2 P3
edge HSQC P1 PC1
edge HSQC P2 PC2
edge HSQC P3 PC3
edge HMBC P1 PC2
edge HMBC P3 PC1
"""

C13_V1 = """!nmrwb/peaklist v1
nucleus: 13C
solvent: CDCl3
frequency_mhz: 100.62
temperature_K: 298.0
reference: TMS
peak PC1 shift_ppm=72.1 intensity=0.80 integration=1.0 multiplicity=s
peak PC2 shift_ppm=31.1 intensity=0.90 integration=2.0 multiplicity=s
peak PC3 shift_ppm=18.7 intensity=1.00 integration=2.0 multiplicity=s
peak PC4 shift_ppm=19.5 intensity=1.00 integration=2.0 multiplicity=s
"""

PARAMS_V1 = {
    "reference": "TMS", "solvent": "CDCl3", "temperature_K": 298.0,
    "bias_1H_ppm": 0.0, "bias_13C_ppm": 0.0,
    "tol_1H_ppm": 0.15, "tol_13C_ppm": 1.5,
    "w_COSY": 0.20, "w_HSQC": 0.50, "w_HMBC": 0.30, "cutoff": 0.05,
}

# parameter revision: reference standard change -> +0.05 ppm global 1H bias
PARAMS_V2_CHANGE = {"reference": "residual-CHCl3", "bias_1H_ppm": 0.05}

BAD_PEAKLIST = """!nmrwb/peaklist v1
nucleus: 1H
solvent: CDCl3
frequency_mhz: 400.13
peak PX shift_ppm=not_a_number intensity=0.5 integration=1.0 multiplicity=s
"""


def build_case(svc: Service, actor: str = "casepack") -> dict:
    """Replay the full narrative into a service. Returns key handles."""
    log: dict = {"case": CASE_ID, "title": TITLE}
    svc.import_document("DOC-MOL", "molskel", MOLSKEL_V1, actor)
    svc.import_document("DOC-1H", "peaklist", H1_V1, actor)
    svc.import_document("DOC-13C", "peaklist", C13_V1, actor)
    svc.commit_params(PARAMS_V1, affects=["1H", "13C"], actor=actor)
    log["run1"] = svc.run_assignment(MAIN, actor)

    # 1) parameter revision: reference change -> global 1H shift bias
    log["params_v2"] = svc.commit_params(PARAMS_V2_CHANGE, affects=["1H"], actor=actor)
    log["run2"] = svc.run_assignment(MAIN, actor)

    # 2) local source edit: only DOC-1H dependents expire
    log["doc_1h_v2"] = svc.import_document("DOC-1H", "peaklist", H1_V2, actor)
    log["run3"] = svc.run_assignment(MAIN, actor)

    # 3) conflicting human decisions (two editors) + resolution + undo
    d1 = svc.decide(MAIN, "P1", "hC1", "confirm", actor="ada")
    d2 = svc.decide(MAIN, "P1", "hC1", "reject", actor="bob")
    log["decision_ada"] = d1
    log["decision_bob"] = d2
    if d2["conflicts"]:
        svc.resolve_conflict(d2["conflicts"][0], d1["decision_id"], actor="ada")
    svc.undo_decision(d2["decision_id"], actor="bob")

    # second branch: missing-peak rule (P4 absent -> no forced match)
    branch = svc.create_branch("missing-peak:overlap-not-forced", parent=MAIN, actor=actor)
    svc.import_document("DOC-1H", "peaklist", H1_V3, actor)
    log["missing_peak_branch"] = branch
    log["run_missing_peak"] = svc.run_assignment(branch, actor)
    return log

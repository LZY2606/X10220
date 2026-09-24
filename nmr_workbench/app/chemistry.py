"""Molecular skeleton and expected NMR correlation graph.

Sites are resolvable nuclei.  Symmetry-equivalent atoms share one site
(symmetric atoms must never be forced into distinct assignments).
"""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Atom:
    id: str
    element: str
    x: float | None
    y: float | None
    label: str | None = None
    exchangeable: bool = False


@dataclass
class Site:
    label: str                 # stable site id, e.g. "H1"
    nucleus: str               # "1H" | "13C"
    atoms: tuple[str, ...]
    symmetry_rank: int = 1
    exchangeable: bool = False
    prior_shift: float | None = None
    prior_reference: str | None = None

    @property
    def symmetric(self) -> bool:
        return self.symmetry_rank > 1


@dataclass
class ExpectedEdge:
    exp_id: str
    kind: str                  # COSY | HSQC | HMBC
    h_site: str
    other_site: str            # H site for COSY, C site for HSQC/HMBC
    bond_distance: int


@dataclass
class Molecule:
    atoms: dict[str, Atom]
    bonds: list[tuple[str, str, int]]
    sites: dict[str, Site]
    expected: dict[str, list[ExpectedEdge]]  # kind -> edges

    def sites_of(self, nucleus: str) -> list[Site]:
        return [s for s in self.sites.values() if s.nucleus == nucleus]

    def adjacency(self) -> dict[str, set[str]]:
        adj: dict[str, set[str]] = {}
        for a, b, _order in self.bonds:
            adj.setdefault(a, set()).add(b)
            adj.setdefault(b, set()).add(a)
        return adj


def _heavy_path(atom_to_csite: dict[str, str], a_heavy: str,
                b_heavy: str, adj: dict[str, set[str]]) -> int | None:
    """Shortest heavy-atom bond count between two heavy atoms."""
    if a_heavy == b_heavy:
        return 0
    frontier, seen, dist = [a_heavy], {a_heavy}, 0
    while frontier:
        nxt = []
        dist += 1
        for node in frontier:
            for nb in adj.get(node, ()):
                if nb in seen:
                    continue
                if nb == b_heavy:
                    return dist
                seen.add(nb)
                nxt.append(nb)
        frontier = nxt
    return None


def build_molecule(records: list[dict]) -> Molecule:
    """Construct Molecule from parsed molecule source records."""
    atoms: dict[str, Atom] = {}
    bonds: list[tuple[str, str, int]] = []
    equiv_rows, prior_rows = [], []
    for rec in records:
        if rec["kind"] == "atom":
            atoms[rec["id"]] = Atom(rec["id"], rec["element"], rec["x"],
                                    rec["y"], rec.get("label"),
                                    rec.get("exchangeable", False))
        elif rec["kind"] == "bond":
            bonds.append((rec["a1"], rec["a2"], int(rec.get("order") or 1)))
        elif rec["kind"] == "equivalent":
            equiv_rows.append(rec)
        elif rec["kind"] == "prior":
            prior_rows.append(rec)

    adj: dict[str, set[str]] = {}
    for a, b, _o in bonds:
        adj.setdefault(a, set()).add(b)
        adj.setdefault(b, set()).add(a)

    # --- sites -----------------------------------------------------------
    # Every NMR-active heavy atom (13C) and every explicit hydrogen get a
    # default site; equivalent-group rows then merge members into one site.
    atom_site: dict[str, str] = {}
    sites: dict[str, Site] = {}

    def add_site(label: str, nucleus: str, members: list[str],
                 rank: int, exchangeable: bool = False):
        if label in sites:
            raise ValueError(f"duplicate site label {label!r}")
        sites[label] = Site(label, nucleus, tuple(members), rank,
                            exchangeable)
        for a in members:
            atom_site[a] = label

    # carbon sites from atoms
    for atom in atoms.values():
        if atom.element == "C":
            add_site(atom.id, "13C", [atom.id], 1)
    # hydrogen sites
    equiv_group_of: dict[str, dict] = {}
    for row in equiv_rows:
        for a in row["atoms"]:
            equiv_group_of[a] = row
    standalone_h: list[str] = []
    for atom in atoms.values():
        if atom.element != "H":
            continue
        row = equiv_group_of.get(atom.id)
        if row is not None:
            if atom.id == row["atoms"][0] and row["nucleus"] == "1H":
                add_site(row["label"], "1H", list(row["atoms"]),
                         len(row["atoms"]),
                         any(atoms[a].exchangeable for a in row["atoms"]))
        else:
            standalone_h.append(atom.id)
    for aid in standalone_h:
        add_site(aid, "1H", [aid], 1, atoms[aid].exchangeable)

    # priors
    for row in prior_rows:
        site = sites.get(row["site"])
        if site is None:
            continue
        if site.nucleus != row["nucleus"]:
            continue
        if row["shift_q"].get("value") is not None:
            site.prior_shift = row["shift_q"]["value"]
            site.prior_reference = row.get("reference")

    # --- expected correlation graph --------------------------------------
    csite_of = {a: s.label for a, s in
                ((a, s) for s in sites.values() if s.nucleus == "13C"
                 for a in s.atoms)}
    hsite_of = {a: s.label for s in sites.values() if s.nucleus == "1H"
                for a in s.atoms}

    def attached_hydrogens(heavy: str) -> list[str]:
        return [nb for nb in adj.get(heavy, ()) if atoms[nb].element == "H"]

    def heavy_owner(h_atom: str) -> str | None:
        for nb in adj.get(h_atom, ()):
            if atoms[nb].element != "H":
                return nb
        return None

    expected = {"HSQC": [], "COSY": [], "HMBC": []}
    exp_counter = {"HSQC": 0, "COSY": 0, "HMBC": 0}

    def add_expected(kind: str, hlabel: str, other: str, distance: int):
        exp_counter[kind] += 1
        eid = f"EXP-{kind}-{exp_counter[kind]:03d}"
        expected[kind].append(ExpectedEdge(eid, kind, hlabel, other, distance))

    for hsite in sites_of_nucleus(sites, "1H"):
        if hsite.exchangeable:
            continue  # labile protons are not expected to give stable edges
        representative = hsite.atoms[0]
        heavy = heavy_owner(representative)
        if heavy is not None and heavy in csite_of:
            add_expected("HSQC", hsite.label, csite_of[heavy], 1)
        # COSY: vicinal hydrogens (H on adjacent heavy atoms)
        if heavy is not None:
            seen_other_h_sites: set[str] = set()
            for nb_heavy in adj.get(heavy, ()):
                if atoms[nb_heavy].element == "H":
                    continue
                for hh in attached_hydrogens(nb_heavy):
                    other_label = hsite_of.get(hh)
                    if (other_label and other_label != hsite.label
                            and other_label not in seen_other_h_sites):
                        seen_other_h_sites.add(other_label)
                        add_expected("COSY", hsite.label, other_label, 3)
        # HMBC: 2J / 3J C-H couplings
        if heavy is not None:
            seen_targets: dict[str, int] = {}
            # 2J: carbons adjacent to the proton-bearing carbon
            for nb in adj.get(heavy, ()):
                if atoms[nb].element == "C" and nb in csite_of:
                    tgt = csite_of[nb]
                    if tgt != csite_of.get(heavy):
                        seen_targets[tgt] = 2
            # 3J: H-C-C-C
            for c2 in list(adj.get(heavy, ())):
                if atoms.get(c2) and atoms[c2].element != "C":
                    continue
                for c3 in adj.get(c2, ()):
                    if atoms.get(c3) and atoms[c3].element == "C" \
                            and c3 in csite_of and c3 != heavy:
                        tgt = csite_of[c3]
                        seen_targets.setdefault(tgt, 3)
            for tgt, distance in sorted(seen_targets.items()):
                add_expected("HMBC", hsite.label, tgt, distance)

    return Molecule(atoms, bonds, sites, expected)


def sites_of_nucleus(sites: dict[str, Site], nucleus: str) -> list[Site]:
    ordered = [s for s in sites.values() if s.nucleus == nucleus]
    # stable, natural order: explicit labels first then numeric
    def key(s: Site):
        letters = "".join(c for c in s.label if c.isalpha())
        nums = "".join(c for c in s.label if c.isdigit())
        return (letters, int(nums) if nums else 0, s.label)
    return sorted(ordered, key=key)

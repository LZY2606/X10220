"""Direct-source fingerprints, overlap groups and coupling evidence.

The coupling network is *evidence only*.  It never forces a unique
assignment: missing expected edges produce evidence gaps (lower scores /
warnings), not hard rejections, and overlapping peaks are never collapsed
into a single assignment.
"""
from __future__ import annotations

from collections import defaultdict
from typing import Any, Dict, List, Optional, Tuple

from .ids import fingerprint


ROLE_DIRECT = {
    "1H": ["mol", "peaks", "edges", "conditions"],
    "13C": ["mol", "peaks", "edges", "conditions"],
}


def _condition_slice(fields: Optional[Dict[str, Any]], nucleus: str) -> Dict[str, Any]:
    if not fields:
        return {}
    out = {
        "solvent": fields.get("solvent_raw"),
        "temperature_k": fields.get("temperature_raw"),
        "temperature_unit": fields.get("temperature_unit"),
        "reference_standard": fields.get("reference_standard"),
    }
    if nucleus == "1H":
        out["spectrometer_mhz"] = fields.get("spectrometer_raw")
        out["frequency_unit"] = fields.get("frequency_unit")
        out["reference_offset_ppm"] = fields.get("reference_offset_ppm")
    else:
        out["reference_offset_ppm"] = fields.get("reference_offset_ppm")
    return out


def direct_fingerprints(sources: List[Dict[str, Any]]) -> Dict[str, List[str]]:
    """Fingerprint of exactly the facts each nucleus-derived layer uses."""
    by_role: Dict[str, Dict[str, Any]] = {}
    for source in sources:
        old = by_role.get(source["role"])
        if old is None or source["version"] > old["version"]:
            by_role[source["role"]] = source
    result: Dict[str, List[str]] = {}
    for nucleus, roles in ROLE_DIRECT.items():
        fps = []
        for role in roles:
            source = by_role.get(role)
            if source is None:
                fps.append("missing:%s" % role)
                continue
            parsed = source["parsed"]
            payload = {
                "source_id": source["source_id"],
                "version": source["version"],
                "kind": source["kind"],
            }
            if role == "conditions":
                payload["slice"] = _condition_slice(
                    parsed.get("fields"), nucleus
                )
            else:
                payload["content_sha256"] = source["content_sha256"]
            fps.append("%s:%s" % (role, fingerprint(payload)))
        result[nucleus] = fps
    return result


def overlap_groups(
    peaks: List[Dict[str, Any]], tolerance_ppm: float
) -> Tuple[Dict[str, str], List[List[str]]]:
    """Cluster unresolved peaks by chemical-shift proximity *and* tags.

    Peaks sharing an explicit ``ovl:...`` tag always group together;
    additional proximity groups are found by single-linkage clustering.
    Returns (peak_id -> group label, groups).
    """
    parent: Dict[str, str] = {p["peak_id"]: p["peak_id"] for p in peaks}

    def find(x: str) -> str:
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a: str, b: str) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[max(ra, rb)] = min(ra, rb)

    tag_map: Dict[str, set] = defaultdict(set)
    by_nuc: Dict[str, List[Dict[str, Any]]] = defaultdict(list)
    for peak in peaks:
        by_nuc[peak["nucleus"]].append(peak)
        for tag in peak.get("tags", []):
            if tag.startswith("ovl:"):
                tag_map[tag].add(peak["peak_id"])
    for members in tag_map.values():
        members = list(members)
        for other in members[1:]:
            union(members[0], other)
    for nuc_peaks in by_nuc.values():
        ordered = sorted(nuc_peaks, key=lambda p: (p["shift_ppm"], p["peak_id"]))
        for i, a_peak in enumerate(ordered):
            for b_peak in ordered[i + 1 :]:
                if b_peak["shift_ppm"] - a_peak["shift_ppm"] > tolerance_ppm:
                    break
                union(a_peak["peak_id"], b_peak["peak_id"])
    sets: Dict[str, set] = defaultdict(set)
    for peak in peaks:
        sets[find(peak["peak_id"])].add(peak["peak_id"])
    clusters = [sorted(c) for c in sets.values() if len(c) > 1]
    clusters.sort(key=lambda c: c[0])
    mapping: Dict[str, str] = {}
    real_groups: List[List[str]] = []
    index = 0
    for cluster in clusters:
        if len(cluster) == 1:
            continue
        index += 1
        label = "ovlg%d" % index
        real_groups.append(cluster)
        for peak_id in cluster:
            mapping[peak_id] = label
    return mapping, real_groups


def build_site_index(
    sites: List[Dict[str, Any]],
    groups: List[Dict[str, Any]],
    group_members: Dict[str, List[str]],
) -> Dict[str, Dict[str, Any]]:
    index = {site["site_id"] if "site_id" in site else site["local_id"]: site
             for site in sites}
    return index


def expected_edge_lookup(
    expected_edges: List[Dict[str, Any]],
    sites_by_local: Dict[str, Dict[str, Any]],
) -> Dict[Tuple[str, str, str], Dict[str, Any]]:
    lookup = {}
    for edge in expected_edges:
        lookup[(edge["site_a"], edge["site_b"], edge["edge_kind"])] = edge
        lookup[(edge["site_b"], edge["site_a"], edge["edge_kind"])] = edge
    return lookup


def edge_support(
    target_sites: List[str],
    peak_id: Optional[str],
    edges: List[Dict[str, Any]],
    sites_by_local: Dict[str, Dict[str, Any]],
    expected: Dict[Tuple[str, str, str], Dict[str, Any]],
    target_nucleus: str,
) -> Tuple[float, List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Return (support_ratio in [0,1], supporting_evidence, missing_edges).

    Ratio = reported-and-matched expected edges / expected edges incident
    to any of the target's member sites.  Expected edges with no report are
    returned as gaps but never reject the candidate.
    """
    if peak_id is None:
        return 0.0, [], []
    incident = [
        key
        for key in expected
        if key[0] in target_sites and key[1] not in target_sites
    ]
    # de-duplicate symmetric
    uniq: Dict[Tuple[str, str, str], Dict[str, Any]] = {}
    for key in incident:
        a, b, kind = key
        canon = (min(a, b), max(a, b), kind)
        uniq[canon] = expected[key]
    reported: Dict[str, set] = defaultdict(set)
    for edge in edges:
        if edge.get("status") != "resolved":
            continue
        endpoints = (edge.get("from_peak_id"), edge.get("to_peak_id"))
        for end in endpoints:
            if end == peak_id:
                other = endpoints[1] if endpoints[0] == peak_id else endpoints[0]
                reported[edge["edge_kind"]].add(other)
    evidence: List[Dict[str, Any]] = []
    missing: List[Dict[str, Any]] = []
    matched = 0
    # map member sites -> bound sites of the partner via skeleton adjacency
    # Expected edges are stated between site local ids; we only know peak
    # membership for the *target*. We therefore treat an expected edge as
    # supported when ANY reported edge of that kind touches the peak.
    reported_kind_touch = {
        kind: bool(peaks) for kind, peaks in reported.items()
    }
    for (a, b, kind), edge in sorted(uniq.items()):
        if reported_kind_touch.get(kind):
            matched += 1
            evidence.append(
                {
                    "code": "reported_%s" % kind,
                    "edge_kind": kind,
                    "expected_edge_id": edge["expected_id"]
                    if "expected_id" in edge
                    else edge.get("local_id"),
                    "detail": "reported %s edge touches candidate peak" % kind,
                }
            )
        else:
            missing.append(
                {
                    "code": "missing_expected_%s" % kind,
                    "edge_kind": kind,
                    "between": [a, b],
                    "detail": "expected %s edge %s-%s has no reported match"
                    % (kind, a, b),
                }
            )
    total = len(uniq)
    ratio = (matched / total) if total else 0.0
    return ratio, evidence, missing

"""Server-side SVG rendering: molecular skeleton and peak/edge network.

Element ids are stable (site id / peak id / edge id) so the review page can
cross-highlight peaks <-> edges <-> skeleton <-> candidate rows.
"""
from __future__ import annotations

from typing import Dict, Iterable, List, Optional, Tuple
from xml.sax.saxutils import escape

from .models import Candidate, Edge, Molecule, Peak, Site

_NUC_COLOR = {"1H": "#1f6feb", "13C": "#1a7f37"}
_STATUS_COLOR = {"legal": "#1a7f37", "tied": "#9a6700", "rejected": "#cf222e",
                 "unknown": "#6e7781"}
_EDGE_COLOR = {"COSY": "#8250df", "HSQC": "#0969da", "HMBC": "#bf3989"}


def _esc(value: str) -> str:
    return escape(str(value))


def skeleton_svg(mol: Molecule,
                 verdicts: Optional[Dict[str, dict]] = None,
                 peak_for_site: Optional[Dict[str, str]] = None,
                 width: int = 720, height: int = 360) -> str:
    verdicts = verdicts or {}
    peak_for_site = peak_for_site or {}
    sites_by_id = {s.id: s for s in mol.sites}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" '
             f'viewBox="0 0 {width} {height}" class="loom-svg" id="skeleton-svg">',
             '<rect width="100%" height="100%" fill="#fbfbfc"/>']
    # Bonds: only draw skeleton connectivity between heavy atoms / attached H.
    for bond in mol.bonds:
        a, b = sites_by_id.get(bond.site_a), sites_by_id.get(bond.site_b)
        if not a or not b:
            continue
        dash = "" if bond.site_a[0] == bond.site_b[0] else ""
        parts.append(
            f'<line x1="{a.x}" y1="{a.y}" x2="{b.x}" y2="{b.y}" '
            f'stroke="#57606a" stroke-width="2" {dash}/>')
    for site in mol.sites:
        color = _NUC_COLOR[site.nucleus]
        v = verdicts.get(site.id)
        ring = None
        if v:
            ring = "#1a7f37" if v["decision"] == "accepted" else "#cf222e"
        x, y = site.x, site.y
        r = 15
        if ring:
            parts.append(f'<circle cx="{x}" cy="{y}" r="{r + 5}" fill="none" '
                         f'stroke="{ring}" stroke-width="3" class="site-ring"/>')
        parts.append(
            f'<g class="site-node" data-site="{_esc(site.id)}" data-nucleus="{site.nucleus}" '
            f'data-peak="{_esc(peak_for_site.get(site.id, ""))}">'
            f'<circle cx="{x}" cy="{y}" r="{r}" fill="white" stroke="{color}" stroke-width="2.5"/>'
            f'<text x="{x}" y="{y + 4}" text-anchor="middle" font-size="11" '
            f'font-family="ui-monospace,monospace" fill="{color}">{_esc(site.id)}</text>'
            f'<text x="{x}" y="{y - r - 6}" text-anchor="middle" font-size="10" '
            f'fill="#24292f">{_esc(site.label)}</text>'
            f'<text x="{x}" y="{y + r + 14}" text-anchor="middle" font-size="9.5" '
            f'fill="#57606a">{site.expected_shift:g} ppm</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def network_svg(peaks: List[Peak], edges: List[Edge], mol: Molecule,
                highlight_candidates: Optional[Dict[str, List[Candidate]]] = None,
                width: int = 720, height: int = 420) -> str:
    """Two columns (1H left, 13C right); correlation edges as curved links."""
    h_peaks = sorted([p for p in peaks if p.nucleus == "1H"], key=lambda p: -(p.shift.value or 0))
    c_peaks = sorted([p for p in peaks if p.nucleus == "13C"], key=lambda p: -(p.shift.value or 0))

    def coords(lst: List[Peak], x0: int, top: int = 46, gap: int = 48) -> Dict[str, Tuple[float, float]]:
        return {p.id: (x0, top + i * gap) for i, p in enumerate(lst)}

    hpos, cpos = coords(h_peaks, 120), coords(c_peaks, 600)
    pos = {**hpos, **cpos}
    parts = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" '
             f'height="{max(height, 46 + max(len(h_peaks), len(c_peaks)) * 48 + 20)}" '
             f'viewBox="0 0 {width} {max(height, 46 + max(len(h_peaks), len(c_peaks)) * 48 + 20)}" '
             f'class="loom-svg" id="network-svg">',
             '<rect width="100%" height="100%" fill="#fbfbfc"/>',
             '<text x="120" y="24" text-anchor="middle" font-size="13" fill="#1f6feb" font-weight="600">1H peaks</text>',
             '<text x="600" y="24" text-anchor="middle" font-size="13" fill="#1a7f37" font-weight="600">13C peaks</text>',
             '<defs>']
    for kind in _EDGE_COLOR:
        parts.append(f'<marker id="arrow-{kind}" markerWidth="7" markerHeight="7" '
                     f'refX="6" refY="3" orient="auto"><path d="M0,0 L7,3 L0,6 Z" '
                     f'fill="{_EDGE_COLOR[kind]}"/></marker>')
    parts.append("</defs>")
    for edge in edges:
        if edge.from_peak not in pos or edge.to_peak not in pos:
            continue
        (x1, y1), (x2, y2) = pos[edge.from_peak], pos[edge.to_peak]
        mx = (x1 + x2) / 2
        color = _EDGE_COLOR.get(edge.kind, "#57606a")
        dash = 'stroke-dasharray="5 4"' if edge.kind == "HMBC" else ""
        parts.append(
            f'<path class="edge-link" data-edge="{_esc(edge.id)}" '
            f'data-kind="{edge.kind}" data-from="{_esc(edge.from_peak)}" '
            f'data-to="{_esc(edge.to_peak)}" '
            f'd="M {x1},{y1} C {mx},{y1} {mx},{y2} {x2},{y2}" '
            f'fill="none" stroke="{color}" stroke-width="1.8" opacity="0.75" {dash}/>')
    for p, (x, y) in pos.items():
        peak = next(q for q in peaks if q.id == p)
        diag = peak.shift.value is None
        ov = f' data-overlap="{_esc(peak.overlap_group)}"' if peak.overlap_group else ""
        parts.append(
            f'<g class="peak-node" data-peak="{_esc(peak.id)}" '
            f'data-nucleus="{peak.nucleus}"{ov}>'
            f'<circle cx="{x}" cy="{y}" r="9" fill="{"#cf222e" if diag else "white"}" '
            f'stroke="{_NUC_COLOR[peak.nucleus]}" stroke-width="2.2"/>'
            f'<text x="{x + (-14 if x < 360 else 14)}" y="{y + 4}" '
            f'text-anchor="{"end" if x < 360 else "start"}" font-size="10.5" '
            f'font-family="ui-monospace,monospace" fill="#24292f">'
            f'{_esc(peak.shift.raw)} ppm · {_esc(peak.id)}</text></g>')
    parts.append("</svg>")
    return "".join(parts)

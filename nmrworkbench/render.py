"""Server-side SVG renderers.

Every rendered mark carries the same stable identity shown elsewhere in the
UI (``data-nid`` / ``data-pid`` / ``data-eid``) so clicking a peak, an edge,
an atom or a candidate row highlights the same objects across views.
"""
from __future__ import annotations

import html
from typing import Dict, Any, List, Tuple

_PALETTE = {
    "C": "#2b2b3a", "H": "#336699", "O": "#aa3333", "N": "#336633"}


def esc(v):
    return html.escape("" if v is None else str(v), quote=True)


def _status_of(view, nid):
    d = view.get("decisions", {}).get(nid)
    return d["verdict"] if d and d.get("currently_valid") else \
        ("expired" if d else None)


def molecule_svg(view, width=430, height=360):
    a = view["analysis"]
    atoms = {x["id"]: x for x in a["skeleton"]["atoms"]}
    bonds = a["skeleton"]["bonds"]
    xs = [x["x"] for x in atoms.values() if x["x"] is not None]
    ys = [u["y"] for u in atoms.values() if u["y"] is not None]
    minx, maxx = min(xs), max(xs); miny, maxy = min(ys), max(ys)
    pad = 55

    def P(x, y):
        sx = pad + (x - minx) / max(1e-9, maxx - minx) * (width - 2 * pad)
        sy = pad + (y - miny) / max(1e-9, maxy - miny) * (height - 2 * pad)
        return sx, sy

    parts = [f'<svg data-view="molecule" viewBox="0 0 {width} {height}" '
             f'xmlns="http://www.w3.org/2000/svg" role="img">']
    for b in bonds:
        u, v = atoms[b["a"]], atoms[b["b"]]
        if u["x"] is None or v["x"] is None:
            continue
        x1, y1 = P(u["x"], u["y"]); x2, y2 = P(v["x"], v["y"])
        dash = ' stroke-dasharray="4 3"' if b.get("order", 1) == 2 else ""
        parts.append(f'<line data-bond="{esc(b["id"])}" x1="{x1:.1f}" y1="{y1:.1f}"'
                     f' x2="{x2:.1f}" y2="{y2:.1f}" stroke="#888" stroke-width="2"{dash}/>')
    for nid, u in atoms.items():
        if u["x"] is None:
            continue
        x, y = P(u["x"], u["y"])
        status = _status_of(view, nid)
        fill = {"accepted": "#cfe9cf", "rejected": "#f3cfcf",
                "expired": "#f0e6c8"}.get(status, "#ffffff")
        el = _PALETTE.get(u["element"], "#444")
        parts.append(
            f'<g data-nid="{esc(nid)}" class="atom status-{esc(status or "open")}">'
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="17" fill="{fill}" '
            f'stroke="{el}" stroke-width="2"/>'
            f'<text x="{x:.1f}" y="{y - 2:.1f}" text-anchor="middle" '
            f'font-size="10" fill="{el}">{esc(u["element"])}</text>'
            f'<text x="{x:.1f}" y="{y + 10:.1f}" text-anchor="middle" '
            f'font-size="9" fill="#222">{esc(nid)}</text></g>')
    parts.append("</svg>")
    return "".join(parts)


def peak_table_svg(view):
    a = view["analysis"]
    rows = ['<table class="grid peaks" data-view="peaks"><thead><tr>'
            "<th>stable id</th><th>nucleus</th><th>shift (raw)</th>"
            "<th>intensity</th><th>integral</th><th>mult.</th><th>overlap</th>"
            "</tr></thead><tbody>"]
    overlap = {pid: g for g in a["overlap_groups"] for pid in g}
    for nuc, peaks in a["peaks"].items():
        for p in peaks:
            s = p["shift"]
            ov = "/".join(overlap.get(p["id"], [])) or "—"
            rows.append(
                f'<tr data-pid="{esc(p["id"])}" data-nuc="{esc(nuc)}">'
                f'<td>{esc(p["id"])}</td><td>{esc(nuc)}</td>'
                f'<td title="basis {esc(s["basis"])}">{esc(s["raw"])}'
                f'<span class="dim"> = {s["canonical_value"]} {esc(s["canonical_unit"])}</span></td>'
                f'<td>{esc(p["intensity"]["raw"] if p["intensity"] else "—")}</td>'
                f'<td>{esc(p["integral"]["raw"] if p["integral"] else "—")}</td>'
                f'<td>{esc(p["multiplicity"] or "—")}</td>'
                f'<td class="warn">{esc(ov)}</td></tr>')
    rows.append("</tbody></table>")
    return "".join(rows)


def edge_table_svg(view):
    a = view["analysis"]
    rows = ['<table class="grid edges" data-view="edges"><thead><tr>'
            "<th>edge id</th><th>experiment</th><th>F1 ppm</th><th>F2 ppm</th>"
            "<th>matched F1 peaks</th><th>matched F2 peaks</th><th>source@ver</th>"
            "</tr></thead><tbody>"]
    missing_keys = {(m["experiment"], "/".join(m["between"]))
                    for m in a["missing_edges"]}
    for exp, items in a["observed_edges"].items():
        for e in items:
            rows.append(
                f'<tr data-eid="{esc(e["edge_id"])}" data-exp="{esc(exp)}">'
                f'<td>{esc(e["edge_id"])}</td><td>{esc(exp)}</td>'
                f'<td>{e["f1"]:.3f}</td><td>{e["f2"]:.3f}</td>'
                f'<td>{esc(",".join(e["f1_peaks"]) or "—")}</td>'
                f'<td>{esc(",".join(e["f2_peaks"]) or "—")}</td>'
                f'<td class="dim">{esc(e["source_doc"])}@{esc(e["version"])}</td></tr>')
    for m in a["missing_edges"]:
        rows.append(
            f'<tr class="missing" data-missing="{esc(m["experiment"])}:'
            f'{esc("/".join(m["between"]))}">'
            f'<td>missing</td><td class="warn">{esc(m["experiment"])}</td>'
            f'<td colspan="3" class="warn">expected between '
            f'{esc(" &amp; ".join(m["between"]))} (coupling evidence only)</td>'
            f'<td colspan="2" class="dim">not in any submitted peak list</td></tr>')
    rows.append("</tbody></table>")
    return "".join(rows)


def candidate_table(view):
    a = view["analysis"]
    rows = ['<table class="grid candidates" data-view="candidates"><thead><tr>'
            "<th>nucleus</th><th>candidate peak</th><th>score</th>"
            "<th>residual ppm</th><th>edge evidence</th><th>status</th>"
            "<th>rank</th><th>human decision</th><th>actions</th>"
            "</tr></thead><tbody>"]
    dec = view.get("decisions", {})
    for nid, cands in a["candidates"].items():
        d = dec.get(nid)
        for c in sorted(cands, key=lambda x: (x["peak_id"] or "~", -x["rank"])):
            ev = ", ".join(x["edge_id"] for x in c["edge_evidence"]) or "—"
            badge = []
            if c["tied_best"] and c["status"] == "candidate" and c["rank"] == 1:
                badge.append('<span class="tie">joint-best</span>')
            if c.get("ambiguity_group"):
                badge.append('<span class="amb">overlap</span>')
            if c.get("symmetry_group"):
                badge.append('<span class="sym">symmetric</span>')
            decision = "—"
            if d and d.get("peak_id") == c["peak_id"]:
                cls = "expired" if not d.get("currently_valid") else d["verdict"]
                decision = f'<span class="{cls}">{esc(d["verdict"])}'
                if not d.get("currently_valid"):
                    decision += " (expired)"
                decision += "</span>"
            elif c["status"] == "rejected":
                decision = '<span class="dim">auto-reject</span>'
            actions = ""
            if c["peak_id"]:
                actions = (f'<button data-act="accept" data-nid="{esc(nid)}" '
                           f'data-pid="{esc(c["peak_id"])}">confirm</button>'
                           f'<button data-act="reject" data-nid="{esc(nid)}" '
                           f'data-pid="{esc(c["peak_id"])}">reject</button>')
            rows.append(
                f'<tr data-nid="{esc(nid)}" data-pid="{esc(c["peak_id"] or "")}" '
                f'class="candrow {esc(c["status"])}">'
                f'<td>{esc(nid)}</td><td>{esc(c["peak_id"] or "unassigned")}</td>'
                f'<td>{c["score"]:.3f}</td>'
                f'<td>{("" if c["shift_residual"] is None else round(c["shift_residual"],4))}</td>'
                f'<td class="dim">{esc(ev)}</td>'
                f'<td>{esc(c["status"])} {" ".join(badge)}</td>'
                f'<td>{c["rank"] or "—"}</td><td>{decision}</td>'
                f'<td>{actions}</td></tr>')
        # explicit unknown / missing peak row reasons
        unk = next((c for c in cands if c["peak_id"] is None), None)
        if unk and unk["rejected_reasons"]:
            rows.append(
                f'<tr class="unknown" data-nid="{esc(nid)}">'
                f'<td>{esc(nid)}</td><td>no unique peak</td><td>—</td><td>—</td>'
                f'<td colspan="5" class="warn">{esc("; ".join(unk["rejected_reasons"]))}</td></tr>')
    rows.append("</tbody></table>")
    return "".join(rows)

"""Bundled offline domain case pack: "NMR 共振经纬"."""
from __future__ import annotations

import json
import os
from typing import Dict, List, Tuple

from .ingest import parse_document
from .models import Parameters
from .store import Store

CASE_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                        "casepack")


def scenario() -> dict:
    with open(os.path.join(CASE_DIR, "scenario.json"), encoding="utf-8") as fh:
        return json.load(fh)


def read_case_file(rel: str) -> str:
    with open(os.path.join(CASE_DIR, rel), encoding="utf-8") as fh:
        return fh.read()


def list_files() -> List[str]:
    out: List[str] = []
    for root, _, files in os.walk(CASE_DIR):
        for name in sorted(files):
            rel = os.path.relpath(os.path.join(root, name), CASE_DIR)
            out.append(rel)
    return sorted(out)


def load_initial(store: Store, project_id: str, actor: str = "importer") -> List[dict]:
    """Commit the case pack's initial revision set onto a fresh project."""
    spec = scenario()
    records = []
    for item in spec["initial"]:
        text = read_case_file(item["path"])
        rec, _ = store.commit_source(project_id, "main", item["path"], text,
                                    item["kind"], actor)
        records.append(rec)
    return records


def default_params_v2() -> Parameters:
    data = json.loads(read_case_file("params-v2.json"))
    return Parameters.from_dict(data)

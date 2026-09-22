"""持久化：导出 -> 删除数据库 -> 再导入，身份、顺序、来源关系保持。"""
import os

from nmrwb import services
from nmrwb.core import Store
from tests.test_statemachine import C13, H1_V1, MOL


def _build(store):
    services.submit_input(store, "mol", "nmrwb-mol", MOL)
    services.submit_input(store, "h1", "nmrwb-1d", H1_V1)
    services.submit_input(store, "c13", "nmrwb-1d", C13)
    services.compute(store)
    st = services.current_state(store)
    aset = list(st["candidate_sets"].values())[-1]
    cand = next(c for c in aset["candidates"] if c["atom_id"] == "H1")
    services.decide(store, "H1", cand["candidate_id"], "confirm", "alice")


def test_export_delete_import_preserves_identity_order_provenance(
        tmp_path, vclock):
    db1 = str(tmp_path / "a.db")
    s1 = Store(db1, clock=vclock)
    _build(s1)
    archive = s1.export_jsonl()

    os.remove(db1)  # 删除本地数据库
    assert not os.path.exists(db1)

    s2 = Store(str(tmp_path / "b.db"), clock=vclock)
    n = s2.import_jsonl(archive)
    assert n == len(s1.events())

    e1, e2 = s1.events(), s2.events()
    assert [e["seq"] for e in e2] == [e["seq"] for e in e1]
    assert [e["event_id"] for e in e2] == [e["event_id"] for e in e1]

    st1, st2 = services.current_state(s1), services.current_state(s2)
    assert list(st1["candidate_sets"]) == list(st2["candidate_sets"])
    for sid in st1["candidate_sets"]:
        a, b = st1["candidate_sets"][sid], st2["candidate_sets"][sid]
        assert a["depends_on"] == b["depends_on"]
        assert [c["candidate_id"] for c in a["candidates"]] == \
               [c["candidate_id"] for c in b["candidates"]]
    assert list(st1["decisions"]) == list(st2["decisions"])


def test_replay_identical_after_reimport(tmp_path, vclock):
    s1 = Store(str(tmp_path / "a.db"), clock=vclock)
    _build(s1)
    text = s1.export_jsonl()
    s1.conn.close()
    s2 = Store(str(tmp_path / "b.db"), clock=vclock)
    s2.import_jsonl(text)
    for seq in range(1, len(s1.events()) + 1):
        t1 = services.current_state(s1, upto=seq)["timeline"]
        t2 = services.current_state(s2, upto=seq)["timeline"]
        assert t1 == t2

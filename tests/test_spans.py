from cutdown import spans
import pytest

T = {"A": {"cut_in": 0., "cut_out": 10., "dur_cut": 10.}}
W = [[1., 1.2, "I"], [1.5, 1.8, "um"], [2.1, 2.3, "love"], [2.4, 2.9, "music"]]

def test_internal():
    assert spans.unit_spans("A", T, {"A": W}) == [[0., 1.35], [1.95, 10.]]

def test_pause_cap():
    w = [[1, 1.2, "I"], [2.2, 2.4, "um"], [3.4, 4, "love"]]
    assert spans.unit_spans("A", T, {"A": w}) == [[0., 1.425], [3.175, 10.]]

def test_glued():
    w = [x[:] for x in W]; w[2][0] = 1.85
    assert spans.unit_spans("A", T, {"A": w}) == [[0., 10.]]

@pytest.mark.parametrize("token", ["mm", "hmm", "yeah", "like"])
def test_meaningful(token):
    w = [x[:] for x in W]; w[1][2] = token
    assert spans.unit_spans("A", T, {"A": w}) == [[0., 10.]]

def test_soft_internal():
    w = [x[:] for x in W]; w[1][2] = "ah,"
    assert len(spans.unit_spans("A", T, {"A": w})) == 2

def test_soft_after_stop():
    w = [x[:] for x in W]; w[0][2] = "it."; w[1][2] = "Ah,"
    assert spans.unit_spans("A", T, {"A": w}) == [[0., 10.]]

def test_soft_first():
    assert spans.unit_spans("A", T, {"A": [[0, .2, "Ah,"], [.6, 1, "yes"]]}) == [[0., 10.]]

def test_run():
    w = W[:2] + [[1.85, 1.9, "uh"]] + W[2:]
    assert len(spans.unit_spans("A", T, {"A": w})) == 2

def test_only_fillers():
    assert spans.unit_spans("A", T, {"A": [[0,.2,"um"],[.6,1,"uh"]]}) == [[0.,10.]]

def test_disabled():
    assert spans.unit_spans("A", T, {"A": W}, cfg={"remove": False}) == [[0.,10.]]

def test_explicit_head_when_disabled():
    assert spans.unit_spans("A", T, {"A": [[0,.2,"Um,"],[.6,1,"yes"]]}, cfg={"remove": False}, flags={"head": True}) == [[.4,10.]]

def test_edge_holes():
    assert spans.unit_spans("A", T, {}, holes=[[0,2],[8,10]]) == [[2,8]]

def test_text():
    sp = spans.unit_spans("A", T, {"A": W})
    assert spans.kept_text("A", {}, {"A": W}, sp) == "I love music"
    assert spans.marked_text("A", {}, {"A": W}, sp) == "I ‖ love music"

@pytest.mark.parametrize("order,sp,joins", [
    (["A","B"], {"A":[[0,3]], "B":[[4,8]]}, ["start","tight"]),
    (["A","C"], {"A":[[0,4]], "C":[[8,12]]}, ["start","edit"]),
    (["A"], {"A":[[0,1],[2,4]]}, ["start","tight"]),
    (["A","B"], {"A":[[0,4]], "B":[[4,8]]}, ["start"]),
])
def test_clips(order, sp, joins):
    t = {u:{"cut_in":i*4,"cut_out":(i+1)*4} for i,u in enumerate("ABC")}
    plan = spans.clips(order, sp, t)
    assert [c["join"] for c in plan] == joins
    if len(plan) == 1:
        assert plan[0] == {"in":0,"out":8,"units":["A","B"],"join":"start"}


CW = [[0,.2,"And"],[.3,.5,"yeah,"],[1,1.2,"I"],[1.3,1.5,"love"],[2,2.2,"all"],[2.3,2.6,"music."]]

def test_locate_leadin():
    assert spans.locate_cut(CW,"And yeah,",T["A"]) == (0.,.775)

def test_locate_middle():
    assert spans.locate_cut(CW,"I love",T["A"]) == (.725,1.775)

def test_locate_no_pause():
    assert spans.locate_cut(CW,"And",T["A"]) is None

def test_locate_ambiguous():
    assert spans.locate_cut(CW+CW,"And yeah",T["A"]) is None

def test_locate_too_little():
    assert spans.locate_cut(CW,"And yeah I love",T["A"]) is None

def test_locate_normalised():
    assert spans.locate_cut(CW,"AND ‖ yeah!",T["A"]) == (0.,.775)

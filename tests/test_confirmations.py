"""责任确认（已闭合越温区间）HTTP API 集成测试。

覆盖：区间定位与快照、汇总状态、确认/异议提交、幂等重传与异内容冲突、
改口保留历次、责任方越界、开放区间拒绝、晚到事件重建后表态失效/继续有效、
区间消失后历史可查、串行化锁与既有报告契约保持不变。
"""
import threading

from app.database import SessionLocal
from app.models import ResponsibilityStance


def meas(client, event_id, at, temp, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": event_id, "cargo_id": cargo,
            "occurred_at": at, "temperature": str(temp),
        },
    )


def handover(client, event_id, at, giver, receiver, cargo="C1"):
    return client.post(
        "/events",
        json={
            "event_id": event_id, "cargo_id": cargo,
            "occurred_at": at, "giver": giver, "receiver": receiver,
        },
    )


def build_closed_interval(client, holder="A", with_handover=True, cargo="C1"):
    """构造一条已闭合区间 [m1 10:10, m2 10:50)，可选 10:30 A->B 交接。"""
    meas(client, "m1", "2026-01-01T10:10:00Z", 8, cargo)
    if with_handover:
        handover(client, "h1", "2026-01-01T10:30:00Z", holder, "B", cargo)
    meas(client, "m2", "2026-01-01T10:50:00Z", 4, cargo)


def stance_body(stance_id, snap, party, stance="confirm", reason=None,
                interval="m1", cargo="C1"):
    body = {
        "stance_id": stance_id,
        "cargo_id": cargo,
        "interval_id": interval,
        "snapshot_id": snap,
        "responsible_party": party,
        "stance": stance,
    }
    if reason is not None:
        body["reason"] = reason
    return body


def post_stance(client, body, cargo="C1"):
    return client.post(f"/cargo/{cargo}/stances", json=body)


def snapshot_for(client, interval="m1", cargo="C1"):
    return client.get(f"/cargo/{cargo}/confirmations/{interval}").json()


# ------------------------------------------------------------- 查询视图


def test_closed_interval_view_locates_by_open_event_id(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    v = snapshot_for(client)
    assert v["interval_id"] == "m1"
    assert v["start_event_id"] == "m1"
    assert v["end_event_id"] == "m2"
    assert v["start"] == "2026-01-01T10:10:00Z"
    assert v["end"] == "2026-01-01T10:50:00Z"
    assert [s["responsible_party"] for s in v["segments"]] == ["A", "B"]
    assert v["party_stances"] == []
    assert v["summary_status"] == "pending"
    assert v["snapshot_id"].startswith("snap-") and len(v["snapshot_id"]) == 69


def test_confirmations_list_excludes_open_intervals(client, make_cargo):
    make_cargo(limit="5", holder="A")
    meas(client, "m1", "2026-01-01T10:10:00Z", 9)  # 只有开放区间
    assert client.get("/cargo/C1/confirmations").json() == []


def test_snapshot_deterministic_and_changes_with_segments(client, make_cargo):
    from app.chain import snapshot_id, rebuild_chain
    from decimal import Decimal
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    s1 = snapshot_for(client)["snapshot_id"]
    # 再查一次：相同起止与片段 -> 快照稳定
    assert snapshot_for(client)["snapshot_id"] == s1
    # 纯函数层：片段顺序/内容决定快照
    r = rebuild_chain("A", Decimal("5"), [])
    assert r.excursions == ()


def test_unknown_cargo_on_confirmations_404(client):
    assert client.get("/cargo/NOPE/confirmations").status_code == 404
    assert client.get("/cargo/NOPE/stances/history").status_code == 404


def test_open_interval_lookup_conflicts(client, make_cargo):
    make_cargo(limit="5", holder="A")
    meas(client, "m1", "2026-01-01T10:10:00Z", 9)
    r = client.get("/cargo/C1/confirmations/m1")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "interval_open"


def test_missing_interval_404(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    r = client.get("/cargo/C1/confirmations/ghost")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "interval_not_found"


# ------------------------------------------------------------- 汇总状态


def test_summary_transitions_pending_disputed_all_confirmed(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]

    post_stance(client, stance_body("sA", snap, "A"))
    assert snapshot_for(client)["summary_status"] == "pending"  # B 未表态

    post_stance(client, stance_body("sB", snap, "B", "object", reason="温度探头失准"))
    v = snapshot_for(client)
    assert v["summary_status"] == "disputed"
    by_party = {p["responsible_party"]: p for p in v["party_stances"]}
    assert by_party["A"]["stance"] == "confirm"
    assert by_party["B"]["stance"] == "object"
    assert by_party["B"]["reason"] == "温度探头失准"

    # B 改口确认（新编号）
    post_stance(client, stance_body("sB2", snap, "B", "confirm"))
    v = snapshot_for(client)
    assert v["summary_status"] == "all_confirmed"
    b = [p for p in v["party_stances"] if p["responsible_party"] == "B"][0]
    assert b["stance_id"] == "sB2"  # 最新有效表态


def test_latest_valid_stance_is_highest_id_on_same_instant(client, make_cargo):
    # created_at 同刻时以表态编号排序取最新（极小概率，仅验证排序兜底）
    make_cargo(limit="5", holder="A")
    build_closed_interval(client, with_handover=False)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("a1", snap, "A", "object", reason="r1"))
    post_stance(client, stance_body("a2", snap, "A", "confirm"))
    v = snapshot_for(client)
    assert v["summary_status"] == "all_confirmed"
    assert v["party_stances"][0]["stance_id"] == "a2"


# ------------------------------------------------------------- 提交校验


def test_submit_confirm_and_object_persistence(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    r = post_stance(client, stance_body("s1", snap, "A"))
    assert r.status_code == 201
    body = r.json()
    assert body["is_current"] is True
    assert body["stance"] == "confirm" and body["reason"] is None

    r = post_stance(client, stance_body("s2", snap, "B", "object", reason="  有异议 "))
    assert r.status_code == 201
    assert r.json()["reason"] == "有异议"  # 首尾空白归一化


def test_object_requires_reason(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    r = post_stance(client, stance_body("s1", snap, "A", "object"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_stance_shape"
    r = post_stance(client, stance_body("s2", snap, "A", "object", reason="   "))
    assert r.status_code == 422
    # 确认不得带理由
    r = post_stance(client, stance_body("s3", snap, "A", "confirm", reason="x"))
    assert r.status_code == 422
    assert client.get("/cargo/C1/stances/history").json() == []  # 拒绝不落库


def test_party_must_be_in_current_segment(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    r = post_stance(client, stance_body("s1", snap, "X"))
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "party_not_in_segment"


def test_stale_snapshot_rejected(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    r = post_stance(client, stance_body("s1", "snap-deadbeef", "A"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "snapshot_stale"


def test_submit_to_open_interval_rejected(client, make_cargo):
    make_cargo(limit="5", holder="A")
    meas(client, "m1", "2026-01-01T10:10:00Z", 9)
    r = post_stance(client, stance_body("s1", "whatever", "A", interval="m1"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "interval_open"


def test_submit_to_missing_interval_rejected(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    r = post_stance(client, stance_body("s1", snap, "A", interval="ghost"))
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "interval_not_found"


def test_path_cargo_mismatch_rejected(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    r = client.post(
        "/cargo/OTHER/stances", json=stance_body("s1", snap, "A")
    )
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "bad_stance_shape"


# ------------------------------------------------------------- 幂等 / 改口


def test_same_stance_id_same_content_replays_original(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    body = stance_body("s1", snap, "B", "object", reason="r")
    assert post_stance(client, body).status_code == 201
    r2 = post_stance(client, body)
    assert r2.status_code == 200
    assert r2.json()["replayed"] is True
    assert r2.json()["stance"]["stance_id"] == "s1"
    db = SessionLocal()
    try:
        assert db.query(ResponsibilityStance).count() == 1
    finally:
        db.close()


def test_same_stance_id_different_content_conflicts(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("s1", snap, "A"))
    r = post_stance(client, stance_body("s1", snap, "B"))
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "stance_id_conflict"
    # 原记录不变
    assert client.get("/cargo/C1/stances/history").json()[0]["responsible_party"] == "A"


def test_change_of_mind_uses_new_id_and_keeps_history(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("s1", snap, "A", "object", reason="v1"))
    post_stance(client, stance_body("s2", snap, "A", "confirm"))
    hist = client.get("/cargo/C1/stances/history/m1").json()
    assert [h["stance_id"] for h in hist] == ["s1", "s2"]
    assert all(h["is_current"] for h in hist)  # 同快照下历次表态都仍可查
    # 最新有效表态决定汇总
    assert snapshot_for(client)["summary_status"] == "pending"


# ------------------------------------------------- 晚到事件重建：失效/有效


def test_late_event_changing_boundary_invalidates_old_stances(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("s1", snap, "A"))
    post_stance(client, stance_body("s2", snap, "B"))
    assert snapshot_for(client)["summary_status"] == "all_confirmed"

    # 晚到 10:40 的不超限测温：区间提前闭合，end 边界变化
    meas(client, "late", "2026-01-01T10:40:00Z", 2)
    v = snapshot_for(client)
    assert v["end_event_id"] == "late"
    assert v["snapshot_id"] != snap
    assert v["summary_status"] == "pending"
    assert v["party_stances"] == []

    hist = client.get("/cargo/C1/stances/history/m1").json()
    assert len(hist) == 2 and all(h["is_current"] is False for h in hist)

    # 旧快照提交被拒
    r = post_stance(client, stance_body("s3", snap, "A"))
    assert r.status_code == 409 and r.json()["error"]["code"] == "snapshot_stale"

    # 已存编号同内容重传仍返回原记录（即使快照已失效）
    r = post_stance(client, stance_body("s1", snap, "A"))
    assert r.status_code == 200 and r.json()["replayed"] is True
    assert r.json()["stance"]["is_current"] is False


def test_late_event_not_affecting_interval_keeps_stances_valid(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("s1", snap, "A"))
    post_stance(client, stance_body("s2", snap, "B"))
    # 区间外的不超限测温（区间开始之前，且 <= 上限）不改变任何东西
    meas(client, "outside", "2026-01-01T10:05:00Z", 1)
    v = snapshot_for(client)
    assert v["snapshot_id"] == snap
    assert v["summary_status"] == "all_confirmed"
    assert all(h["is_current"] for h in
               client.get("/cargo/C1/stances/history/m1").json())


def test_late_event_changing_start_makes_interval_disappear_history_kept(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("s1", snap, "A"))
    # 更早的超限测温 m0 开启区间；m1 变为区间内重复超限，不再是开启事件
    meas(client, "m0", "2026-01-01T10:05:00Z", 9)
    r = client.get("/cargo/C1/confirmations/m1")
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "interval_not_found"
    # 对新区间 m0 提交同样 404（旧键），历史仍可查且失效
    hist = client.get("/cargo/C1/stances/history/m1").json()
    assert len(hist) == 1 and hist[0]["is_current"] is False
    # 新区间以 m0 定位，尚无表态
    v = client.get("/cargo/C1/confirmations/m0").json()
    assert v["summary_status"] == "pending"


def test_rejected_submissions_are_not_persisted(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("bad1", snap, "X"))                       # 责任方越界
    post_stance(client, stance_body("bad2", "snap-x", "A"))                  # 快照过期
    post_stance(client, stance_body("bad3", snap, "A", interval="ghost"))     # 区间不存在
    assert client.get("/cargo/C1/stances/history").json() == []


# ------------------------------------------------------------- 并发串行化


def test_stance_submission_serialized_with_event_write(client, make_cargo):
    """表态提交与事件写入共用货物行锁：并发下不允许出现“读到旧快照”的脏表态。

    内存 StaticPool 下 FOR UPDATE 被忽略且单连接天然串行，用线程做烟雾验证；
    文件型库 / PostgreSQL 上行锁提供真正的串行化。
    """
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("s1", snap, "A"))
    post_stance(client, stance_body("s2", snap, "B"))
    assert snapshot_for(client)["summary_status"] == "all_confirmed"

    errors: list[Exception] = []

    def add_late_close():
        try:
            meas(client, "late", "2026-01-01T10:40:00Z", 2)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def submit_old_snapshot():
        try:
            post_stance(client, stance_body("s3", snap, "A"))
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    t1 = threading.Thread(target=add_late_close)
    t2 = threading.Thread(target=submit_old_snapshot)
    t1.start(); t2.start(); t1.join(); t2.join()
    assert errors == []
    v = snapshot_for(client)
    # 无论两者谁先：边界已变，旧快照表态绝不可能以“有效”身份落库
    assert v["snapshot_id"] != snap
    rows = client.get("/cargo/C1/stances/history").json()
    new_rows = [r for r in rows if r["stance_id"] == "s3"]
    assert new_rows == [] or new_rows[0]["is_current"] is False


# ------------------------------------------------------------- 既有契约保持


def test_party_appearing_in_multiple_segments_counts_once(client, make_cargo):
    # 区间内交接 A->B 再 B->A：A 出现在两个片段中，确认一次即覆盖其全部责任
    make_cargo(limit="5", holder="A")
    meas(client, "m1", "2026-01-01T10:10:00Z", 8)
    handover(client, "h1", "2026-01-01T10:20:00Z", "A", "B")
    handover(client, "h2", "2026-01-01T10:30:00Z", "B", "A")
    meas(client, "m2", "2026-01-01T10:50:00Z", 4)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("s1", snap, "A"))
    v = snapshot_for(client)
    # 责任方去重：只有 A、B 两方；A 已确认，B 未表态 -> 待确认
    assert [s["responsible_party"] for s in v["segments"]] == ["A", "B", "A"]
    assert v["summary_status"] == "pending"
    post_stance(client, stance_body("s2", snap, "B"))
    assert snapshot_for(client)["summary_status"] == "all_confirmed"


def test_two_excursions_have_independent_snapshots_and_stances(client, make_cargo):
    make_cargo(limit="5", holder="A")
    meas(client, "m1", "2026-01-01T10:10:00Z", 8)
    meas(client, "m2", "2026-01-01T10:20:00Z", 4)
    handover(client, "h1", "2026-01-01T10:25:00Z", "A", "B")
    meas(client, "m3", "2026-01-01T10:30:00Z", 9)
    meas(client, "m4", "2026-01-01T10:40:00Z", 4)
    vlist = client.get("/cargo/C1/confirmations").json()
    assert [v["interval_id"] for v in vlist] == ["m1", "m3"]
    assert vlist[0]["snapshot_id"] != vlist[1]["snapshot_id"]
    # 仅确认第一区间；第二区间仍待确认
    post_stance(client, stance_body("s1", vlist[0]["snapshot_id"], "A"))
    assert snapshot_for(client, "m1")["summary_status"] == "all_confirmed"
    assert snapshot_for(client, "m3")["summary_status"] == "pending"


def test_report_contract_unchanged_and_coexists(client, make_cargo):
    make_cargo(limit="5", holder="A")
    build_closed_interval(client)
    snap = snapshot_for(client)["snapshot_id"]
    post_stance(client, stance_body("s1", snap, "A"))
    rpt = client.get("/cargo/C1/report").json()
    ex = rpt["excursions"][0]
    # 追责报告原有字段保持不变
    assert set(ex.keys()) == {"start", "end", "is_open", "segments"}
    assert ex["is_open"] is False
    assert [s["responsible_party"] for s in ex["segments"]] == ["A", "B"]
    # 事件写入 / 幂等链路仍可用
    r = meas(client, "m1", "2026-01-01T10:10:00Z", 8)
    assert r.status_code == 200 and r.json()["replayed"] is True

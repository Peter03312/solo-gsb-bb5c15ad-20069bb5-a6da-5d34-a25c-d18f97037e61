"""已闭合越温区间的责任确认：查询视图、表态提交与历史。

区间标识
--------
区间以“开启测温事件ID”定位（``interval_id = start_event_id``）。

快照标识
--------
``chain.snapshot_id`` 对（区间起止事件 + 责任片段）取 SHA-256：边界或责任片段
变化则快照变化，绑定旧快照的表态随之失效；未变化则快照稳定、表态继续有效。

提交串行化
----------
表态提交与事件写入一样先 ``SELECT ... FOR UPDATE`` 锁定货物行，因此同一货物的
表态与事件写入在同一把锁上串行化，校验所用的快照必然是提交时的最新重建结果。

表态生命周期
------------
* 表态不可修改：同一编号同内容重传返回原记录（200 replayed，即使其快照已失效）；
  同一编号异内容拒绝（409 stance_id_conflict）；改口须使用新编号，历次表态保留。
* 有效性（``is_current``）为派生状态：``stance.snapshot_id == 当前快照标识``。
* 仅接受对“已闭合且当前仍存在”的区间、当前责任片段中的责任方、匹配当前快照的
  表态；过期快照 / 区间不存在 / 区间仍开放一律拒绝且不落库。
"""
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app import chain, crud
from app.models import Cargo, Event, ResponsibilityStance, StanceType
from app.schemas import (
    ClosedIntervalConfirmation,
    PartyStance,
    ResponsibilitySegment,
    StanceCreate,
    StanceOut,
)
from app.timeutil import ensure_utc

# 汇总状态字面量
ALL_CONFIRMED = "all_confirmed"  # 全确认：每个当前责任方最新有效表态均为确认
DISPUTED = "disputed"            # 有异议：存在最新有效表态为异议
PENDING = "pending"              # 待确认：尚有责任方没有有效表态（且无异议）


@dataclass
class StanceWriteResult:
    stance: StanceOut
    replayed: bool


# ------------------------------------------------------------------ 内部工具


def _load_cargo(db, cargo_id: str, *, locked: bool = False) -> Cargo:
    if locked:
        stmt = select(Cargo).where(Cargo.cargo_id == cargo_id).with_for_update()
        cargo = db.scalars(stmt).first()
    else:
        cargo = db.get(Cargo, cargo_id)
    if cargo is None:
        raise crud.ApiError(404, "cargo_not_found", f"货物 {cargo_id} 不存在")
    cargo.started_at = ensure_utc(cargo.started_at)
    return cargo


def _load_events(db, cargo_id: str) -> list[Event]:
    stmt = (
        select(Event)
        .where(Event.cargo_id == cargo_id)
        .order_by(Event.occurred_at, Event.event_id)
    )
    events = list(db.scalars(stmt).all())
    for e in events:
        e.occurred_at = ensure_utc(e.occurred_at)
    return events


def _load_stances(db, cargo_id: str, interval_id: str | None = None):
    stmt = select(ResponsibilityStance).where(
        ResponsibilityStance.cargo_id == cargo_id
    )
    if interval_id is not None:
        stmt = stmt.where(ResponsibilityStance.interval_id == interval_id)
    stmt = stmt.order_by(
        ResponsibilityStance.created_at, ResponsibilityStance.stance_id
    )
    rows = list(db.scalars(stmt).all())
    for r in rows:
        r.created_at = ensure_utc(r.created_at)
    return rows


def _closed_excursions(cargo: Cargo, events: list[Event]):
    """重建链路并返回“已闭合”区间（按开始时间升序）。"""
    result = chain.rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), events
    )
    return [ex for ex in result.excursions if not ex.is_open]


def _find_closed(cargo: Cargo, events: list[Event], interval_id: str):
    """按开启测温事件ID定位一条已闭合区间；不存在/未闭合返回 None。"""
    for ex in _closed_excursions(cargo, events):
        if ex.start_event_id == interval_id:
            return ex
    return None


def _to_out(row: ResponsibilityStance, current_snapshot: str | None) -> StanceOut:
    return StanceOut(
        stance_id=row.stance_id,
        cargo_id=row.cargo_id,
        interval_id=row.interval_id,
        snapshot_id=row.snapshot_id,
        responsible_party=row.responsible_party,
        stance=row.stance_type,
        reason=row.reason,
        is_current=(current_snapshot is not None and row.snapshot_id == current_snapshot),
        created_at=ensure_utc(row.created_at),
    )


def _summarize(parties: list[str], latest: dict[str, ResponsibilityStance]) -> str:
    """依据当前责任方与其“最新有效表态”给出汇总状态。"""
    if len(latest) < len(parties):
        # 有责任方尚无有效表态；但若已有人异议则优先报“有异议”
        if any(r.stance_type is StanceType.object for r in latest.values()):
            return DISPUTED
        return PENDING
    if any(r.stance_type is StanceType.object for r in latest.values()):
        return DISPUTED
    return ALL_CONFIRMED


def _build_view(cargo: Cargo, excursion, stances) -> ClosedIntervalConfirmation:
    """组装一条已闭合区间的确认视图。"""
    snap = chain.snapshot_id(excursion)
    # 片段按时间升序、无零长；同一责任方可能在多个片段中重复出现（如 A->B->A），
    # 汇总按“去重后的责任方集合”判定（任一片段确认即覆盖该方全部责任）。
    parties = list(dict.fromkeys(seg.party for seg in excursion.segments))

    # 各责任方“当前快照下的最新有效表态”（stances 已按 created_at 升序）
    latest: dict[str, ResponsibilityStance] = {}
    for row in stances:
        if row.snapshot_id != snap or row.responsible_party not in parties:
            continue
        latest[row.responsible_party] = row  # 后出现的覆盖先前的 -> 最新

    party_stances = [
        PartyStance(
            responsible_party=r.responsible_party,
            stance=r.stance_type,
            reason=r.reason,
            stance_id=r.stance_id,
            created_at=ensure_utc(r.created_at),
        )
        for r in latest.values()
    ]

    return ClosedIntervalConfirmation(
        cargo_id=cargo.cargo_id,
        interval_id=excursion.start_event_id,
        start_event_id=excursion.start_event_id,
        end_event_id=excursion.end_event_id,
        start=excursion.start,
        end=excursion.end,
        snapshot_id=snap,
        segments=[
            ResponsibilitySegment(start=seg.start, end=seg.end, responsible_party=seg.party)
            for seg in excursion.segments
        ],
        party_stances=party_stances,
        summary_status=_summarize(parties, latest),
    )


# ------------------------------------------------------------------ 查询


def list_closed_intervals(db, cargo_id: str) -> list[ClosedIntervalConfirmation]:
    cargo = _load_cargo(db, cargo_id)
    events = _load_events(db, cargo_id)
    excursions = _closed_excursions(cargo, events)
    if not excursions:
        return []
    stances = _load_stances(db, cargo_id)
    return [_build_view(cargo, ex, stances) for ex in excursions]


def get_closed_interval(db, cargo_id: str, interval_id: str) -> ClosedIntervalConfirmation:
    cargo = _load_cargo(db, cargo_id)
    events = _load_events(db, cargo_id)

    result = chain.rebuild_chain(
        cargo.initial_holder, Decimal(cargo.temp_upper_limit), events
    )
    target = next(
        (ex for ex in result.excursions if ex.start_event_id == interval_id), None
    )
    # 区分“区间仍开放”与“区间不存在”：开放区间拒绝确认
    if target is not None and target.is_open:
        raise crud.ApiError(
            409,
            "interval_open",
            f"区间 {interval_id} 尚未闭合（未出现不超限测温），不接受责任确认",
        )
    if target is None:
        raise crud.ApiError(
            404,
            "interval_not_found",
            f"货物 {cargo_id} 不存在以 {interval_id} 开启的已闭合越温区间"
            "（可能从未有此区间，或晚到事件重建后其边界已变化）",
        )

    stances = _load_stances(db, cargo_id, interval_id)
    return _build_view(cargo, target, stances)


def list_stance_history(db, cargo_id: str, interval_id: str | None) -> list[StanceOut]:
    """返回表态历史（全部历次表态，含已失效）；区间已消失也可查。

    不传 interval_id 时返回该货物全部表态。
    """
    cargo = _load_cargo(db, cargo_id)
    events = _load_events(db, cargo_id)
    # 区间 -> 当前快照（仅当前仍存在的已闭合区间有快照）
    snaps = {
        ex.start_event_id: chain.snapshot_id(ex)
        for ex in _closed_excursions(cargo, events)
    }
    rows = _load_stances(db, cargo_id, interval_id)
    return [_to_out(row, snaps.get(row.interval_id)) for row in rows]


# ------------------------------------------------------------------ 提交


def _same_stance(existing: ResponsibilityStance, payload: StanceCreate) -> bool:
    return (
        existing.cargo_id == payload.cargo_id
        and existing.interval_id == payload.interval_id
        and existing.snapshot_id == payload.snapshot_id
        and existing.responsible_party == payload.responsible_party
        and existing.stance_type is payload.stance
        and existing.reason == payload.reason
    )


def submit_stance(db, payload: StanceCreate) -> StanceWriteResult:
    try:
        try:
            payload.normalize_and_validate()
        except ValueError as exc:
            raise crud.ApiError(422, "bad_stance_shape", str(exc))

        # 与事件写入同一把货物行锁：表态提交与同货物事件写入串行化
        cargo = _load_cargo(db, payload.cargo_id, locked=True)
        events = _load_events(db, payload.cargo_id)

        # 幂等优先：已存表态编号同内容重传 -> 返回原记录（即使快照已失效）
        existing = db.get(ResponsibilityStance, payload.stance_id)
        if existing is not None:
            if _same_stance(existing, payload):
                db.rollback()  # 仅释放货物行锁，未做任何修改
                current = _current_snapshot_for(db, cargo, existing.interval_id)
                return StanceWriteResult(
                    stance=_to_out(existing, current), replayed=True
                )
            raise crud.ApiError(
                409,
                "stance_id_conflict",
                f"表态编号 {payload.stance_id} 已存在且内容不同，拒绝覆盖；"
                "改口请使用新的表态编号",
            )

        # 区间必须当前存在且已闭合（开放区间拒绝）
        result = chain.rebuild_chain(
            cargo.initial_holder, Decimal(cargo.temp_upper_limit), events
        )
        target = None
        for ex in result.excursions:
            if ex.start_event_id == payload.interval_id:
                target = ex
                break
        if target is None:
            raise crud.ApiError(
                404,
                "interval_not_found",
                f"货物 {payload.cargo_id} 不存在以 {payload.interval_id} 开启的"
                "越温区间（晚到事件重建后该区间可能已变化）",
            )
        if target.is_open:
            raise crud.ApiError(
                409,
                "interval_open",
                f"区间 {payload.interval_id} 尚未闭合，不接受责任确认",
            )

        # 快照校验：过期（边界或责任片段已变化）拒绝
        current_snapshot = chain.snapshot_id(target)
        if payload.snapshot_id != current_snapshot:
            raise crud.ApiError(
                409,
                "snapshot_stale",
                f"快照 {payload.snapshot_id} 已过期，当前快照为 {current_snapshot}；"
                "晚到事件可能改变了区间边界或责任片段，请重新查询后再表态",
            )

        # 责任方必须出现在当前责任片段中
        parties = [seg.party for seg in target.segments]
        if payload.responsible_party not in parties:
            raise crud.ApiError(
                422,
                "party_not_in_segment",
                f"责任方 {payload.responsible_party} 不在区间 {payload.interval_id} "
                f"当前责任片段 {parties} 中",
            )

        row = ResponsibilityStance(
            stance_id=payload.stance_id,
            cargo_id=payload.cargo_id,
            interval_id=payload.interval_id,
            snapshot_id=payload.snapshot_id,
            responsible_party=payload.responsible_party,
            stance_type=payload.stance,
            reason=payload.reason,
        )
        db.add(row)
        db.commit()
    except crud.ApiError:
        db.rollback()
        raise
    except IntegrityError:
        db.rollback()
        raise crud.ApiError(
            409,
            "integrity_conflict",
            f"表态 {payload.stance_id} 与既有数据冲突（编号重复 / 字段约束）",
        )

    return StanceWriteResult(stance=_to_out(row, current_snapshot), replayed=False)


def _current_snapshot_for(db, cargo: Cargo, interval_id: str) -> str | None:
    """重放分支辅助：返回某区间当前快照；区间已不存在/开放则为 None（表态失效）。"""
    events = _load_events(db, cargo.cargo_id)
    excursion = _find_closed(cargo, events, interval_id)
    if excursion is None:
        return None
    return chain.snapshot_id(excursion)

"""保管链重建与越温责任拆分（纯函数，便于单测）。

规则
----
* 事件按 occurred_at 排序；同一时刻测温先于交接处理
  （“交接时刻起归接收方”——交接在该时刻已生效）。
* 高于上限的测温开启越温区间；越温期间首次不超限（<= 上限）的测温关闭区间。
* 区间跨交接时按交接时刻拆分：[区间起, 交接时刻) 归交出方，
  [交接时刻, ...) 归接收方；交接与区间起点同刻时直接由接收方担责。
* 区间内重复超限测温不影响区间；区间外不超限测温无影响。
* 不产生零长度片段。
"""
import hashlib
import json
from dataclasses import dataclass
from datetime import timezone
from decimal import Decimal

from app.models import Event, EventType


@dataclass(frozen=True)
class Segment:
    start: object          # datetime，含
    end: object | None     # datetime，不含；None 表示未闭合
    party: str


@dataclass(frozen=True)
class ExcursionResult:
    start: object
    end: object | None
    segments: tuple[Segment, ...]
    start_event_id: str | None = None   # 开启区间的测温事件ID（区间定位键）
    end_event_id: str | None = None     # 闭合区间的测温事件ID（未闭合为 None）

    @property
    def is_open(self) -> bool:
        return self.end is None


def _utc_iso(dt) -> str:
    """时间统一格式化为 UTC 的 ISO 8601（Z 结尾），供快照规范化使用。"""
    if dt.tzinfo is None:
        raise ValueError("快照计算要求带时区的时间")
    return dt.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def snapshot_id(excursion: ExcursionResult) -> str:
    """计算已闭合区间的当前快照标识。

    快照由「区间起止（开启/闭合测温事件）+ 责任片段（起止与责任方）」决定：
    其中任一要素变化，快照标识即变化（旧表态随之失效）；要素不变则快照稳定
    （既有表态继续有效）。输出为 ``snap-`` 加 64 位 SHA-256 十六进制。
    """
    if excursion.is_open:
        raise ValueError("未闭合区间没有快照标识")
    material = {
        "version": 1,
        "start_event_id": excursion.start_event_id,
        "end_event_id": excursion.end_event_id,
        "start": _utc_iso(excursion.start),
        "end": _utc_iso(excursion.end),
        "segments": [
            {
                "start": _utc_iso(seg.start),
                "end": _utc_iso(seg.end),
                "responsible_party": seg.party,
            }
            for seg in excursion.segments
        ],
    }
    blob = json.dumps(
        material, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    digest = hashlib.sha256(blob.encode("utf-8")).hexdigest()
    return f"snap-{digest}"


@dataclass(frozen=True)
class ChainResult:
    current_holder: str
    excursions: tuple[ExcursionResult, ...]


def _sorted_events(events: list[Event]) -> list[Event]:
    # 同一时刻：measurement 在 handover 之前（交接在测温“之后”生效）
    type_order = {EventType.measurement: 0, EventType.handover: 1}
    return sorted(
        events,
        key=lambda e: (e.occurred_at, type_order[e.event_type], e.event_id),
    )


def rebuild_chain(
    initial_holder: str,
    temp_upper_limit: Decimal,
    events: list[Event],
) -> ChainResult:
    """按事件流重建当前保管方与全部越温区间（含责任片段）。"""
    limit = Decimal(temp_upper_limit)
    holder = initial_holder

    open_start = None                  # 当前越温区间起点
    open_event_id = None               # 开启区间的测温事件ID
    segments: list[Segment] = []       # 当前区间已切分出的片段（最后一段开放）
    excursions: list[ExcursionResult] = []

    for e in _sorted_events(events):
        if e.event_type is EventType.measurement:
            over = Decimal(e.temperature) > limit
            if open_start is None and over:
                # 开启越温：当前保管方自测温时刻起担责
                open_start = e.occurred_at
                open_event_id = e.event_id
                segments = [Segment(e.occurred_at, None, holder)]
            elif open_start is not None and not over:
                # 首次不超限测温 -> 在该时刻闭合
                last = segments[-1]
                if last.start < e.occurred_at:
                    segments[-1] = Segment(last.start, e.occurred_at, last.party)
                else:
                    segments.pop()  # 理论不会发生（同刻两笔测温已拒绝）
                excursions.append(
                    ExcursionResult(
                        open_start,
                        e.occurred_at,
                        tuple(segments),
                        start_event_id=open_event_id,
                        end_event_id=e.event_id,
                    )
                )
                open_start = None
                open_event_id = None
                segments = []
        else:  # handover
            if open_start is not None:
                # 跨交接拆分责任片段
                prev = segments[-1]
                if e.occurred_at > prev.start:
                    # 交出方承担 [prev.start, 交接时刻)，接收方自交接时刻起
                    segments[-1] = Segment(prev.start, e.occurred_at, prev.party)
                    segments.append(Segment(e.occurred_at, None, e.receiver))
                else:
                    # 交接与区间起点（同刻的开启测温）同一时刻：
                    # 交出方片段为零长，直接由接收方自该时刻起担责
                    segments[-1] = Segment(e.occurred_at, None, e.receiver)
            holder = e.receiver

    if open_start is not None:
        # 未闭合区间：最后片段保持开放终点，标记“未结束”
        excursions.append(
            ExcursionResult(
                open_start,
                None,
                tuple(segments),
                start_event_id=open_event_id,
                end_event_id=None,
            )
        )

    return ChainResult(current_holder=holder, excursions=tuple(excursions))


def holder_at(initial_holder: str, events: list[Event], at) -> str:
    """返回 at 时刻（该时刻的交接已生效后）的保管方。

    用于写入校验：交接的交出方必须等于“交接时刻的保管方”。
    """
    holder = initial_holder
    for e in _sorted_events(events):
        if e.occurred_at <= at and e.event_type is EventType.handover:
            holder = e.receiver
    return holder

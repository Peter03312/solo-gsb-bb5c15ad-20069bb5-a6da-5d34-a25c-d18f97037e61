"""SQLAlchemy ORM 模型：货物与冷链事件（测温 / 交接）。"""
import enum
from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Numeric,
    String,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database import Base


class EventType(str, enum.Enum):
    """事件类型。成员名与 PG enum 字面量保持一致。"""

    measurement = "measurement"  # 测温
    handover = "handover"        # 交接


class StanceType(str, enum.Enum):
    """责任方对其责任片段的表态。成员名与 PG enum 字面量保持一致。"""

    confirm = "confirm"      # 确认
    object = "object"        # 异议（须给出理由）


class Cargo(Base):
    __tablename__ = "cargo"

    cargo_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temp_upper_limit: Mapped[Decimal] = mapped_column(
        Numeric(10, 3), nullable=False
    )
    initial_holder: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class Event(Base):
    """一条测温或交接事件。

    测温：temperature 有值，giver/receiver 为空。
    交接：giver/receiver 有值，temperature 为空。
    """

    __tablename__ = "event"
    __table_args__ = (
        CheckConstraint(
            "(event_type = 'measurement' AND temperature IS NOT NULL "
            "AND giver IS NULL AND receiver IS NULL) "
            "OR (event_type = 'handover' AND temperature IS NULL "
            "AND giver IS NOT NULL AND receiver IS NOT NULL AND giver <> receiver)",
            name="chk_event_shape",
        ),
        # 同一货物同一时刻只能有一笔测温（跨类型不互斥：测温与交接可同刻发生）
        Index(
            "ux_event_cargo_time_measurement",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'measurement'::event_type"),
            sqlite_where=text("event_type = 'measurement'"),
        ),
        # 同一货物同一时刻只能有一笔交接
        Index(
            "ux_event_cargo_time_handover",
            "cargo_id",
            "occurred_at",
            unique=True,
            postgresql_where=text("event_type = 'handover'::event_type"),
            sqlite_where=text("event_type = 'handover'"),
        ),
    )

    event_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    event_type: Mapped[EventType] = mapped_column(
        Enum(EventType, name="event_type", create_constraint=True),
        nullable=False,
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    temperature: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 3), nullable=True
    )
    giver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    receiver: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )


class ResponsibilityStance(Base):
    """责任方对某条已闭合越温区间的一次表态（确认 / 异议）。

    表态不可修改：改口即提交一条新表态（新编号），历次表态全部保留。
    表态在提交时绑定当时的快照标识；晚到事件重建后，若区间边界或责任片段
    变化（快照标识变化），旧表态标记为失效（记录仍可查），未变化则继续有效。
    有效性为派生状态（由 snapshot_id 是否等于当前快照决定），不落库标记。
    """

    __tablename__ = "responsibility_stance"
    __table_args__ = (
        CheckConstraint(
            "(stance_type = 'confirm' AND reason IS NULL) "
            "OR (stance_type = 'object' AND reason IS NOT NULL "
            "AND length(trim(reason)) > 0)",
            name="chk_stance_shape",
        ),
        Index("ix_stance_cargo_interval", "cargo_id", "interval_id"),
    )

    stance_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    cargo_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("cargo.cargo_id", ondelete="RESTRICT"),
        nullable=False,
    )
    # 区间定位键 = 开启该区间的测温事件ID；仅作业务键存储，不与 event 表硬绑定，
    # 因为晚到事件重建后该区间可能已不再存在而表态历史仍须可查。
    interval_id: Mapped[str] = mapped_column(String(64), nullable=False)
    snapshot_id: Mapped[str] = mapped_column(String(80), nullable=False)
    responsible_party: Mapped[str] = mapped_column(String(128), nullable=False)
    stance_type: Mapped[StanceType] = mapped_column(
        Enum(StanceType, name="stance_type", create_constraint=True),
        nullable=False,
    )
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(timezone.utc),
    )

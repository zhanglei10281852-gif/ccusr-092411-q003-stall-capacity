"""SQLite 持久化：事件日志 + 状态投影。

设计要点
--------
* 全部写操作持有同一把进程内可重入锁，在单连接、``IMMEDIATE`` 事务内完成，
  并发占位在容量检查与写入之间不会被插队，容量不会被穿透。
* 事件只追加（event_log），状态投影同步更新；同一 ``request_id`` 重放
  直接返回首次结果，不重复冻结、释放或计费。
* 搬运任务即“空间账”的分录：planned/approved 冻结容量，moving 保持冻结，
  placed 实际在场，released/expired/cancelled 释放。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime
from pathlib import Path

SCHEMA = """
create table if not exists event_log (
    event_id      text primary key,
    request_id    text,
    event_type    text not null,
    aggregate_id  text not null,
    actor         text not null,
    payload       text not null,
    occurred_at   text not null
);
create unique index if not exists idx_event_request on event_log(request_id)
    where request_id is not null;

create table if not exists tenants (
    tenant_id text primary key,
    name      text not null,
    is_admin  integer not null default 0
);

create table if not exists spaces (
    space_id           text primary key,
    kind               text not null,
    name               text not null,
    area_m2            real not null,
    max_weight_kg      real not null,
    max_height_m       real not null,
    is_fire_lane       integer not null default 0,
    state              text not null default 'active',
    owner_tenant_id    text references tenants(tenant_id),
    parent_space_id    text references spaces(space_id),
    allowed_categories text not null default ''
);

create table if not exists cargo_lots (
    lot_id             text primary key,
    tenant_id          text not null references tenants(tenant_id),
    category           text not null,
    length_m           real not null,
    width_m            real not null,
    height_m           real not null,
    weight_kg          real not null,
    stackable          integer not null,
    stacks_on          text not null default '',
    taboo_adjacent     text not null default '',
    declared_at        text,
    expected_departure text
);

create table if not exists move_tasks (
    task_id         text primary key,
    lot_id          text not null references cargo_lots(lot_id),
    tenant_id       text not null references tenants(tenant_id),
    from_space_id   text references spaces(space_id),
    to_space_id     text not null references spaces(space_id),
    planned_in_at   text not null,
    planned_out_at  text not null,
    state           text not null default 'planned',
    started_at      text,
    completed_at    text,
    actual_out_at   text,
    review_decision text,
    stacked_on_task text references move_tasks(task_id),
    permit_id       text references emergency_permits(permit_id),
    request_id      text
);

create table if not exists emergency_permits (
    permit_id    text primary key,
    space_id     text references spaces(space_id),
    reason       text not null,
    requested_by text not null,
    approver_a   text,
    approver_b   text,
    valid_from   text not null,
    valid_until  text not null,
    state        text not null default 'pending'
);

create table if not exists audit_actions (
    action_id    integer primary key autoincrement,
    actor        text not null,
    action       text not null,
    target_type  text not null,
    target_id    text not null,
    detail       text not null default '',
    occurred_at  text not null
);

create table if not exists space_adjacency (
    space_id    text not null references spaces(space_id),
    neighbor_id text not null references spaces(space_id),
    primary key (space_id, neighbor_id)
);

create table if not exists charges (
    charge_id   integer primary key autoincrement,
    task_id     text not null references move_tasks(task_id),
    tenant_id   text not null,
    amount      real not null,
    kind        text not null,
    created_at  text not null,
    unique(task_id, kind)
);
"""

# 仍占用容量（冻结或在场）的任务状态
OCCUPYING_STATES = ("planned", "approved", "moving", "placed", "pending_review")


def _iso(dt: datetime) -> str:
    return dt.isoformat()


def _parse(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value is not None else None


def _csv(values) -> str:
    return ",".join(sorted(values))


class Store:
    """单连接 SQLite 存储；内存模式与文件模式行为一致。"""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self._lock = threading.RLock()
        self.path = str(path)
        self.conn = sqlite3.connect(
            self.path, check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("pragma journal_mode=WAL")
        self.conn.execute("pragma foreign_keys=ON")
        self.conn.execute("pragma busy_timeout=5000")
        self.conn.executescript(SCHEMA)

    # ---- 事务 ---------------------------------------------------------

    @property
    def lock(self) -> threading.RLock:
        return self._lock

    def begin_immediate(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.commit()

    def rollback(self) -> None:
        self.conn.rollback()

    # ---- 事件与审计 ----------------------------------------------------

    def append_event(
        self,
        event_type: str,
        aggregate_id: str,
        actor: str,
        payload: dict,
        occurred_at: datetime,
        event_id: str | None = None,
        request_id: str | None = None,
    ) -> str:
        eid = event_id or (
            f"evt-{aggregate_id}-{event_type}-"
            f"{occurred_at.timestamp():.6f}-{uuid.uuid4().hex[:8]}"
        )
        self.conn.execute(
            "insert into event_log(event_id, request_id, event_type, aggregate_id, "
            "actor, payload, occurred_at) values (?, ?, ?, ?, ?, ?, ?)",
            (
                eid, request_id, event_type, aggregate_id, actor,
                json.dumps(payload, ensure_ascii=False, sort_keys=True),
                _iso(occurred_at),
            ),
        )
        return eid

    def find_event_by_request(self, request_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from event_log where request_id = ? order by occurred_at limit 1",
            (request_id,),
        ).fetchone()

    def events_for(self, aggregate_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "select * from event_log where aggregate_id = ? order by occurred_at, rowid",
            (aggregate_id,),
        ).fetchall()

    def all_events(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "select * from event_log order by occurred_at, rowid"
        ).fetchall()

    def audit(self, actor: str, action: str, target_type: str,
              target_id: str, detail: dict, now: datetime) -> None:
        self.conn.execute(
            "insert into audit_actions(actor, action, target_type, target_id, detail, occurred_at) "
            "values (?, ?, ?, ?, ?, ?)",
            (actor, action, target_type, target_id,
             json.dumps(detail, ensure_ascii=False), _iso(now)),
        )

    def list_audit(self, target_id: str | None = None) -> list[sqlite3.Row]:
        if target_id is None:
            return self.conn.execute(
                "select * from audit_actions order by action_id"
            ).fetchall()
        return self.conn.execute(
            "select * from audit_actions where target_id = ? order by action_id",
            (target_id,),
        ).fetchall()

    # ---- 租户 ----------------------------------------------------------

    def add_tenant(self, tenant_id: str, name: str, is_admin: bool = False) -> None:
        self.conn.execute(
            "insert or ignore into tenants(tenant_id, name, is_admin) values (?, ?, ?)",
            (tenant_id, name, 1 if is_admin else 0),
        )

    def is_admin(self, tenant_id: str) -> bool:
        row = self.conn.execute(
            "select is_admin from tenants where tenant_id = ?", (tenant_id,)
        ).fetchone()
        return bool(row and row["is_admin"])

    def tenant_exists(self, tenant_id: str) -> bool:
        return self.conn.execute(
            "select 1 from tenants where tenant_id = ?", (tenant_id,)
        ).fetchone() is not None

    # ---- 空间 ----------------------------------------------------------

    def add_space(self, space: dict) -> None:
        self.conn.execute(
            "insert into spaces(space_id, kind, name, area_m2, max_weight_kg, "
            "max_height_m, is_fire_lane, state, owner_tenant_id, parent_space_id, "
            "allowed_categories) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                space["space_id"], space["kind"], space["name"],
                space["area_m2"], space["max_weight_kg"], space["max_height_m"],
                1 if space.get("is_fire_lane") else 0,
                space.get("state", "active"),
                space.get("owner_tenant_id"),
                space.get("parent_space_id"),
                _csv(space.get("allowed_categories", ())),
            ),
        )

    def get_space_row(self, space_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from spaces where space_id = ?", (space_id,)
        ).fetchone()

    def list_space_rows(self) -> list[sqlite3.Row]:
        return self.conn.execute("select * from spaces order by space_id").fetchall()

    def set_space_state(self, space_id: str, state: str) -> None:
        self.conn.execute(
            "update spaces set state = ? where space_id = ?", (state, space_id)
        )

    # ---- 相邻关系 ------------------------------------------------------

    def add_adjacency(self, space_id: str, neighbor_id: str) -> None:
        if space_id == neighbor_id:
            raise ValueError("空间不能与自身相邻")
        self.conn.execute(
            "insert or ignore into space_adjacency(space_id, neighbor_id) values (?, ?)",
            (space_id, neighbor_id),
        )
        self.conn.execute(
            "insert or ignore into space_adjacency(space_id, neighbor_id) values (?, ?)",
            (neighbor_id, space_id),
        )

    def neighbors_of(self, space_id: str) -> list[str]:
        return [
            r["neighbor_id"]
            for r in self.conn.execute(
                "select neighbor_id from space_adjacency where space_id = ? "
                "order by neighbor_id",
                (space_id,),
            ).fetchall()
        ]

    # ---- 货物 ----------------------------------------------------------

    def insert_lot(self, lot: dict) -> None:
        self.conn.execute(
            "insert into cargo_lots(lot_id, tenant_id, category, length_m, width_m, "
            "height_m, weight_kg, stackable, stacks_on, taboo_adjacent, "
            "declared_at, expected_departure) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                lot["lot_id"], lot["tenant_id"], lot["category"],
                lot["length_m"], lot["width_m"], lot["height_m"], lot["weight_kg"],
                1 if lot.get("stackable") else 0,
                _csv(lot.get("stacks_on", ())),
                _csv(lot.get("taboo_adjacent", ())),
                _iso(lot["declared_at"]) if lot.get("declared_at") else None,
                _iso(lot["expected_departure"]) if lot.get("expected_departure") else None,
            ),
        )

    def get_lot_row(self, lot_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from cargo_lots where lot_id = ?", (lot_id,)
        ).fetchone()

    # ---- 搬运任务 ------------------------------------------------------

    def insert_task(self, t: dict) -> None:
        self.conn.execute(
            "insert into move_tasks(task_id, lot_id, tenant_id, from_space_id, "
            "to_space_id, planned_in_at, planned_out_at, state, stacked_on_task, "
            "permit_id, request_id) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                t["task_id"], t["lot_id"], t["tenant_id"], t.get("from_space_id"),
                t["to_space_id"], _iso(t["planned_in_at"]), _iso(t["planned_out_at"]),
                t.get("state", "planned"), t.get("stacked_on_task"),
                t.get("permit_id"), t.get("request_id"),
            ),
        )

    def get_task_row(self, task_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from move_tasks where task_id = ?", (task_id,)
        ).fetchone()

    def list_task_rows(self, *, tenant_id: str | None = None) -> list[sqlite3.Row]:
        if tenant_id is None:
            return self.conn.execute(
                "select * from move_tasks order by planned_in_at, task_id"
            ).fetchall()
        return self.conn.execute(
            "select * from move_tasks where tenant_id = ? order by planned_in_at, task_id",
            (tenant_id,),
        ).fetchall()

    def tasks_for_lot(self, lot_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "select * from move_tasks where lot_id = ? order by planned_in_at",
            (lot_id,),
        ).fetchall()

    def tasks_touching_space(self, space_id: str) -> list[sqlite3.Row]:
        placeholders = ",".join("?" for _ in OCCUPYING_STATES)
        return self.conn.execute(
            f"select * from move_tasks where to_space_id = ? "
            f"and state in ({placeholders}) order by planned_in_at, task_id",
            (space_id, *OCCUPYING_STATES),
        ).fetchall()

    def inflight_tasks(self) -> list[sqlite3.Row]:
        return self.conn.execute(
            "select * from move_tasks where state in ('moving', 'pending_review') "
            "order by planned_in_at, task_id"
        ).fetchall()

    def planned_tasks_into(self, space_id: str) -> list[sqlite3.Row]:
        """尚未搬入（planned/approved/moving）且目标为该空间的任务。"""
        return self.conn.execute(
            "select * from move_tasks where to_space_id = ? "
            "and state in ('planned', 'approved', 'moving') "
            "order by planned_in_at, task_id",
            (space_id,),
        ).fetchall()

    def update_task_state(self, task_id: str, state: str, **fields) -> None:
        assignments = ["state = ?"]
        values: list = [state]
        for key, value in fields.items():
            assignments.append(f"{key} = ?")
            if isinstance(value, datetime):
                value = _iso(value)
            values.append(value)
        values.append(task_id)
        self.conn.execute(
            f"update move_tasks set {', '.join(assignments)} where task_id = ?", values
        )

    # ---- 紧急放行 ------------------------------------------------------

    def insert_permit(self, p: dict) -> None:
        self.conn.execute(
            "insert into emergency_permits(permit_id, space_id, reason, requested_by, "
            "valid_from, valid_until, state) values (?, ?, ?, ?, ?, ?, 'pending')",
            (
                p["permit_id"], p.get("space_id"), p["reason"], p["requested_by"],
                _iso(p["valid_from"]), _iso(p["valid_until"]),
            ),
        )

    def get_permit_row(self, permit_id: str) -> sqlite3.Row | None:
        return self.conn.execute(
            "select * from emergency_permits where permit_id = ?", (permit_id,)
        ).fetchone()

    def approve_permit(self, permit_id: str, approver: str) -> None:
        row = self.get_permit_row(permit_id)
        if row["approver_a"] is None:
            self.conn.execute(
                "update emergency_permits set approver_a = ? where permit_id = ?",
                (approver, permit_id),
            )
        else:
            self.conn.execute(
                "update emergency_permits set approver_b = ?, state = 'granted' "
                "where permit_id = ?",
                (approver, permit_id),
            )

    def permit_approvals(self, permit_id: str) -> tuple[str | None, str | None]:
        row = self.get_permit_row(permit_id)
        return row["approver_a"], row["approver_b"]

    def active_permits_for_space(self, space_id: str, at: datetime) -> list[sqlite3.Row]:
        return self.conn.execute(
            "select * from emergency_permits where state = 'granted' "
            "and (space_id is null or space_id = ?) "
            "and valid_from <= ? and valid_until > ?",
            (space_id, _iso(at), _iso(at)),
        ).fetchall()

    def expire_due_permits(self, now: datetime) -> list[str]:
        rows = self.conn.execute(
            "select permit_id from emergency_permits "
            "where state in ('pending', 'granted') and valid_until <= ?",
            (_iso(now),),
        ).fetchall()
        self.conn.execute(
            "update emergency_permits set state = 'expired' "
            "where state in ('pending', 'granted') and valid_until <= ?",
            (_iso(now),),
        )
        return [r["permit_id"] for r in rows]

    def tasks_using_permit(self, permit_id: str) -> list[sqlite3.Row]:
        return self.conn.execute(
            "select * from move_tasks where permit_id = ?", (permit_id,)
        ).fetchall()

    # ---- 计费 ----------------------------------------------------------

    def add_charge(self, task_id: str, tenant_id: str, amount: float,
                   kind: str, now: datetime) -> bool:
        try:
            self.conn.execute(
                "insert into charges(task_id, tenant_id, amount, kind, created_at) "
                "values (?, ?, ?, ?, ?)",
                (task_id, tenant_id, amount, kind, _iso(now)),
            )
            return True
        except sqlite3.IntegrityError:
            return False  # 同一任务同类费用只登记一次，重放不重复计费

    def list_charges(self, tenant_id: str | None = None) -> list[sqlite3.Row]:
        if tenant_id is None:
            return self.conn.execute("select * from charges order by charge_id").fetchall()
        return self.conn.execute(
            "select * from charges where tenant_id = ? order by charge_id", (tenant_id,)
        ).fetchall()

    def close(self) -> None:
        self.conn.close()

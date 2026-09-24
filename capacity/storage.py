"""SQLite 存储：状态表 + 只追加事件日志。

并发模型：所有写事务使用 BEGIN IMMEDIATE 立即拿写锁，
容量校验在"读取候选占用 -> 校验 -> 写入"同一事务内完成，
SQLite 的库级写锁保证两个并发预订不可能同时通过校验。
"""
from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
create table if not exists tenants (
  tenant_id   text primary key,
  name        text not null,
  created_at  text not null
);

create table if not exists spaces (
  space_id    text primary key,
  kind        text not null,              -- fixed_stall/temp_area/shelf_level/fire_lane
  owner_id    text,                        -- 固定档口归属租户
  parent_id   text,                        -- 货架层位所属货架组
  level_no    integer,                     -- 层号，地面=0，层位从1起
  x real not null, y real not null,
  w real not null, d real not null,
  net_height  real not null,               -- 净高
  floor_load  real not null,               -- 千克/平方米（地面承重）
  level_load  real not null default 0,     -- 货架层总承重（千克）
  status      text not null default 'open',-- open/closed
  closed_reason text,
  attrs       text not null default '{}'
);

create table if not exists adjacency_taboos (
  category_a text not null,
  category_b text not null,
  min_gap    real not null,                -- 米
  primary key (category_a, category_b)
);

create table if not exists cargo (
  cargo_id    text primary key,
  tenant_id   text not null,
  category    text not null,
  length real not null, width real not null,
  height real not null, weight real not null,
  stackable integer not null,
  max_layers integer not null,
  bear_load real not null,
  in_transit integer not null default 1,   -- 1=在途未到
  declared_at text not null
);

-- 空间账核心：带时间区间的占用凭证
create table if not exists holds (
  hold_id     text primary key,
  space_id    text not null,
  cargo_id    text not null,
  tenant_id   text not null,
  x real not null, y real not null,       -- 空间坐标系内偏移
  stack_level integer not null default 0,  -- 0=直接落地/层板面；>=1 落在同空间下层货上
  start_at    text not null,               -- 预计搬入
  leave_at    text not null,               -- 预计离场
  status      text not null,               -- reserved/placed/released/cancelled/expired
  idem_key    text not null,
  grant_id    text,                        -- 若由紧急放行覆盖边界，指向 grant
  created_at  text not null,
  placed_at   text,
  released_at text,
  unique (idem_key)
);
create index if not exists idx_holds_space_time on holds(space_id, start_at, leave_at);
create index if not exists idx_holds_cargo on holds(cargo_id);
create index if not exists idx_holds_status on holds(status);

create table if not exists move_tasks (
  move_id     text primary key,
  tenant_id   text not null,
  status      text not null,               -- planned/approved/moving/completed/cancelled
  idem_key    text not null unique,
  created_at  text not null
);
create table if not exists move_steps (
  move_id     text not null,
  step_no     integer not null,
  cargo_id    text not null,
  from_space  text, from_hold text,
  to_space    text not null,
  to_x real, to_y real, to_level integer,
  start_at    text not null,
  leave_at    text not null,
  status      text not null default 'pending', -- pending/in_progress/done/cancelled
  new_hold_id text,
  primary key (move_id, step_no)
);

create table if not exists emergency_grants (
  grant_id    text primary key,
  tenant_id   text not null,
  space_id    text not null,
  override    text not null,               -- fire_lane/ownership/closure
  reason      text not null,
  requested_by text not null,
  approved_by text,                         -- 第二名批准人
  requested_at text not null,
  approved_at text,
  expires_at  text not null,
  status      text not null,               -- pending_second_approval/active/reclaimed/rejected
  idem_key    text not null unique
);

create table if not exists violations (
  violation_id text primary key,
  kind        text not null,               -- FIRE_LANE_BLOCKED/UNAUTHORIZED_OCCUPANCY/LEDGER_MISMATCH/OVERSTAY
  space_id    text,
  cargo_id    text,
  tenant_id   text,
  detail      text not null,
  status      text not null default 'open', -- open/resolved
  observed_by text not null,
  observed_at text not null,
  resolved_at text,
  clearance_order_id text
);

create table if not exists clearance_orders (
  order_id    text primary key,
  violation_id text not null,
  tenant_id   text,
  space_id    text,
  cargo_id    text,
  plan_id     text,
  status      text not null default 'open', -- open/done
  created_by  text not null,
  created_at  text not null,
  done_at     text
);

create table if not exists relocation_plans (
  plan_id     text primary key,
  reason      text not null,               -- clearance/closure/rebalance
  status      text not null default 'open', -- open/executing/done/cancelled
  created_by  text not null,
  created_at  text not null
);
create table if not exists relocation_steps (
  plan_id     text not null,
  step_no     integer not null,
  cargo_id    text not null,
  from_space  text,
  to_space    text not null,
  to_x real, to_y real, to_level integer,
  start_at    text not null,
  leave_at    text not null,
  status      text not null default 'pending', -- pending/done/skipped/cancelled
  move_id     text,
  primary key (plan_id, step_no)
);

-- 计费台账：每步搬运/每次占位唯一计费，绝不重复计费
create table if not exists ledger (
  entry_id    text primary key,
  idem_key    text not null unique,
  tenant_id   text not null,
  kind        text not null,               -- move_charge/hold_charge/...
  ref_type    text not null,
  ref_id      text not null,
  amount      real not null,
  currency    text not null default 'CNY',
  created_at  text not null
);

-- 崩溃恢复：对中断 move 的人工/自动判定
create table if not exists recovery_decisions (
  move_id     text primary key,
  decision    text not null,               -- resume/revert/manual
  reason      text not null,
  decided_by  text not null,
  decided_at  text not null
);

-- 只追加事件日志（跨区域移动的每一步都在此留痕）
create table if not exists event_log (
  event_id    text primary key,
  event_type  text not null,
  aggregate_id text not null,
  occurred_at text not null,
  actor_id    text,
  payload     text not null default '{}'
);
create index if not exists idx_events_agg on event_log(aggregate_id, occurred_at);
create index if not exists idx_events_type on event_log(event_type, occurred_at);
"""


def connect(db_path: str | Path = ":memory:") -> sqlite3.Connection:
    connection = sqlite3.connect(db_path)
    connection.row_factory = sqlite3.Row
    connection.execute("pragma foreign_keys=on")
    connection.execute("pragma journal_mode=wal")
    connection.execute("pragma busy_timeout=5000")
    connection.executescript(SCHEMA)
    return connection


def now_iso(now: datetime) -> str:
    return now.isoformat()


def parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def dumps(payload: Any) -> str:
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)

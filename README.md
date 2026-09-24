# 档口峰值容量治理

把安全边界（消防通道、地面/层板承重、净高、相邻禁忌）与经营安排（在途预订、
预计离场、车辆延误、区域封闭、强制清退）记进**同一本空间账**的领域服务。
纯 Python 标准库实现，SQLite 持久化，无外部依赖。

## 模型

- **space**：`fixed_stall`（固定档口，带归属租户）、`temp_area`（临时区域）、
  `shelf_level`（货架层位，带层承重）、`fire_lane`（消防通道，永久禁占）。
  每个空间有全局矩形坐标、净高、地面承重；相邻空间的几何关系直接在坐标系里计算。
- **cargo_lot**：货物批次，尺寸/重量/品类/可叠放/最大层数/顶面承载，可标记在途。
- **hold（空间账核心）**：某批货物在时间区间 `[搬入, 离场)` 内对某空间某位置
  （局部 x/y + 叠放层位）的占用凭证，状态
  `reserved → placed → released`，另有 `cancelled / expired`。
- **move_task / move_step**：跨区域搬运，分步执行、顺序约束、逐步留痕。
- **relocation_plan**：清退/封闭生成的可执行腾挪顺序。
- **occupancy_violation / clearance_order**：巡场账实比对与强制清退工单。
- **emergency_grant**：紧急放行，双人批准，到期回收。
- **ledger**：计费台账，幂等键保证不重复计费。
- **event_log**：只追加事件，跨区域移动的每一步都可追查。

## 规则（`capacity/engine.py`，纯函数可独立测试）

在候选区间被其他占位端点切成的每个**时间片**上，同时在场的货物参与判定：

1. **消防通道**：足迹与任意 fire_lane 相交即拒（紧急放行可限时覆盖）。
2. **平面冲突**：同层面积重叠即 `AREA_CONFLICT`；边相切不算。
3. **叠放**：上层必须落在同空间下层的支承面内；层号必须连续（悬空即拒）；
   受链上每个货物的 `max_layers`、上下层承重约束。
4. **净高**：一条竖向链的总高不得超过空间净高。
5. **承重**：地面按落地垛投影面积算压强（kg/m²）；货架层算总重。
6. **相邻禁忌**：跨空间品类对的矩形净距不足即拒。
7. **归属/封闭/边界**：固定档口仅归属租户可用；封闭窗口内不可预订；
   足迹不得超出空间边界。

## 关键业务约定

- **容量不可穿透**：每次写入在 `BEGIN IMMEDIATE` 事务内完成
  "读占用 → 重算 → 写凭证"，配合唯一约束与幂等键；并发抢同一货位只有一方成功
  （见 `tests/test_concurrency.py`）。
- **调整边界**：销售加快/车辆延误只能改 `reserved`（尚未搬入）的计划；
  已 `placed` 的货物改期被拒，只能走腾挪。
- **超期**：未到场预订到期直接过期释放；已到场超期记 `OVERSTAY`，
  货物物理上仍占容量（离场时间延展），直到真正释放或清退。
- **巡检**：管理员上报现场快照，系统比对台账产出
  `FIRE_LANE_BLOCKED / UNAUTHORIZED_OCCUPANCY / LEDGER_MISMATCH / OVERSTAY`，
  同一未决违规不重复开单；可下强制清退工单。
- **腾挪顺序**：上方遮挡货先移、违规货最后移，必须严格按步执行；
  规划时为每一步在候选空间内搜索合法落点。
- **紧急放行**：申请人发起，第二名管理员/安保批准（不得同人；租户无权批准）；
  绑定租户、空间、覆盖类型与期限；到期后未搬入预订撤销，已搬入开违规单转清退。
- **崩溃恢复**：对 moving 单由管理员判定 `resume / revert / manual`——
  - `resume`：begin/complete 幂等，重放不二次释放、不二次计费；
  - `revert`：仅限尚无任何步骤 begin（无法确认货物是否离地即拒自动撤销）；
  - `manual`：冻结自动执行，管理员现场核实后人工了结。
  决策本身落库留痕（`recovery.decided`）。
- **租户可见性**：租户只看自己货位细节，他人占用仅返回掩码（知道被占，不知是谁/什么）；
  管理员可追查全部细节与越权占位。

## 目录

- `domain/contract.json`：实体、状态机、事件类型（v2，按"新版本追加"策略扩展）。
- `examples/events.json`：按时间排列的事件样例。
- `examples/walkthrough.py`：完整业务故事线演示。
- `capacity/`：服务实现（`engine.py` 规则引擎、`service.py` 应用服务、
  `storage.py` SQLite 存储、`model.py` 值对象、`errors.py` 错误）。
- `tests/`：64 个单元/并发/持久化测试。
- `tools/validate_contract.py`：领域资料离线校验。

## 构建

```bash
python3 -m compileall -q .
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 资料校验与演示

```bash
python3 tools/validate_contract.py
python3 examples/walkthrough.py
```

所有命令均在项目根目录执行，不需要启动额外服务。

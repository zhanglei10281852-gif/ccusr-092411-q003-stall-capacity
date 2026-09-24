# 档口峰值容量治理

把市场安全边界与经营安排放进同一本**空间账**的容量治理服务。
固定档口、临时区域、货架层位统一记账；接货判断同时考虑货物尺寸重量、
可叠放性、相邻禁忌与预计离场时间；消防通道等安全边界以两人批准的
紧急放行许可限时开放，到期自动回收。

## 组成

- `capacity_governance/models.py`：空间、货物批次、搬运任务、紧急放行许可与状态机
  （planned → approved → moving → placed → released / expired）。
- `capacity_governance/storage.py`：SQLite 事件日志 + 状态投影；单连接 `IMMEDIATE`
  事务保证并发占位不穿透容量；`request_id` 幂等重放。
- `capacity_governance/service.py`：容量治理核心服务（见下）。
- `domain/contract.json`：领域合同（实体、状态、事件类型，v2.0）。
- `examples/patrol_scenario.py`：巡场场景端到端演示。
- `tools/validate_contract.py`：领域资料离线校验。

## 核心规则

**接货判断（`validate_intake` / `plan_move`）**
面积与承重按时间轴峰值核算（planned/approved 即冻结容量，超期未离场按仍在场
保守核算）；限高沿叠放链累计；叠放不重复占地面但仍计承重，且要求底座可承压、
类别允许、窗口覆盖；相邻空间的在场品类互相检查禁忌；在场窗口不得超出货物
预计离场时间；固定档口仅限归属商户，封闭区域拒绝接货。

**消防通道（安全边界）**
普通计划一律拒绝；须先申请紧急放行许可，由**两名不同的管理员**批准
（申请人不得自批），且许可期限必须覆盖整个在场窗口。到期由 `sweep` 回收：
未搬入的计划失效，仍在场的按强制清退据实结算并留痕。

**计划调整**
销售加快（提前离场）、车辆延误（推迟入场）、区域封闭（改投空间）只能调整
`planned`/`approved` 的**尚未搬入**计划；已 moving/placed 的任务拒绝调整。

**腾挪顺序（`build_evacuation_plan`）**
叠放上层先挪、临期货物加速离场、其余按离场时间改投同租户可用空间，
未搬入计划改投或取消；每步执行前按最新现场重新校验。

**异常重启恢复（`recover` / `review_inflight`）**
对 moving 任务逐一裁定：继续（重校容量与许可后落位）、撤销（释放目标冻结、
归还来源空间）、待人工确认（容量继续冻结）。计费以 `(task_id, kind)` 唯一约束
保证不重复；空间释放以状态机保证不重复。

**租户隔离**
租户只能看到自有货位详情，其他商户仅见聚合占用量；越权占位登记、强制清退、
腾挪计划仅管理员可用，全部写入审计表。

## 构建

```bash
python3 -m compileall -q .
```

## 测试

```bash
python3 -m unittest discover -s tests -v
```

## 资料校验

```bash
python3 tools/validate_contract.py
```

## 场景演示

```bash
python3 examples/patrol_scenario.py
```

所有命令均在项目根目录执行，不需要启动额外服务。

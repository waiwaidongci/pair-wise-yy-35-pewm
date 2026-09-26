# 职业辐射剂量与异常事件

合并监测读数，比较历史剂量并管理超限调查、医学随访与报告期限。

## 模块结构

- `app.py`：参数解析、依赖组装和HTTP服务启动。
- `src/domain.py`：数据结构、错误、状态和基础校验。
- `src/rules.py`：状态机、角色矩阵、优先级、期限和关闭不变量。
- `src/repository.py`：SQLite建表、事务、版本控制和审计链。
- `src/service.py`：权限检查、用例编排、并发控制和审计。
- `src/http_api.py`：JSON路由和统一错误响应。
- `src/audit.py`：UTC时间和SHA-256审计事件。
- `static/index.html`：最小演示页。
- `tests/`：完整流程、规则和失败测试。

## 初始化与启动

```bash
python3 app.py --db ./data.db --port 8312
```

默认端口为`8312`，首次启动自动建库。使用`X-Actor`和`X-Role`请求头传递身份。

## 主要接口

- `GET /health`
- `GET /api/items`
- `POST /api/items`
- `GET /api/items/{id}`
- `POST /api/items/{id}/records`
- `POST /api/items/{id}/transition`，必须提交`expected_version`
- `GET /api/items/{id}/holds`：查看事件的法务保全单
- `POST /api/items/{id}/holds`：开具保全单（radiation_officer/health_physicist），提交`case_no`、`reason`、`custodian`、`hold_from`、`hold_to`
- `POST /api/items/{id}/holds/{hold_id}/release`：解除保全
- `POST /api/cleanup`：例行清理（radiation_officer），返回已删除与跳过明细
- `GET /api/audit`

允许角色：dosimetrist, radiation_officer, health_physicist, viewer。剂量与调查水平之比决定升级程度，超过阈值必须进入调查；更正剂量不能覆盖已确认审计记录。

## 保留期与法务保全

- 结案（进入`closed`）时按严重程度生成保留截止日`retention_until`：low 1年、elevated 3年、high 7年、critical 30年。
- 保全单写明案号`case_no`、原因`reason`、保管人`custodian`和起止时间`hold_from`/`hold_to`；同一事件案号唯一，可解除。
- 例行清理只处理已结案且保留截止日已过的事件；存在**有效保全**（未解除且当前时间落在起止区间内）的事件被拦住，跳过原因记为`active_hold`。
- 每条删除在同一事务内重查版本、截止日和有效保全；扫描后任一状态变动即放弃删除并退回（`changed`），下轮按最新状态重算。旧库中无截止日的结案数据不会被清理。

## 测试

```bash
python3 -m unittest discover -s tests -v
```

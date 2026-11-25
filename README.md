# 标本同件索引交接 API

两馆合库后，向合作馆移交标本“同件索引”的交接服务。合作馆只接收
**能独立复核** 的配对：每个交接对必须同时给出两条同件证据链，两条链
除首尾登记号外 **登记号不共用、证据也不共用**。

- 框架：FastAPI（交互文档 `GET /docs`）
- 存储：SQLite（WAL 模式，数据落在 `/data` 卷）
- 部署：Docker Compose，一条命令起服务

## 业务规则

1. 证据表每条证据含 **证据号** 与判为同件的 **两个登记号**。
2. 馆员用已有的同件配对申请交接；系统在当前 **有效（未撤回）** 证据中
   查找两条独立证据链：
   - 两条链都从申请对的一端到另一端；
   - 除首尾两个登记号外，两条链不得共用登记号；
   - 两条链不得共用证据。
3. 两条链齐备 → 状态 `supplied`（供数中），响应与落库结论都附上两条链的
   登记号序列和证据号序列；凑不齐 → 状态 `pending`（待补）。
4. 撤回证据时，**只重核用到它的交接对**（通过 `handover_evidence` 索引
   定位）：
   - 还能凑齐两条有效链 → 更新依据，继续 `supplied`（即“换链”）；
   - 凑不齐 → 状态置为 `paused`（暂停供数），清空链依据并记录原因。
5. 撤回与重核在 **同一个数据库事务** 内一起生效；任一步失败整体回滚。
6. 查询接口 **直接读取已保存的结论**，不临时重算；写入失败时上次结论
   仍可正常查询。

## 算法

把登记号看作无向图顶点、证据看作带编号的无向边。用 **点拆点 + 容量
约束** 建网络：

- 每个登记号拆成入点/出点，中间登记号点容量为 1（两条链不能共用），
  首尾点容量为 2；
- 每条无向证据边在两个方向各建容量 1 的弧（证据不能共用，也不能在
  链内折返）；
- Edmonds-Karp 求最大流，流量达到 2 即存在两条合法链，两次增广路
  直接给出两条链，并再做一次防御性互斥校验。

## 启动

```bash
docker compose up --build
# 打开 http://localhost:8000/docs 试办交接
```

不使用 Docker 时的本地运行方式：

```bash
python -m venv .venv && . .venv/bin/activate
pip install -r requirements.txt
HANDOVER_DB=./handover.db uvicorn app.main:app --reload
```

## 接口一览

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| POST | `/evidence` | 录入证据（证据号唯一，重复返回 409） |
| GET | `/evidence` | 列出有效证据，`?include_withdrawn=true` 含已撤回 |
| GET | `/evidence/{id}` | 查询单条证据 |
| POST | `/evidence/{id}/withdraw` | 撤回证据，同事务只重核相关交接对，返回受影响对象及重核结论 |
| POST | `/handovers` | 凭同件配对申请交接，当场判定 `supplied`/`pending` 并落库 |
| GET | `/handovers` | 列出全部交接对及已保存结论（不重算） |
| GET | `/handovers/{id}` | 读取单个交接对已保存的结论 |
| POST | `/handovers/{id}/reverify` | 手工重核（补证后可让待补/暂停对象重新判定） |
| GET | `/health` | 健康检查 |

状态取值：`supplied`（供数中）、`pending`（待补）、`paused`（暂停供数）。

## 试办示例（/docs 可直接照填）

```bash
# 录入三条互不相交的 A->B 候选链
for body in \
  '{"evidence_id":"E1","reg_a":"A","reg_b":"M1"}' \
  '{"evidence_id":"E2","reg_a":"M1","reg_b":"N1"}' \
  '{"evidence_id":"E3","reg_a":"N1","reg_b":"B"}' \
  '{"evidence_id":"E4","reg_a":"A","reg_b":"M2"}' \
  '{"evidence_id":"E5","reg_a":"M2","reg_b":"N2"}' \
  '{"evidence_id":"E6","reg_a":"N2","reg_b":"B"}' \
  '{"evidence_id":"E7","reg_a":"A","reg_b":"M3"}' \
  '{"evidence_id":"E8","reg_a":"M3","reg_b":"N3"}' \
  '{"evidence_id":"E9","reg_a":"N3","reg_b":"B"}'; do
  curl -s -X POST localhost:8000/evidence -H 'Content-Type: application/json' -d "$body" >/dev/null
done

# 申请交接 -> supplied，返回两条链的登记号与证据号
curl -s -X POST localhost:8000/handovers \
  -H 'Content-Type: application/json' \
  -d '{"reg_a":"A","reg_b":"B"}'

# 撤回 E1 -> 自动换到 E7/E8/E9 那条链，继续供数
curl -s -X POST localhost:8000/evidence/E1/withdraw
```

## 测试

```bash
python -I tests/test_graph.py   # 算法单元测试（8 项）
python -I tests/test_api.py     # API 端到端测试（5 项，含事务回滚）
python -I tests/demo_http.py    # 真实 HTTP 试办：供数→换链→暂停→恢复
```

## 数据表

- `evidence`：证据（`withdrawn` 软撤回标记，行保留可追溯）。
- `handover`：交接申请与已保存结论（状态、两条链的登记号/证据号 JSON、
  原因、时间戳），`(reg_a, reg_b)` 唯一且规范化排序。
- `handover_evidence`：交接对当前依据所用证据索引，支撑“只重核用到它
  的交接对”的精准定位。

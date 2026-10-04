# 邮件会话重建 API（Mail Thread Reconstruction）

基于 **Python + FastAPI + PostgreSQL + 标准库 `email`** 的邮件会话（线程）重建服务。
接收原始 EML 批次，解析 `Message-ID` / `In-Reply-To` / `References`，按邮箱独立地
重建会话树；每次有效变更原子地形成一个不可变修订，可按修订读取历史时刻的会话树与缺件。

## 快速启动

```bash
docker compose up --build
```

一条命令启动两个容器：`db`（PostgreSQL 16）和 `api`（FastAPI/uvicorn，2 worker）。
`api` 容器限制为 **2 CPU / 1 GiB**（见 `docker-compose.yml` 的 `deploy.resources.limits`）。

## 访问地址

| 地址 | 说明 |
|---|---|
| `http://localhost:8000` | API 服务 |
| `http://localhost:8000/docs` | Swagger UI 交互文档 |
| `http://localhost:8000/health` | 健康检查 |
| `localhost:5432` | PostgreSQL（用户/密码 `mail`/`mail`，库 `mailmerge`） |

运行测试（可选，使用独立的 `mailmerge_test` 库）：

```bash
docker compose --profile test run --rm test
```

## API

### `POST /imports` — 导入一批原始 EML

请求体（JSON）：`mailbox` 为邮箱标识（各邮箱完全独立），`messages` 为原始 EML
字符串数组（JSON 字符串，`\r\n` 需转义，与下例一致）。

```json
{
  "mailbox": "demo@example.com",
  "messages": [
    "From: Carol <carol@example.com>\r\nTo: Bob <bob@example.com>\r\nSubject: Q3 plan\r\nMessage-ID: <c3@example.com>\r\nReferences: <a1@example.com> <b2@example.com>\r\n\r\nHere are my numbers.\r\n"
  ]
}
```

响应 `200`（真实返回）——父件 `b2@example.com` 缺失，先占位：

```json
{
  "mailbox": "demo@example.com",
  "revision": 1,
  "created": true,
  "imported": ["c3@example.com"],
  "duplicates": [],
  "merges": [],
  "missing": ["b2@example.com"]
}
```

字段说明：

| 字段 | 含义 |
|---|---|
| `revision` | 本批次形成的修订号（每邮箱从 1 开始连续递增，无空洞） |
| `created` | 是否真正产生了新修订（幂等重投时为 `false`） |
| `imported` | 本批新入库的 Message-ID |
| `duplicates` | 与已存邮件 ID 相同且字节相同的重复件 |
| `merges` | 本批引发的会话归并：旧会话编号 → 新会话编号 |
| `missing` | 导入后仍缺失的父件（占位符）列表 |

**迟到父件触发归并**（真实请求/响应）。导入 `b2@example.com`，它自身回复
`a1@example.com`（仍缺失）：

```bash
curl -X POST http://localhost:8000/imports -H 'Content-Type: application/json' -d '{
  "mailbox": "demo@example.com",
  "messages": [
    "From: Bob <bob@example.com>\r\nTo: Alice <alice@example.com>\r\nSubject: Re: Q3 plan\r\nMessage-ID: <b2@example.com>\r\nIn-Reply-To: <a1@example.com>\r\nReferences: <a1@example.com>\r\n\r\nThanks, forwarding to Carol.\r\n"
  ]
}'
```

```json
{
  "mailbox": "demo@example.com",
  "revision": 2,
  "created": true,
  "imported": ["b2@example.com"],
  "duplicates": [],
  "merges": [{"old_thread_id": "b2@example.com", "new_thread_id": "a1@example.com"}],
  "missing": ["a1@example.com"]
}
```

原本以占位符 `b2` 为根的会话被归并到以 `a1` 为根的新会话，`merges` 返回旧新会话编号映射。

**幂等重投**：同 ID 同原文字节重投不推进修订，返回原修订（该批全部邮件最早共同可见的修订）：

```json
{
  "mailbox": "demo@example.com",
  "revision": 3,
  "created": false,
  "imported": [],
  "duplicates": ["a1@example.com"],
  "merges": [],
  "missing": []
}
```

### `GET /threads` — 读取会话树

```
GET /threads?mailbox=demo@example.com            # 默认读最新修订（head）
GET /threads?mailbox=demo@example.com&revision=2 # 读指定历史修订
```

真实响应（`revision=2`，即根件 `a1` 到达之前；历史修订不随后续归并改写）：

```json
{
  "mailbox": "demo@example.com",
  "revision": 2,
  "head_revision": 3,
  "threads": [
    {
      "thread_id": "a1@example.com",
      "root": {
        "message_id": "a1@example.com",
        "present": false,
        "children": [
          {
            "message_id": "b2@example.com",
            "present": true,
            "children": [
              {"message_id": "c3@example.com", "present": true, "children": []}
            ]
          }
        ]
      }
    }
  ],
  "missing": ["a1@example.com"]
}
```

* `thread_id` = 会话根的 Message-ID（根为缺失父件时即占位符 ID）。
* 树节点 `present: false` 表示占位符（被引用为父件但尚未入库）。
* `missing` 为该修订时刻的全部缺件。
* 子节点按 Message-ID 排序，输出确定，与同批导入顺序无关。

### 错误响应

整批拒绝（原子回滚，**不推进修订**），HTTP 状态码及 `detail.error` 如下：

| 状态码 | `error` | 触发条件 | 真实响应示例 |
|---|---|---|---|
| 422 | `content_conflict` | 同 Message-ID 字节不同（批内或与已存冲突） | `{"detail":{"error":"content_conflict","message":"Message-ID already stored with different bytes","message_ids":["a1@example.com"]}}` |
| 422 | `missing_message_id` | 缺少 Message-ID 头 | `{"detail":{"error":"missing_message_id","message":"every message must carry a Message-ID header","indices":[0]}}` |
| 422 | `self_reference` | In-Reply-To/References 引用自身 | `{"detail":{"error":"self_reference","message":"a message must not reference its own Message-ID","message_ids":["s1@example.com"]}}` |
| 422 | `cycle_detected` | 应用本批将形成回复环（含迟到父件成环） | `{"detail":{"error":"cycle_detected","message":"applying the batch would create a reply cycle","cycle":["x9@example.com","y9@example.com","x9@example.com"]}}` |
| 422 | `empty_batch` | `messages` 为空 | `{"detail":{"error":"empty_batch","message":"messages must not be empty"}}` |
| 404 | `mailbox_not_found` | 邮箱不存在 | `{"detail":{"error":"mailbox_not_found","mailbox":"..."}}` |
| 404 | `revision_not_found` | 修订号超出范围 | `{"detail":{"error":"revision_not_found","mailbox":"...","revision":7,"head_revision":3}}` |

## 语义规则

1. **父件解析**：`In-Reply-To`（唯一回复标识）优先，取最后一个 ID；否则取
   `References` 的末个 ID；两者皆无则为根。ID 统一规范化为去尖括号形式
   （`<a@b>` → `a@b`），API 中所有 ID 均为该形式。
2. **占位与归并**：父件缺失时以占位符占位，占位符即其会话的根（会话编号 =
   占位符 ID）。迟到父件入库并连上已存祖件时，原会话整体归并到祖件所在会话，
   响应 `merges` 给出旧新会话编号映射。
3. **修订**：每次产生有效变更的导入在一个事务内完成（消息 + 修订行 + 邮箱
   水位），形成连续无空洞的修订号；拒绝的批次整体回滚、不推进修订。
4. **历史不可变**：消息行只增不改，读取修订 R 即过滤 `revision <= R` 重放，
   归并永远不会改写历史修订的视图。
5. **幂等**：同 ID 同字节重投为无操作，返回原修订；同 ID 异字节拒绝。
6. **顺序无关**：整批先解析为集合再统一落库，同批换序不改变最终会话树。
7. **邮箱隔离**：不同邮箱的 ID 空间、修订序列完全独立。

## 性能

`scripts/bench.py` 先经 API 灌入 10 万封邮件，再测量单封（≤8 KiB）补件延迟：

```bash
python scripts/bench.py --base-url http://localhost:8000 --total 100000
```

在 2 核（taskset 绑定）环境下实测：单封导入中位 **~75 ms**、最大 **~86 ms**
（预算 2 s）；10 万封全量 `GET /threads` 约 1.1 s；两个 uvicorn worker
常驻内存合计 **~220 MiB**（预算 1 GiB）。

## 项目结构

```
app/
  main.py        # FastAPI 路由与请求/响应模型
  service.py     # 导入事务、校验、归并计算、按修订读取
  threads.py     # 纯函数：求根/成环检测、会话森林构建
  emailparse.py  # 标准库 email 解析 Message-ID/In-Reply-To/References
  db.py          # 连接池与幂等建表
tests/test_api.py  # 19 个端到端测试（真实 PostgreSQL）
scripts/bench.py   # 10 万封性能验证
docker-compose.yml # 一键启动（db + api，另含 test profile）
```

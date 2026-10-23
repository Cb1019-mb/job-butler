# 架构

本文描述求职管家的当前实现：模块边界、数据流、记忆生命周期、数据模型与运行约束。安装与使用见 [README](../README.md#快速开始)。

## 总览

```
浏览器（Jinja2 + HTMX，SSE）
        │ HTTP
        ▼
FastAPI 单进程（app/main.py）
├── routers/         页面与 JSON API
├── modules/         档案 / 面试 / 记忆 / 看板 / 周报 / 设置
├── llm.py           OpenAI 兼容客户端、思考模式、token 记录
├── llm_tasks.py     档案抽取 / 点评 / 出题 / 综合点评
├── embed.py         Embedding 客户端与短路保护
├── scheduler.py     APScheduler
├── mcp_server.py    stdio MCP 三工具
└── routers/mcp_http.py  MCP 工具的 HTTP 包装

        │                          │
        ▼                          ▼
SQLite（WAL、FK、单写者锁）    OpenAI 兼容 LLM / Embedding 端点
```

| 层 | 实现 | 说明 |
|---|---|---|
| Web | FastAPI + Jinja2 + HTMX | 服务端渲染；点评走 SSE；双主题、字号与密度仅存 `localStorage` |
| 存储 | SQLite + WAL | `busy_timeout=5000`、外键开启；写操作经进程内单写者锁串行化 |
| 记忆向量 | `memory.embedding` BLOB | 合并在 Python 内计算余弦相似度 |
| LLM | OpenAI 兼容协议 | 端点、模型、Key、温度、超时均由 `settings` 决定 |
| Embedding | OpenAI 兼容协议 | 默认 `bge-small-zh-v1.5`（512 维） |
| 调度 | APScheduler | `Asia/Shanghai`，每周日 23:59 生成周报 |
| MCP | FastMCP / MCPServer 三工具 | stdio 入口为 `run_stdio()`（模块没有 `__main__` 入口）；另有三个 HTTP GET 端点 |

## 代码结构

```
app/
├── main.py             # lifespan：init_db → embedding warmup → scheduler，挂载 8 个 router
├── config.py           # 路径、记忆阈值、出题配比、厂商预设
├── db.py               # 连接、9 张表、FTS5 触发器、settings kv、单写者事务
├── llm.py              # OpenAI 客户端、思考模式、chat kwargs、token 记录
├── llm_tasks.py        # 档案抽取、三轴点评（同步/流式）、出题、综合点评
├── json_utils.py       # JSON 提取、键名归一化、Schema 校验、重试与安全调用
├── embed.py            # Embedding 请求、归一化、60s 短路
├── scheduler.py        # 周日 23:59 周报任务
├── mcp_server.py       # stdio MCP 三工具
├── routers/            # pages / profile / interview / memory / kanban / report / settings / mcp_http
├── modules/            # profile / question_bank / interview / memory_engine / kanban / report / settings
└── templates/          # 7 个 Jinja2 模板（6 个主页面 + 会话详情）
data/
├── seed_questions.json # 50 条兜底题库
├── regression_30.json  # 30 条抽取回归集
├── ab_report.md/.json  # A/B 实验报告
└── demo_script.md      # 3 分钟演示脚本
tests/                  # 4 个验收脚本 + A/B 脚本 + conftest
```

## 请求流

**启动**：`init_db()` 建表、补列、创建 FTS5 表与触发器；随后 `embed.warmup()` 预热，失败只告警；最后启动 APScheduler。

**能力档案**：`POST /api/profile/parse`（页面表单）→ `extract_profile()` 按 JSON Schema 抽取 `skills` / `projects` / `weak_signals` / `jd_focus` → 新插入一条 `profile` 记录。另有 `POST /api/profile/upload` 接收 zip 并解压合并文本，当前页面未提供入口。抽取失败时保留原文并在 `parsed_skills_json._parse_error` 记录原因，页面展示错误而不是空档案。

**开始面试**：`POST /api/interview/start` → 创建 `interview_session` → `build_questions()` 用 3 个线程并行生成 WEAKNESS / RESUME / NEW 三槽题目（每槽配额来自 settings，默认 2/2/1），跨槽去重，带近 30 天避重清单；LLM 失败时回退到 `seed_questions.json`。题目写入 `session_question`，重启不丢。

**作答**：`POST /api/interview/answer` → 先做跑题/空话拦截，命中时直接生成 0 分点评并落库；否则调用流式点评，按 SSE 推送 `chunk`，落库后推送 `done`（携带归一化后的评分）。生成器异常时推送 `error` 事件。`done` 之后在后台线程执行记忆抽取，串行锁 + 幂等保护避免重复入库。

**综合点评**：`POST /api/interview/{id}/summary` → 先非阻塞地补一次幂等记忆抽取，再聚合全部题目的三轴评分，调用 LLM 生成复盘 Markdown，写入 `interview_session.summary_md`。

**周报**：APScheduler 或 `POST /api/report/generate` → 汇总本周作答数、均分、新记忆、掌握/失效数量与 Top 5 活跃弱点，用模板渲染 Markdown 并写入 `weekly_report`；不调用 LLM。

**MCP**：`memory_search` 对 `memory.content` 做关键词过滤并按有效强度排序；`memory_brief` 返回状态计数与 Top N；`profile_get` 返回最近 N 条档案概要。stdio 与 HTTP 共用 `app/mcp_server.py` 中的实现。

## 记忆生命周期

```
单题作答 + 三轴点评
        │
        ▼
抽取（规则 + LLM）→ 去重 → 质量门
        │
        ▼
WEAKNESS 类型：与 ACTIVE / IDLE 记忆算余弦
        ├── 相似度 > 阈值（默认 0.85）→ 旧条 strength +1、reinforce_count +1
        └── 否则新建 ACTIVE
        │
        ▼
ACTIVE ── 30 天无强化 或 effective < 0.1 ──► IDLE
  │ ▲                                          │
  │ └──────────── 手动恢复 ────────────────────┘
  │
  ├── 手动标记已掌握 ──► MASTERED
  └── 手动标记无效 ────► DISABLED（不再参与出题）

单场超过 max_per_session（默认 6）：按 effective 排序保留前 6 条，其余置 IDLE。
```

| 环节 | 实现 |
|---|---|
| 抽取 | 规则：从 `completeness.missed_points` 与过短作答中提取；LLM：提示词要求输出弱点 JSON，解析时兼容数组、`{weaknesses: [...]}` 与单条对象。两路结果去重后逐条过质量门 |
| 质量门 | 必须含具体知识点（技术名词）或强信号短语；空话（“表达不够自信”“应该多练”）直接拒绝；内容少于 3 字拒绝 |
| 合并 | 仅 WEAKNESS 参与；候选为 ACTIVE / IDLE；余弦相似度严格大于 `memory.merge_threshold` 时强化最相似的一条，不新建 |
| 衰减 | `effective = strength × exp(-ln2 × days / half_life_days)`；默认半衰期：弱点 14 天、事实 60 天、偏好 30 天 |
| 闲置 | 每次记忆抽取后刷新：`effective < 0.1` 或距上次强化达到 `memory.idle_days`（默认 30 天）时，ACTIVE → IDLE |
| 场次上限 | 按本场 `qa_log.id` 统计 ACTIVE 记忆，超过 `memory.max_per_session` 时保留 effective 最高的若干条，其余置 IDLE |
| 出题检索 | 只取 ACTIVE 记忆，按 effective 降序；MASTERED 与 DISABLED 不进入 WEAKNESS 槽 |
| 幂等 | `qa_log.extracted_at` 非空即跳过；未抽出弱点的题目也会标记，避免反复重试 |
| 开关 | `memory.enabled=false` 时 WEAKNESS 槽直接取题库，不检索记忆也不调用 LLM；历史记忆仍可浏览与手动操作 |

当前自动抽取只写入 `WEAKNESS`；`FACT` / `PREF` 的类型与半衰期配置存在于模型层，但没有生产写入路径。

## 数据模型

SQLite 共 9 张业务表：

```sql
profile(id, name, resume_text, parsed_skills_json, target_jd, created_at)
interview_session(id, role_tag, company, mode, profile_id, summary_md, created_at)
qa_log(id, session_id, question, question_source, answer, score_json,
       reference_answer, extracted_at, created_at)
session_question(id, session_id, qa_index, question, question_source,
                 category, reference_answer, scoring_points_json)
memory(id, type, content, source_qa_id, embedding, half_life_days,
       strength, reinforce_count, status, created_at, last_reinforced_at)
application(id, company, role, stage, stage_updated_at, notes)
weekly_report(id, week_start, content_md, memory_cited, created_at)
settings(key, value, updated_at)
token_usage(id, purpose, model, prompt_tokens, completion_tokens, created_at)
```

- `interview_session.profile_id` 为 `ON DELETE SET NULL`；`qa_log` 与 `session_question` 随会话 `ON DELETE CASCADE`；`memory.source_qa_id` 为 `ON DELETE SET NULL`。删除会话会级联清理题目与作答。
- 看板阶段固定为 `投递 / 笔试 / 面试 / offer / 挂`，非法阶段会被拒绝。
- `session_question` 以 `(session_id, qa_index)` 唯一约束保存题目，SSE 作答从该表恢复题目。
- `init_db()` 内置轻量迁移，为旧库补 `interview_session.profile_id`、`summary_md` 与 `profile.name` 三列。
- `qa_log` 另有 FTS5 trigram 虚拟表，由插入/删除触发器同步；当前业务查询未使用该索引。
- SQLite 连接会加载 `sqlite-vec` 扩展，但未创建向量虚拟表；记忆相似度在 Python 中计算。

## 配置与模型接入

所有默认值集中在 `app/modules/settings.py` 的 `DEFAULTS`，其中模型相关项派生自 `app/config.py` 并支持环境变量覆盖；`settings` 表中的值优先。

| Key | 默认值 | 说明 |
|---|---|---|
| `llm.provider` | `vllm` | 仅用于选择预设与思考模式默认值 |
| `llm.base_url` | `http://localhost:8000/v1` | OpenAI 兼容端点 |
| `llm.api_key` | `EMPTY` | 云厂商填真实 Key；页面回显掩码 |
| `llm.model` | `qwen3-4b-awq` | |
| `llm.temperature` / `llm.timeout` | `0` / `30` 秒 | |
| `llm.disable_thinking` | 空 | 空=按厂商预设；`true` / `false` 强制 |
| `embed.base_url` | `http://localhost:8001/v1` | OpenAI 兼容 embeddings 端点 |
| `embed.api_key` | `EMPTY` | |
| `embed.model` / `embed.dim` | `bge-small-zh-v1.5` / `512` | |
| `interview.questions_per_session` | `5` | 每场题数 |
| `interview.weakness_ratio` / `resume_ratio` / `new_ratio` | `2` / `2` / `1` | 三槽配额 |
| `interview.dedup_days` | `30` | 出题避重窗口 |
| `memory.enabled` | `true` | 弱点出题开关 |
| `memory.merge_threshold` | `0.85` | 相似合并阈值 |
| `memory.max_per_session` | `6` | 单场记忆上限 |
| `memory.half_life_weakness` / `fact` / `pref` | `14` / `60` / `30` 天 | 半衰期 |
| `memory.idle_days` | `30` | 转入 IDLE 的闲置天数 |

`embed.dim` 当前仅保存与展示，代码不按该值校验返回向量维度；实际维度由 Embedding 端点与所用模型决定。

**厂商预设**（`config.PROVIDER_PRESETS`，下拉选择后自动填充 base_url 与 model）：

| provider | base_url | 默认 model | 默认关闭思考 |
|---|---|---|---|
| `vllm` | `http://localhost:8000/v1` | `qwen3-4b-awq` | 是 |
| `deepseek` | `https://api.deepseek.com/v1` | `deepseek-chat` | 否 |
| `openai` | `https://api.openai.com/v1` | `gpt-4o-mini` | 否 |
| `moonshot` | `https://api.moonshot.cn/v1` | `kimi-k2-0905-preview` | 否 |
| `zhipu` | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.5-air` | 否 |
| `dashscope` | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | 是 |
| `custom` | 手动填写 | 手动填写 | 否 |

思考模式通过 `chat_template_kwargs.enable_thinking=false` 关闭，仅对识别该字段的模型发送；用户显式设置优先于厂商预设。

## 运行约束与降级

- **无鉴权**：应用只应绑定 `127.0.0.1`。暴露到局域网等于公开简历、档案与设置。
- **SQLite 并发**：WAL + `busy_timeout=5000` + 进程内写锁；多进程部署会绕过写锁，不支持。
- **时间**：数据库统一存 UTC（`datetime('now')`）；周报查询把本地周边界转成 UTC，token 日统计按 `localtime` 分组，调度时区为 `Asia/Shanghai`。
- **Embedding 短路**：连接失败后 60 秒内直接抛 `EmbeddingUnavailable`，连接超时 1.5 秒，避免阻塞业务；合并失败时降级为“不合并、直接入库”。
- **LLM 客户端**：按 provider / base_url / api_key / timeout 缓存；设置保存后立即失效，下次请求重建。客户端设置 `trust_env=False`，避免系统代理劫持 localhost。
- **出题降级**：LLM 出题失败按槽位回退题库；题目数量不足时继续从题库补齐。
- **点评降级**：明显跑题或空话的作答不调用 LLM，直接返回带采分点的 0 分点评；SSE 生成失败时推送 `error` 事件，由前端展示原因。
- **记忆抽取降级**：LLM 不可用时使用规则抽取；后台线程异常只记录日志，不影响已完成的点评。
- **token 统计**：记录失败不影响主流程。
- **API Key**：读取设置时做掩码；保存时 `****` 开头的值视为未修改并跳过。
- **出题避重**：同时覆盖 `qa_log`（已作答）与 `session_question`（已出未答），窗口由 `interview.dedup_days` 控制。
- **调度**：周报任务运行在应用进程内，进程停止期间不会触发；错过的时间点不会补跑。

## 测试与实验

`test_w1`~`test_w4` 在导入业务模块前设置独立的 `JOB_BUTLER_DB`。`ab_report.py` 在 `main()` 中才调用 `os.environ.setdefault`，此时 `app.config` 已导入，默认仍指向 `data/job_butler.db`；它还会清空目标库的 `memory`、`qa_log` 与 `interview_session`。运行 A/B 前必须先设置 `JOB_BUTLER_DB` 指向临时库，或备份主库。

| 脚本 | 覆盖范围 |
|---|---|
| `tests/test_w1.py` | 衰减边界、出题配比与避重、点评 Schema / 容错 / 防照抄、settings、档案历史 |
| `tests/test_w2.py` | 质量门、规则抽取、相似合并、状态机、场次上限、30 条回归、出题接记忆、全链路沉淀 |
| `tests/test_w3.py` | `memory.enabled` 开关、弱点出题、sid 偏移、半衰期配置键一致性 |
| `tests/test_w4.py` | 看板 CRUD 与阶段校验、周报渲染/落库、MCP 三工具、APScheduler 启停 |
| `tests/ab_report.py` | 记忆开关 A/B 实验，输出 `data/ab_report.md` 与 `data/ab_report.json`；会清空目标库的记忆、作答与会话 |

`test_w2.py` 加 `--skip-slow` 可跳过依赖真实 LLM 的用例。A/B 实验每组 20 场、每场 2 道弱点题，结果见 [data/ab_report.md](../data/ab_report.md)。

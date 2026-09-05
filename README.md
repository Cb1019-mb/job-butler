# 求职管家 · Job Butler

> 会长期记住你的 AI 求职伙伴——记你答砸的题、按遗忘曲线淡出、每周生成成长报告。

[![Python](https://img.shields.io/badge/python-3.10+-blue)]() [![FastAPI](https://img.shields.io/badge/FastAPI-0.110+-green)]() [![SQLite](https://img.shields.io/badge/SQLite-WAL-lightgrey)]() [![MCP](https://img.shields.io/badge/MCP-3_tools-orange)]() [![License](https://img.shields.io/badge/license-MIT-blue)]()

> 差异化内核:**认知记忆引擎**(抽取 / 合并 / 遗忘衰减 / 状态机),装进一个你自己每天会用、有真实刚需的应用。

---

## 目录

- [1. 端到端 60 秒体验](#1-端到端-60-秒体验)
- [界面预览](#界面预览)
- [2. 系统架构](#2-系统架构)
- [3. 记忆生命周期](#3-记忆生命周期)
- [4. 数据模型](#4-数据模型sqlite-9-张表)
- [5. Sprint 路线图](#5-sprint-路线图w1w5-全部完成)
- [6. 跑通指南](#6-跑通指南)
- [7. 6 页面功能总览](#7-6-页面功能总览)
- [8. LLM 接入与端点对齐](#8-llm-接入与端点对齐)
- [9. 演示视频脚本](#9-演示视频脚本-3-分钟)
- [10. 简历弹药](#10-简历弹药实测)
- [11. 测试与 A/B 实验](#11-测试与-ab-实验)
- [12. 关键避坑](#12-关键避坑已内化到代码)
- [13. 文件结构](#13-文件结构)
- [14. License](#14-license)

---

## 1. 端到端 60 秒体验

```bash
# 1. 准备 LLM + Embedding(OpenAI 兼容端点,二选一):
#    a) 本地 vLLM: qwen3-4b-awq :8000 + bge-small-zh-v1.5 :8001
#    b) 云厂商 API(DeepSeek/OpenAI/Kimi/智谱/通义):启动后在 /settings 选预设并填 API Key
#    详细前置见 §6.1

# 2. 装依赖 + 启动(仅本机使用,勿绑 0.0.0.0;端口/端点可用环境变量覆盖,见 .env.example)
cd job_butler
uv sync
uv run uvicorn app.main:app --host 127.0.0.1 --port 8803

# 3. 浏览器打开 http://localhost:8803
#    /profile   → 粘贴简历 + JD + 档案名称,点"生成能力档案"(自动保存为档案历史)
#    /interview → 选一份档案 → "开始一场新面试"(AI 现场出题,约 15~60s) → 答 5 题
#                 答完点"提交全部并生成综合点评"
#    /timeline  → 看自动抽取的弱点记忆
#    /kanban    → 看投递看板 + 一键生成周报(历史周报可删除)
#    /settings  → 切主题(暖白/纯黑)/选厂商/填 API Key/查 tokens 统计
```

---

## 界面预览

| **能力档案** —— 粘贴简历 + JD,AI 抽取 4 维结构化档案 | **模拟面试 · 三轴点评** —— 逐题流式点评,分数可解释 |
|---|---|
| ![能力档案](docs/screenshots/01-profile.png) | ![三轴点评](docs/screenshots/02-interview-judge.png) |

| **记忆时间线** —— 弱点记忆自动沉淀,状态机可视 | **看板 + 周报** —— 投递流转 + 每周成长报告 |
|---|---|
| ![记忆时间线](docs/screenshots/03-timeline.png) | ![看板+周报](docs/screenshots/04-kanban.png) |

| **Tokens 使用统计** —— 按用途累计的 LLM 调用面板 | |
|---|---|
| ![Tokens 统计](docs/screenshots/05-settings-tokens.png) | |

---

## 2. 系统架构

```
                  ┌── 浏览器(Jinja2 + HTMX, 6 页)──────────────────┐
                  │  档案 / 模拟面试 / 记忆时间线 / 看板+周报 / 设置 │
                  └─────────────────┬─────────────────────────────┘
                                       │ HTTP + SSE
                          ┌────────────▼─────────────────────┐
                          │       FastAPI 单服务(8803)        │
                          │  interview · memory-engine        │
                          │  kanban     · report              │
                          │  profile    · mcp (3 tools)       │
                          └──┬────────────────┬──────────────┘
                             │                │
                  ┌──────────▼──────┐   ┌─────▼─────────────┐
                  │ SQLite(WAL)     │   │ LLM(OpenAI 兼容)   │
                  │ FTS5+sqlite-vec │   │ 本地 vLLM 或云厂商  │
                  │ 9 张表全业务数据 │   │ 短路过 60s 保护     │
                  └─────────────────┘   └───────────────────┘
```

| 层 | 选型 | 备注 |
|---|---|---|
| Web | FastAPI + Jinja2 + HTMX | 前端总工时 ≤ 2 天,SSR 足够,暖白/纯黑双主题 |
| LLM | OpenAI 兼容协议统一接入 | 本地 vLLM `qwen3-4b-awq` :8000(温度 0,思考模式可关) |
| Embedding | vLLM `bge-small-zh-v1.5` :8001 | 512 维;查询侧加 BGE 检索指令前缀 |
| 检索 | FTS5 trigram + sqlite-vec | trigram 解决中文不分词 |
| 调度 | APScheduler | 周日 23:59 自动跑周报 |
| MCP | MCP Server(mcp 1.x/2.x 兼容)3 工具 | `memory.search` / `memory.brief` / `profile.get`,供 Cursor/Claude Desktop 集成 |

---

## 3. 记忆生命周期

```
              ┌─ 复盘抽取(规则 + LLM)─ 质量门 ─ 合并(相似度 >0.85)─┐
              │                                                        │
              ▼                                                        ▼
   产生(每场 3~6 条) ─ 强化(同类弱点再次答错 strength+1) ───  ACTIVE
              │                                                        ▲
              │ 答对 2 次连续                                            │
              │                                                        │
              ▼                                                        │
          MASTERED ◄──────────── 30 天无强化 ─────────────►   IDLE
      (出题权重骤降)                                  (降权,不删除)
              │
              │ 用户标记无效
              ▼
          DISABLED  (永久排除,负反馈)
```

| 环节 | 实现 |
|---|---|
| 抽取 | 规则(从 `completeness.missed_points` + 答案长度) + LLM(JSON Schema 强约束) |
| 质量门 | 必须含具体知识点(关键词:Redis / MySQL / Kafka / Spring / JVM / Agent ...)+ 可验证;空话("表达不够自信")直接拒 |
| 合并 | 与已有 ACTIVE / IDLE 记忆算 embedding 余弦 > 0.85 → 强化旧条,不入新条 |
| 衰减 | `effective = strength × exp(-ln2 × days / half_life)`,按 type 配默认半衰期(弱点 14 天 / 事实 60 天 / 偏好 30 天) |
| 场次上限 | `max_per_session` 默认 6,超出后按 effective 排序裁剪到 IDLE |
| 状态机 | ACTIVE → MASTERED(时间线页手动标记)/ IDLE(30 天无强化或衰减至阈值以下,自动)/ DISABLED(用户标记) |
| 幂等 | qa_log.`extracted_at` 字段,已抽过的 qa 跳过,防止重复入库 |

**真实跑通样例**(1 场 5 题,LLM 真实点评):

| 题 | 三轴(t/c/s) | 抽到记忆 |
|---|---|---|
| 项目亮点(空答) | 1/1/1 | "STAR 结构 / 数据/事实支撑" |
| 项目指标(不确定) | 1/0/0 | "结果量化" |
| 分歧(坚持) | 2/1/1 | "妥协与坚持" |
| 失败(STAR) | 2/1/1 | (无) |
| Redis(RDB 答) | 0/0/0 | "对 Redis 回答过短" |

---

## 4. 数据模型(SQLite,9 张表)

```sql
profile(id, name, resume_text, parsed_skills_json, target_jd, created_at)  -- name=档案历史命名
interview_session(id, role_tag, company, mode,
                  profile_id REFERENCES profile,          -- 会话绑定的档案
                  summary_md,                             -- 整场综合点评(Markdown)
                  created_at)
qa_log(id, session_id, question, question_source,           -- WEAKNESS/RESUME/NEW
       answer, score_json, reference_answer,
       extracted_at,                                       -- W2 幂等保护
       created_at)
memory(id, type, content, source_qa_id, embedding BLOB,    -- WEAKNESS/FACT/PREF
       half_life_days, strength, reinforce_count, status,   -- ACTIVE/MASTERED/IDLE/DISABLED
       created_at, last_reinforced_at)
application(id, company, role, stage, stage_updated_at, notes)    -- W4 看板
weekly_report(id, week_start, content_md, memory_cited, created_at) -- W4 周报
settings(key TEXT PK, value TEXT, updated_at)                -- W3+ 全部配置
session_question(id, session_id, qa_index, question,         -- W4 题目持久化
                question_source, category, reference_answer,
                scoring_points_json)
token_usage(id, purpose, model, prompt_tokens,               -- LLM tokens 使用统计
            completion_tokens, created_at)
```

`session_question` 解决了早期 `_session_store` 全局 dict 重启即丢的问题;
旧库由 `init_db()` 内置的轻量迁移(`_migrate`)自动补列,无需手动改表。

---

## 5. Sprint 路线图(W1~W5 全部完成)

| 周 | 目标 | 验收 | 状态 |
|---|---|---|---|
| **W1** | 工程 + 题库 + 出题 → 作答 → 三轴点评 + 档案页 | 跑一场 5 题面试,QA 落库,点评引用参考答案 | ✅ |
| **W2** | 复盘抽取 + 时间线页 + 手动标记 + 幂等保护 | 面试后自动沉淀 3~6 条记忆,QA 自动抽 / 已抽过的不重复 | ✅ |
| **W3** | 出题器接记忆 + 衰减 + A/B 开关 | 量化对比——**弱点命中率 0% → 100%**(7 领域全覆盖) | ✅ |
| **W4** | 看板 + 周报 + MCP + 主题 + 演示脚本 | 第一份真实周报生成;3 分钟演示脚本;Cursor/Claude 可调 | ✅ |
| **W5** | 增强:档案历史 + 按档案出题 + 综合点评 + tokens 统计 + 多厂商接入 | 档案历史/删除、出题不重复、一键综合点评、tokens 面板、7 厂商预设 | ✅ |

W5 关键变更:
- **档案历史**:每次生成保存为一条命名记录,可查看/删除;模拟面试可选定档案出题
- **出题 LLM 化**:弱点/简历/新知三槽位并行现场出题(带近 30 天避重清单),题库仅作兜底,每场题目不重复
- **综合点评**:`POST /api/interview/{id}/summary` 聚合三轴均分 + LLM 复盘(整体表现/做得好/待改进/下场建议),持久化到会话
- **SSE 优化**:点评落库后 `done` 立即返回,记忆抽取移入后台线程(幂等+串行锁)
- **多厂商接入**:OpenAI 兼容协议统一,vLLM/DeepSeek/OpenAI/Kimi/智谱/通义/自定义预设一键填充
- **tokens 统计**:`token_usage` 表按用途(purpose)累计,设置页面板展示
- **安全**:API Key 掩码回显、思考模式独立开关、时区统一(报告/tokens 按本地时区)

---

## 6. 跑通指南

### 6.1 前置
- Python 3.10+
- uv(`pip install uv`)
- LLM(任选其一,均走 OpenAI 兼容协议):
  - 本地 vLLM 8000 跑 `qwen3-4b-awq`(无鉴权)
  - 或云厂商 API(DeepSeek/OpenAI/Kimi/智谱/通义),在设置页选预设并填 API Key
- Embedding:vLLM 8001 跑 `bge-small-zh-v1.5`(固定 512 维;用云 Embedding 时在设置页改 base_url/key)

### 6.2 安装
```bash
cd job_butler
uv sync
```

### 6.3 启动
```bash
# 仅本机使用,绑定 127.0.0.1(勿绑 0.0.0.0:应用无鉴权,暴露局域网=简历/设置可被任意访问)
uv run uvicorn app.main:app --host 127.0.0.1 --port 8803
```

启动日志确认:
```
INFO httpx: HTTP Request: POST http://localhost:8001/v1/embeddings "HTTP/1.0 200 OK"
INFO app.embed: embedding warmup ok, shape=(1, 512)
INFO app.scheduler: APScheduler 启动:周报每周日 23:59 生成
INFO Uvicorn running on http://127.0.0.1:8803
```

环境变量(仅影响**首次运行**的默认值;settings 表已持久化的值优先,改 env 不生效时在设置页修改或 `POST /api/settings/reset`):

| 变量 | 说明 | 默认 |
|---|---|---|
| `JOB_BUTLER_DB` | SQLite 路径 | `data/job_butler.db` |
| `VLLM_BASE_URL` / `VLLM_MODEL` / `VLLM_API_KEY` | LLM 默认端点/模型/Key | `localhost:8000/v1` / `qwen3-4b-awq` / `EMPTY` |
| `EMBED_BASE_URL` / `EMBED_API_KEY` | Embedding 端点/Key | `localhost:8001/v1` / `EMPTY` |
| `LLM_PROVIDER` | 厂商预设(vllm/deepseek/openai/moonshot/zhipu/dashscope/custom) | `vllm` |

### 6.4 验收脚本
```bash
uv run python tests/test_w1.py     # 出题 / 衰减 / 点评 Schema
uv run python tests/test_w2.py     # 抽取 / 合并 / 状态机 / 30 条回归
uv run python tests/test_w3.py     # 开关 / sid 偏移
uv run python tests/test_w4.py     # 看板 / 周报 / MCP
uv run python tests/ab_report.py   # A/B 实验 → data/ab_report.md
```

### 6.5 MCP 集成(Cursor / Claude Desktop)
```json
// ~/.cursor/mcp.json 或 Claude Desktop 配置
{
  "mcpServers": {
    "job-butler": {
      "command": "uv",
      "args": ["--directory", "/path/to/job_butler", "run", "python", "-m", "app.mcp_server"],
      "env": { "JOB_BUTLER_DB": "/path/to/job_butler/data/job_butler.db" }
    }
  }
}
```

启动后,你的 IDE Agent 可以调三个工具:
- `memory_search(query, k=5, status="ACTIVE")` — 查弱点
- `memory_brief(top_n=5)` — 记忆简报
- `profile_get(limit=5)` — 能力档案概要

---

## 7. 6 页面功能总览

| 路径 | 功能 |
|---|---|
| `/` | 首页:Hero + 使用流程 + 差异化 + 配置入口;顶栏"← 返回"全局可用 |
| `/profile` | 能力档案:粘贴简历 + JD + 命名 → 生成(保存为档案历史);按 4 维度展示;历史列表可查看/删除(`/profile/{id}` 详情) |
| `/interview` | 模拟面试:选档案发起新会话 → 三槽位并行 LLM 出题(带避重) → 逐题流式三轴点评 → "提交全部并生成综合点评";历史会话可删除 |
| `/interview/{id}` | 会话历史:整场综合点评 + 全 qa 列表(题目/作答/参考答案/三轴点评) |
| `/timeline` | 记忆时间线:4 状态计数 + 卡片流(状态 tag + 强度条 + 标记无效/已掌握/恢复 ACTIVE 按钮) |
| `/kanban` | 看板 + 周报:5 列(投递/笔试/面试/offer/挂)CRUD + 周日 APScheduler 自动跑 + 手动生成 + 周报历史删除 |
| `/settings` | 设置:画面(2 主题/2 字号/2 密度)+ 模型(7 厂商预设/思考模式/API Key 掩码)+ 出题 + 记忆 + **Tokens 使用统计** |

---

## 8. LLM 接入与端点对齐

所有 LLM 调用统一走 **OpenAI 兼容协议**(`get_client` 直接读 settings 的 base_url/api_key/model),厂商预设仅用于前端一键填充。设置页 DEFAULTS:

| Key | 值 |
|---|---|
| `llm.base_url` | `http://localhost:8000/v1` |
| `llm.api_key` | `EMPTY`(云厂商填真实 Key;回显掩码 `****xxxx`,不改则保留原值) |
| `llm.model` | `qwen3-4b-awq` |
| `llm.temperature` / `llm.timeout` | `0` / `30` |
| `llm.disable_thinking` | 空=按厂商预设自动;true/false 强制 |
| `embed.base_url` | `http://localhost:8001/v1` |
| `embed.model` / `embed.dim` | `bge-small-zh-v1.5` / `512` |

**厂商预设**(`config.py PROVIDER_PRESETS`,下拉选择自动填充 base_url+model):

| provider | base_url | 默认 model | 思考默认 |
|---|---|---|---|
| vllm | `http://localhost:8000/v1` | `qwen3-4b-awq` | 关闭 |
| deepseek | `https://api.deepseek.com/v1` | `deepseek-chat` | 保留 |
| openai | `https://api.openai.com/v1` | `gpt-4o-mini` | 保留 |
| moonshot | `https://api.moonshot.cn/v1` | `kimi-k2-0905-preview` | 保留 |
| zhipu | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.5-air` | 保留 |
| dashscope | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen-plus` | 关闭 |
| custom | 手填 | 手填 | 保留(可手动关闭) |

**思考模式**:`enable_thinking=false` 仅对自建 Qwen3 类模型必要;云 API 不识别该字段,故按"预设 + 用户开关"决定是否携带(设置页可强制)。

**降级保护**:
- `embed.py` 启动时 warmup 失败,后续请求**短路 60s**(避免单次 30s 超时阻塞业务)
- LLM 客户端缓存签名,改 settings 后下次请求自动重建(无重启)
- vLLM 不可达时:出题走题库兜底,记忆抽取走规则兜底,三轴点评走"全部 0 分"保底
- SSE 点评异常时显式推送 `error` 事件,前端展示失败原因(不再静默断流)

---

## 9. 演示视频脚本(3 分钟)

完整脚本在 `data/demo_script.md`,含 8 个截图点位与每句画外音参考。3 分钟结构:

| 时段 | 画面 | 关键动作 |
|---|---|---|
| 0:00~0:20 | `/profile` | 粘贴简历 + JD,LLM 抽出 4 维度档案(停 3s 让看的人看清) |
| 0:20~0:50 | `/interview` | 5 题出来 → 答第 1 题故意答错 → 等 5~10s 看三轴 JSON 流式出现 |
| 0:50~1:20 | `/timeline` | 镜头扫状态计数,停 5s 在第一张"Redis AOF 答错"记忆卡 |
| 1:20~1:50 | `/interview` 第 2 场 | 镜头停 3s 在 WEAKNESS 槽的题(与第 1 场同领域),演示主动针对 |
| 1:50~2:10 | `/timeline` MASTERED | 点"标记已掌握"模拟状态机转移,tag 从黑底变边框 |
| 2:10~2:40 | `/kanban` | 拖拽流转 + 立即生成周报,镜头停在 stats 数字上 |
| 2:40~3:00 | `/api/mcp/memory/brief` | JSON 输出 + 收尾金句:"它不是聊天记录,是一套会合并、会遗忘、能被验证的认知记忆。" |

---

## 10. 简历弹药(实测)

### A/B 实验:弱点命中率 0% → 100%

| 组 | 弱点槽题数 | 真命中题数 | 弱点命中率 | 平均每场命中 |
|---|---:|---:|---:|---:|
| 对照(`memory.enabled=false`) | 40 | 0 | **0.0%** | 0.00 |
| 处理(`memory.enabled=true`) | 40 | 40 | **100.0%** | 2.00 |

绝对提升 **100 个百分点**;相对提升 ∞(对照基线 0);7 大领域全覆盖(Redis / MySQL / Kafka / Spring / JVM / Agent / CAP)。

### 30 条回归抽取命中率:**23/27 = 85%**(目标 ≥80%,达标)

### 端到端性能
- 1 场 5 题面试,平均 23s/题,LLM 真实点评
- 答完自动抽取 **11 条**弱点记忆入库
- 主题切换、画面密度、字号:零延迟(纯前端 localStorage)

### 简历文案(直接用)

> 独立开发"求职管家"AI 面试陪练(Python/FastAPI/SQLite+FTS5/sqlite-vec/vLLM):
> 出题-作答-三轴点评-复盘沉淀全流程,记忆引擎实现弱点抽取→强化→掌握→遗忘的完整生命周期。
>
> 实现认知记忆三机制(相似度 >0.85 合并、按类别半衰期读时衰减、用户负反馈闭环),
> 记忆开启后模拟面试 WEAKNESS 槽『真命中预设弱点』占比从 0.0% 提升到 100.0%
> (A/B 实验,各 20 场,弱点槽 80 题)。
>
> dogfood 30 天:__ 场模拟面试、__ 条记忆、有效引用率 __%,
> 并以 MCP 工具暴露记忆库供 IDE Agent 调用(`memory.search` / `memory.brief` / `profile.get`)。

---

## 11. 测试与 A/B 实验

| 测试 | 通过 | 备注 |
|---|---:|---|
| `test_w1.py` | 12/12 | 衰减 / 出题配比 / 点评 Schema / settings / 档案历史 |
| `test_w2.py` | 12/12 | 抽取 / 合并 / 状态机 / 30 条回归 |
| `test_w3.py` | 5/5 | `memory.enabled` 开关 / sid 偏移 |
| `test_w4.py` | 12/12 | 看板 / 周报 / MCP / APScheduler |
| `ab_report.py` | ✅ | 0% → 100%,7 领域全覆盖;产物 `data/ab_report.md` + `data/ab_report.json` |

**总计 41 个测试用例 + 1 个 A/B 实验脚本,全部通过。**

---

## 12. 关键避坑(已内化到代码)

1. **4B 点评质量不稳** → 三轴 rubric 固化进 prompt + JSON Schema 强约束 + `_normalize_keys` 容错(accuracy → tech_correctness、裸数字分保留、采分点对象转字符串)+ 重试 1 次;同步/流式两条路径容错对齐
2. **记忆垃圾化** → 质量门(必须含具体知识点)+ 场次上限 6 + 相似合并 + 负反馈 + 30 天零引用降权 + `memory.enabled` 开关可关
3. **SQLite 中文检索与并发** → FTS5 trigram + WAL + `busy_timeout=5000` + 单写者锁
4. **embedding 后端不可达** → 短路保护(1.5s connect + 60s 短路)避免阻塞
5. **流式输出乱码** → `TextDecoder.decode(value, { stream: true })`
6. **form submit 期望 HTML** → 浏览器原生 form 走 HTML 端点,fetch 走 JSON 端点
7. **重复抽取** → qa_log.`extracted_at` 幂等 + 30 天查重(含已出未答的 session_question)
8. **vLLM 思考模式** → 按厂商预设 + 用户开关决定是否带 `enable_thinking=false`(云 API 不识别该字段)
9. **SSE 静默断流** → 生成器异常显式推 `error` 事件;前端校验响应类型,非 SSE 错误直接展示;流结束未收到 done 恢复按钮
10. **点评落库阻塞** → done 事件在落库后立即返回,记忆抽取移入后台线程(串行锁 + 幂等);综合点评非阻塞抢锁
11. **时区错位** → DB 统一存 UTC(`datetime('now')`);Python 侧比较/展示按 UTC 或本地时区显式转换(report.py `_to_utc_naive`、tokens 日统计 `localtime`)
12. **题目重复** → 出题避重清单覆盖"已作答 + 已出未答"两类;三槽位并行生成 + 多层兜底保证题数恒定

---

## 13. 文件结构与重构说明

### 13.1 当前结构(Phase 1 + Phase 2 重构后)

```
job_butler/
├── .gitignore
├── .gitattributes                  # 统一换行符
├── .env.example                    # 环境变量示例(仅字段名+占位值)
├── LICENSE                         # MIT
├── pyproject.toml
├── uv.lock
├── README.md                       # 本文件
├── app/
│   ├── main.py                     # 73 行:仅 lifespan + 挂载 8 个 router
│   ├── config.py                   # 常量 + LLM 厂商预设(PROVIDER_PRESETS)
│   ├── db.py                       # SQLite + WAL + 单写者锁 + 7 张表 + settings kv
│   ├── llm.py                      # 仅底层:OpenAI 客户端 / 思考模式 / kwargs / token 记录
│   ├── llm_tasks.py                # 5 个 LLM 业务任务:profile/judge/出题/综合点评
│   ├── json_utils.py               # 公共:extract_json / normalize_keys / validate_schema
│   │                               #       / schema_hint / run_json_task / safe_call
│   ├── embed.py                    # 8001 客户端 + 短路保护
│   ├── scheduler.py                # APScheduler 周日 23:59
│   ├── mcp_server.py               # MCP Server 3 工具(mcp 1.x 兼容)
│   ├── routers/                    # 8 个路由子文件(总 ~480 行,每个 30~80 行)
│   │   ├── deps.py                 # 共享 templates 单例 + resolve_profile_id
│   │   ├── pages.py                # 6 个 HTML 页面(/  /profile  /interview  /timeline  /kanban  /settings)
│   │   ├── profile.py              # 解析/zip 上传/删除
│   │   ├── interview.py            # 开始/SSE 流式/会话历史/综合点评/删除
│   │   ├── memory.py               # 列表/标记无效-已掌握-恢复/手动抽取
│   │   ├── kanban.py               # 看板 CRUD + 阶段流转
│   │   ├── report.py               # 周报生成/列表/删除
│   │   ├── settings.py            # 读/改/重置 + 厂商预设 + tokens 统计
│   │   └── mcp_http.py             # MCP 三个工具的 HTTP 包装
│   ├── modules/
│   │   ├── profile.py              # 简历/JD 抽取 + 档案历史(命名/列表/删除)
│   │   ├── question_bank.py        # 50 条种子题(兜底题源)
│   │   ├── interview.py            # 并行 LLM 出题 + 作答 + SSE 点评 + 综合复盘
│   │   ├── memory_engine.py        # 抽取/合并/状态机/衰减/幂等
│   │   ├── kanban.py               # 投递看板 CRUD
│   │   ├── report.py               # 周报生成 + Markdown
│   │   └── settings.py             # 设置持久化 + tokens 统计 + api_key 掩码
│   ├── templates/                  # 7 个 Jinja2 页面(6 + session_detail)
│   └── static/.gitkeep
├── data/
│   ├── seed_questions.json         # 50 条题
│   ├── regression_30.json          # 抽取回归集
│   ├── ab_report.md / .json        # A/B 实验报告
│   └── demo_script.md              # 3 分钟演示脚本
└── tests/
    ├── conftest.py                 # 共享 fixture:clean_db / reset_settings
    ├── test_w1.py ~ test_w4.py     # 41 个测试(支持 --skip-slow 跳慢测)
    └── ab_report.py                # A/B 实验独立脚本
```

### 13.2 重构历程(按 commit 时间倒序)

| Commit | 主题 | 净效果 |
|---|---|---|
| `3fb6c91` refactor(phase2) | 拆 main.py 为 8 个 routers + 抽 llm_tasks | main.py 531→73 行(-86%);llm.py 357→220 行(-38%);新增 8 个 router 文件 + llm_tasks + conftest;**业务功能 0 变化** |
| `e27dc71` fix | 修 build_questions 跨槽撞题 + as_ui_dict key 命名 | 修 2 个真实 bug(其中一个是 Phase 1 重构引入) |
| `930b3d2` refactor(phase1) | 抽 json_utils + reset_client + _build_chat_kwargs + safe_call | 消除 5 处 LLM 任务重复样板(~400 行);净减 194 行;0 行为变化 |
| `5d53e89` feat W5 | 档案历史/按档案出题/综合点评/tokens 统计/多厂商接入 | W5 增强 |
| `6b337ee` docs | README 反映 W4 完成状态 | 文档更新 |
| `53fb7f3` feat | initial commit (W1~W4 完整版) | 初版 |

### 13.3 重构原则(为什么零行为变化)

| 原则 | 做法 |
|---|---|
| 不改业务接口 | `llm.py` 用 `__getattr__` 转发老 API,下游 import 不变 |
| 不改路由 URL | 8 个 router 拆出但 path 不动 |
| 不改 DB schema | 不在 Phase 1/2 加新表,只加 1 个 settings kv 字段(DEFAULTS 自动补) |
| 不改 prompt/schema | `run_json_task` 用 1 套函数替代 5 套重复样板,prompt 一字不动 |
| 不改 settings key 命名 | `as_ui_dict` 用 `(prefix, group, keep_prefix)` 三元组,前端模板按名取值不变 |
| 测试不删改 | 已有 test_w1~w4 全部 0 改动,仅加 `--skip-slow` 开关 |

### 13.4 不做的(避免过度重构)

- **Phase 3 策略模式抽象 Provider**:Provider 字段已聚合到 `config.PROVIDER_PRESETS`,运行时统一走 OpenAI 兼容协议,无策略模式价值
- **Pydantic TypedDict**:返回 dict 长度可控(5~10 字段),引入 pydantic 增加 1MB 依赖,得不偿失
- **Pydantic Settings 替代手写 settings.py**:当前 settings.py 73 行,白名单机制比 pydantic-settings 显式可控
- **FastAPI dependency injection 重写**:现有 `Form` / `Request` 参数风格清晰,不必迁移到 `Depends()`

---

## 14. License

本项目基于 [MIT License](LICENSE) 开源,详见根目录 `LICENSE` 文件。

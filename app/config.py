"""集中配置:LLM/Embedding/数据库/记忆引擎阈值。"""
import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DB_PATH = Path(os.getenv("JOB_BUTLER_DB", str(DATA_DIR / "job_butler.db")))
SEED_QUESTIONS_PATH = DATA_DIR / "seed_questions.json"
REGRESSION_PATH = DATA_DIR / "regression_30.json"

LLM_PROVIDER = os.getenv("LLM_PROVIDER", "vllm")
VLLM_BASE_URL = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
VLLM_API_KEY = os.getenv("VLLM_API_KEY", "EMPTY")
VLLM_MODEL = os.getenv("VLLM_MODEL", "qwen3-4b-awq")

LLM_TEMPERATURE = 0.0

# ---------- LLM 厂商预设(OpenAI 兼容格式) ----------
# 前端下拉选择后自动填充 base_url / model;api_key 需用户自行填写
# thinking_off: 该厂商的 Qwen3 类模型需要 chat_template_kwargs.enable_thinking=false
PROVIDER_PRESETS: dict[str, dict[str, str]] = {
    "vllm":      {"base_url": "http://localhost:8000/v1",                        "model": "qwen3-4b-awq",          "thinking_off": "true"},
    "deepseek":  {"base_url": "https://api.deepseek.com/v1",                     "model": "deepseek-chat",         "thinking_off": "false"},
    "openai":    {"base_url": "https://api.openai.com/v1",                       "model": "gpt-4o-mini",           "thinking_off": "false"},
    "moonshot":  {"base_url": "https://api.moonshot.cn/v1",                      "model": "kimi-k2-0905-preview",  "thinking_off": "false"},
    "zhipu":     {"base_url": "https://open.bigmodel.cn/api/paas/v4",            "model": "glm-4.5-air",           "thinking_off": "false"},
    "dashscope": {"base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1", "model": "qwen-plus",           "thinking_off": "true"},
    "custom":    {"base_url": "",                                                "model": "",                      "thinking_off": "false"},
}

EMBED_BASE_URL = os.getenv("EMBED_BASE_URL", "http://localhost:8001/v1")
EMBED_API_KEY = os.getenv("EMBED_API_KEY", "EMPTY")
# vLLM served-model-name(对齐部署标准,不带 HF 前缀)
BGE_MODEL_NAME = "bge-small-zh-v1.5"
BGE_DIM = 512

MERGE_THRESHOLD = 0.85
MAX_MEMORIES_PER_SESSION = 6

QUESTIONS_PER_SESSION = 5
WEAKNESS_RATIO = 2
RESUME_RATIO = 2
NEW_KNOWLEDGE_RATIO = 1

DEDUP_DAYS = 30

WEAK_HALF_LIFE_DAYS = 14.0
FACT_HALF_LIFE_DAYS = 60.0
PREF_HALF_LIFE_DAYS = 30.0

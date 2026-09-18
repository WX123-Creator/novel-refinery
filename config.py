"""
小说提炼工坊 - 模型配置与费用计算
================================
所有模型配置、定价、Token预算均在此管理。

API Key 配置说明：
- 沙箱环境：无需填写，平台自动管理
- 本地部署：请根据使用的模型档位，在下方填写对应 provider 的 API Key
"""

import os

# 极简 .env 加载器（本地部署时从 .env 读取密钥，避免硬编码进仓库；.env 已被 .gitignore 忽略）
def _load_dotenv(path=".env"):
    try:
        with open(path, encoding="utf-8") as _f:
            for _line in _f:
                _line = _line.strip()
                if not _line or _line.startswith("#") or "=" not in _line:
                    continue
                _k, _, _v = _line.partition("=")
                _k, _v = _k.strip(), _v.strip()
                if _k and _k not in os.environ:
                    os.environ[_k] = _v.strip('"').strip("'")
    except FileNotFoundError:
        pass

_load_dotenv()

# ============================================================
# API Key 配置（本地部署时填写）
# ============================================================
# 沙箱环境无需填写（平台自动管理）
# 日常使用：把真实 key 放进项目根目录 .env（一行 KEY=value），或设置同名环境变量；
#           仓库内仅保留占位符，真实密钥永不提交。
# 智谱 AI (GLM) ：https://open.bigmodel.cn/
# 字节跳动 豆包 ：https://console.volcengine.com/ark/
# 阿里云 通义  ：https://dashscope.console.aliyun.com/
# MiniMax      ：https://platform.minimaxi.com/
ZHIPU_API_KEY = os.environ.get("ZHIPU_API_KEY", "YOUR_ZHIPU_API_KEY_HERE")
DOUBAO_API_KEY = os.environ.get("DOUBAO_API_KEY", "YOUR_DOUBAO_API_KEY_HERE")
QWEN_API_KEY = os.environ.get("QWEN_API_KEY", "YOUR_QWEN_API_KEY_HERE")
MINIMAX_API_KEY = os.environ.get("MINIMAX_API_KEY", "YOUR_MINIMAX_API_KEY_HERE")

# Provider -> (API Key, Base URL) 映射
# 本地部署时 LLM 服务从此读取
PROVIDER_CONFIGS = {
    "智谱AI": {
        "api_key": ZHIPU_API_KEY,
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
    },
    "字节跳动": {
        "api_key": DOUBAO_API_KEY,
        "base_url": "https://ark.cn-beijing.volces.com/api/v3",
    },
    "阿里云": {
        "api_key": QWEN_API_KEY,
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
    },
    "MiniMax": {
        "api_key": MINIMAX_API_KEY,
        "base_url": "https://api.minimax.chat/v1",
    },
}

# 占位符列表（用于检测 key 是否已填写）
_PLACEHOLDER_KEYS = {
    "YOUR_ZHIPU_API_KEY_HERE",
    "YOUR_DOUBAO_API_KEY_HERE",
    "YOUR_QWEN_API_KEY_HERE",
    "YOUR_MINIMAX_API_KEY_HERE",
    "",
    None,
}


def is_api_key_configured(provider: str) -> bool:
    """检查指定 provider 的 API Key 是否已配置（非占位符）"""
    cfg = PROVIDER_CONFIGS.get(provider)
    if not cfg:
        return False
    key = cfg.get("api_key", "")
    return key not in _PLACEHOLDER_KEYS


def get_api_key_for_model(model_id: str) -> tuple:
    """
    根据模型 ID 获取对应的 (api_key, base_url, provider)
    
    解析优先级：
    1. 显式映射 MODEL_TO_PROVIDER（最可靠）
    2. 遍历 ALL_TIERS 查找（回退）
    
    Returns:
        (api_key, base_url, provider_name) 或 (None, None, None) 如果未找到
    """
    # 优先使用显式映射
    provider = MODEL_TO_PROVIDER.get(model_id)
    
    # 回退：遍历所有档位查找
    if not provider:
        for tier_key, tier in ALL_TIERS.items():
            if model_id in tier.get("models", {}):
                provider = tier["models"][model_id].get("provider", "")
                break
    
    if not provider:
        return None, None, None
    
    cfg = PROVIDER_CONFIGS.get(provider, {})
    return cfg.get("api_key"), cfg.get("base_url"), provider


# ============================================================
# 模型配置（三档）
# 使用沙箱环境提供的可用模型 ID
# ============================================================

# 第一档：免费测试档
TIER_FREE = {
    "name": "🆓 免费测试档",
    "models": {
        "glm-4.7-flash": {
            "display": "GLM-4.7-Flash（智谱免费）",
            "provider": "智谱AI",
            "input_price": 0,       # 元/1K tokens（免费）
            "output_price": 0,      # 元/1K tokens（免费）
            "context_window": 128000,
            "note": "测试阶段默认，免费使用",
        },
    },
    "default": "glm-4.7-flash",
}

# 第二档：性价比档
TIER_BALANCE = {
    "name": "⚖️ 性价比档",
    "models": {
        "glm-4.5-air": {
            "display": "GLM-4.5-Air（智谱）",
            "provider": "智谱AI",
            "input_price": 0.0008,
            "output_price": 0.006,
            "context_window": 128000,
            "note": "性价比推理，适合提炼场景，低思考强度",
        },
        "doubao-seed-2-0-lite-260215": {
            "display": "豆包 Seed 2.0 Lite",
            "provider": "字节跳动",
            "input_price": 0.0008,
            "output_price": 0.0016,
            "context_window": 256000,
            "note": "轻量高效，性价比之选",
        },
        "minimax-m2-5-260212": {
            "display": "MiniMax M2.5",
            "provider": "MiniMax",
            "input_price": 0.001,
            "output_price": 0.002,
            "context_window": 200000,
            "note": "编码与智能体能力强",
        },
    },
    "default": "glm-4.5-air",
}

# 第三档：质量档
TIER_QUALITY = {
    "name": "🏆 质量档",
    "models": {
        "glm-5.3-flash": {
            "display": "GLM-5.3-Flash（智谱付费）",
            "provider": "智谱AI",
            "input_price": 0.0008,     # 元/1K tokens（≈0.8元/百万，按公开行情修正）
            "output_price": 0.0028,    # 元/1K tokens（≈2.8元/百万，按公开行情修正）
            "context_window": 128000,
            "note": "快速推理，5折限时价，支持低思考强度",
        },
        "glm-5": {
            "display": "GLM-5（智谱旗舰）",
            "provider": "智谱AI",
            "input_price": 0.004,     # 元/1K tokens（≈4元/百万，按公开行情修正）
            "output_price": 0.018,    # 元/1K tokens（≈18元/百万，按公开行情修正）
            "context_window": 128000,
            "note": "新一代旗舰，Agent 级能力",
        },
    },
    "default": "glm-5.3-flash",
}

# 所有档位汇总（用于下拉选择）
ALL_TIERS = {
    "free": TIER_FREE,
    "balance": TIER_BALANCE,
    "quality": TIER_QUALITY,
}

# ============================================================
# 显式模型 → Provider 映射（最可靠的解析方式）
# 每个模型 ID 直接映射到对应的 provider 名称
# provider 名称必须与 PROVIDER_CONFIGS 的 key 完全一致
# ============================================================
MODEL_TO_PROVIDER = {
    # 智谱 AI (GLM 系列)
    "glm-4.7-flash": "智谱AI",
    "glm-4.5-air": "智谱AI",
    "glm-5.3-flash": "智谱AI",
    "glm-5": "智谱AI",
    # 字节跳动 豆包 (Doubao 系列)
    "doubao-seed-2-0-lite-260215": "字节跳动",
    # MiniMax
    "minimax-m2-5-260212": "MiniMax",
}

# ============================================================
# 成本控制参数
# ============================================================

# Token 预算硬上限（总输入 + 总输出 tokens）
# 设置为 0 表示不限制
MAX_TOTAL_TOKENS = 50_000_000  # Token 预算硬上限（总输入+总输出），5000万 tokens

# 单次 LLM 调用的最大输出 tokens
MAX_OUTPUT_TOKENS_PER_CALL = 4096

# 单块重试上限次数（免费模型易触发限流，适当增加）
MAX_RETRIES_PER_BLOCK = 5

# 估算 token 数（中文字符 ≈ 1.5 tokens）
TOKENS_PER_CHAR = 1.5

# 每块最大字符数（避免超长上下文）
MAX_CHARS_PER_BLOCK = 6000

# 调用间隔（秒），避免连续请求触发限流，免费模型建议 1~2 秒
CALL_INTERVAL_SECONDS = 1.5

# 检查点文件路径
CHECKPOINT_DIR = "checkpoints"
CHECKPOINT_FILE = "checkpoint.json"

# ============================================================
# 工具函数
# ============================================================

def estimate_tokens(text: str) -> int:
    """估算文本的 token 数"""
    return int(len(text) * TOKENS_PER_CHAR)


def get_context_window(model_id: str, tier_key: str) -> int:
    """获取指定模型的上下文窗口长度（tokens）；取不到时返回一个安全默认值"""
    tier = ALL_TIERS.get(tier_key)
    if tier and model_id in tier["models"]:
        return int(tier["models"][model_id].get("context_window", 128000))
    return 128000


def calc_cost(input_tokens: int, output_tokens: int, model_id: str, tier_key: str) -> float:
    """计算费用（元）"""
    tier = ALL_TIERS.get(tier_key)
    if not tier:
        return 0.0
    model_config = tier["models"].get(model_id)
    if not model_config:
        return 0.0
    input_cost = (input_tokens / 1000) * model_config["input_price"]
    output_cost = (output_tokens / 1000) * model_config["output_price"]
    return round(input_cost + output_cost, 6)


def get_model_display_name(model_id: str, tier_key: str) -> str:
    """获取模型显示名称"""
    tier = ALL_TIERS.get(tier_key)
    if tier and model_id in tier["models"]:
        return tier["models"][model_id]["display"]
    return model_id


def get_default_model(tier_key: str) -> str:
    """获取指定档位的默认模型"""
    tier = ALL_TIERS.get(tier_key)
    if tier:
        return tier["default"]
    return "glm-4.7-flash"


def get_tier_models_for_dropdown(tier_key: str) -> list:
    """获取指定档位所有模型（用于下拉框）"""
    tier = ALL_TIERS.get(tier_key)
    if not tier:
        return []
    return [
        (f"{cfg['display']} - {cfg['provider']}", model_id)
        for model_id, cfg in tier["models"].items()
    ]
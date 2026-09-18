"""
LLM 服务封装
-----------
支持两种运行模式：
1. 沙箱模式：使用 coze-coding-dev-sdk 的 LLMClient（平台自动管理 API Key）
2. 本地模式：使用 openai SDK + config.py 中配置的 API Key

自动检测运行环境，优先使用沙箱模式，回退到本地模式。
"""

import json
import os
import sys
import threading
import time
from typing import Optional

from langchain_core.messages import SystemMessage, HumanMessage

from config import (
    MAX_OUTPUT_TOKENS_PER_CALL,
    MAX_RETRIES_PER_BLOCK,
    CALL_INTERVAL_SECONDS,
    calc_cost,
    estimate_tokens,
    get_api_key_for_model,
    is_api_key_configured,
    MODEL_TO_PROVIDER,
)


def _is_sandbox() -> bool:
    """检测是否在沙箱环境中运行"""
    return os.environ.get("COZE_WORKSPACE_PATH") is not None


def _safe_print(msg: str) -> None:
    """
    安全打印，处理 Windows GBK 编码问题
    遇到无法编码的字符时自动替换，避免崩溃
    """
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        # Windows GBK 环境下，替换无法编码的字符
        print(msg.encode("utf-8", errors="replace").decode("utf-8", errors="replace"), flush=True)


class LLMService:
    """LLM 调用服务封装"""

    def __init__(self, tier_key: str = "free", model_id: Optional[str] = None):
        """
        初始化 LLM 服务
        Args:
            tier_key: 档位 key ('free' | 'balance' | 'quality')
            model_id: 模型 ID，None 则使用档位默认
        """
        from config import get_default_model, ALL_TIERS

        self.tier_key = tier_key
        self.model_id = model_id or get_default_model(tier_key)

        # 获取模型对应的 provider（优先使用显式映射 MODEL_TO_PROVIDER）
        self.provider = MODEL_TO_PROVIDER.get(self.model_id, "")

        # 回退：从指定档位的模型配置中查找
        if not self.provider:
            tier = ALL_TIERS.get(tier_key, {})
            model_cfg = tier.get("models", {}).get(self.model_id, {})
            self.provider = model_cfg.get("provider", "")

        # 再回退：遍历所有档位查找
        if not self.provider:
            for t in ALL_TIERS.values():
                if self.model_id in t.get("models", {}):
                    self.provider = t["models"][self.model_id].get("provider", "")
                    break

        # 是否关闭模型的 thinking/深度思考模式
        # 免费档模型（如 GLM-4.7-Flash）默认开启混合思考，小说提炼不需要深度推理
        # 关闭后单块处理速度可提升 10~50 倍，且不影响提炼质量
        self.disable_thinking = (tier_key == "free")

        # 推理强度控制：付费模型可设置低思考强度以平衡速度与质量
        # glm-5.3-flash 支持 reasoning_effort 参数（low/medium/high）
        # 小说提炼不需要深度推理，设为 low 可显著提速并节省推理 token
        self.reasoning_effort: str | None = None
        if self.model_id in ("glm-5.3-flash", "glm-4.5-air"):
            self.reasoning_effort = "low"

        # 自测：打印解析结果（方便调试）
        _safe_print(f"[LLMService] model_id={self.model_id}, tier={tier_key}, "
                    f"provider=\"{self.provider}\", disable_thinking={self.disable_thinking}, "
                    f"reasoning_effort={self.reasoning_effort}")

        # 决定运行模式
        self.use_sandbox = _is_sandbox()
        self.client = None
        self.openai_client = None

        if self.use_sandbox:
            # 沙箱模式：使用 coze-coding-dev-sdk
            try:
                from coze_coding_dev_sdk import LLMClient
                self.client = LLMClient()
            except ImportError:
                # SDK 不可用，回退到本地模式
                self.use_sandbox = False

        if not self.use_sandbox:
            # 本地模式：使用 openai SDK
            api_key, base_url, resolved_provider = get_api_key_for_model(self.model_id)
            # 使用 get_api_key_for_model 解析出的 provider（更可靠）
            if resolved_provider:
                self.provider = resolved_provider
            if not api_key or api_key.startswith("YOUR_"):
                raise ValueError(
                    f"API Key 未配置！请在 config.py 中为 {self.provider or '对应Provider'} 填写有效的 API Key。\n"
                    f"当前模型: {self.model_id}，Provider: {self.provider or '未识别'}"
                )
            try:
                from openai import OpenAI
                self.openai_client = OpenAI(api_key=api_key, base_url=base_url)
            except ImportError:
                raise ImportError(
                    "本地模式需要 openai 包。请运行: pip install openai"
                )

        # 累计统计
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_cost = 0.0
        self.total_calls = 0
        self._stats_lock = threading.Lock()

    def get_stats(self) -> dict:
        """获取当前累计统计"""
        return {
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_cost": self.total_cost,
            "total_calls": self.total_calls,
        }

    def get_cost_summary(self) -> str:
        """获取费用摘要字符串"""
        stats = self.get_stats()
        total_tokens = stats["total_input_tokens"] + stats["total_output_tokens"]
        return (
            f"[Stats] Calls: {stats['total_calls']} | "
            f"Input Tokens: {stats['total_input_tokens']:,} | "
            f"Output Tokens: {stats['total_output_tokens']:,} | "
            f"Total Tokens: {total_tokens:,} | "
            f"Cost: {stats['total_cost']:.6f} CNY"
        )

    def invoke(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.3,
        max_retries: int = MAX_RETRIES_PER_BLOCK,
    ) -> tuple:
        """
        调用 LLM（非流式，带重试）
        Returns:
            (content, meta) - content 为文本内容，meta 包含 token 统计
        Raises:
            RuntimeError: 所有重试均失败
        """
        last_error = None
        for attempt in range(max_retries + 1):
            try:
                if self.use_sandbox:
                    content, usage = self._invoke_sandbox(
                        system_prompt, user_prompt, temperature
                    )
                else:
                    content, usage = self._invoke_local(
                        system_prompt, user_prompt, temperature
                    )

                input_tokens = usage.get("input_tokens", 0) or estimate_tokens(
                    system_prompt + user_prompt
                )
                output_tokens = usage.get("output_tokens", 0) or estimate_tokens(
                    content
                )

                # 更新累计统计
                self._stats_lock.acquire()
                try:
                    self.total_input_tokens += input_tokens
                    self.total_output_tokens += output_tokens
                    cost = calc_cost(
                        input_tokens, output_tokens, self.model_id, self.tier_key
                    )
                    self.total_cost += cost
                    self.total_calls += 1
                finally:
                    self._stats_lock.release()

                meta = {
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "cost": cost,
                    "attempt": attempt + 1,
                }
                return content, meta

            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    # 对 500 错误（服务端限流/过载）使用更长的退避时间
                    err_str = str(e)
                    if "500" in err_str or "1234" in err_str:
                        wait = 10 * (attempt + 1)  # 10s, 20s, 30s, 40s, 50s
                    else:
                        wait = 3 * (2 ** attempt)  # 3s, 6s, 12s, 24s, 48s
                    print(
                        f"[WARN] LLM call failed (attempt {attempt+1}/{max_retries+1}), "
                        f"retrying in {wait}s: {e}",
                        flush=True,
                    )
                    time.sleep(wait)
                else:
                    raise RuntimeError(
                        f"LLM 调用失败，已重试 {max_retries} 次: {last_error}"
                    )

    def _invoke_sandbox(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> tuple:
        """沙箱模式调用"""
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(content=user_prompt),
        ]

        response = self.client.invoke(
            messages=messages,
            model=self.model_id,
            temperature=temperature,
            max_completion_tokens=MAX_OUTPUT_TOKENS_PER_CALL,
            thinking_enabled=not self.disable_thinking,
            reasoning_effort=self.reasoning_effort if self.reasoning_effort else None,
        )

        content = self._extract_text(response.content)
        usage = response.response_metadata.get("usage", {})
        return content, usage

    def _invoke_local(
        self, system_prompt: str, user_prompt: str, temperature: float
    ) -> tuple:
        """本地模式调用（OpenAI 兼容 API）"""
        kwargs = dict(
            model=self.model_id,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=temperature,
            max_tokens=MAX_OUTPUT_TOKENS_PER_CALL,
        )
        # 关闭 thinking 深度思考模式（免费档模型提速关键）
        if self.disable_thinking:
            kwargs["extra_body"] = {"thinking": {"enabled": False}}
        # 低推理强度：付费模型可设置 reasoning_effort 以平衡速度与质量
        elif self.reasoning_effort:
            extra = kwargs.get("extra_body", {})
            extra["reasoning_effort"] = self.reasoning_effort
            kwargs["extra_body"] = extra
        response = self.openai_client.chat.completions.create(**kwargs)

        content = response.choices[0].message.content or ""
        usage_data = response.usage
        usage = {}
        if usage_data:
            usage = {
                "input_tokens": getattr(usage_data, "prompt_tokens", 0),
                "output_tokens": getattr(usage_data, "completion_tokens", 0),
            }
        return content.strip(), usage

    def _extract_text(self, content) -> str:
        """安全提取文本内容"""
        if isinstance(content, str):
            return content.strip()
        elif isinstance(content, list):
            if content and isinstance(content[0], str):
                return " ".join(content).strip()
            else:
                return " ".join(
                    item.get("text", "")
                    for item in content
                    if isinstance(item, dict) and item.get("type") == "text"
                )
        return str(content).strip()

"""OpenAI 兼容后端 —— DeepSeek / Kimi(Moonshot) / 任何 OpenAI 兼容端点。

**为什么不复用 AnthropicBackend + LiteLLM 翻译**：实测确认 LiteLLM 的
Anthropic 形状 `/v1/messages` 能路由到 DeepSeek/Moonshot（请求确实到了上游），
但我们依赖的 `output_config.format`（结构化输出）是 Anthropic 专有字段，
能否被忠实翻译成对方的 `response_format` 无法验证。结构化输出一旦被静默丢弃，
Subagent 的动作解析就会崩 —— 这种失败模式比不支持更糟。

所以这里直接说 OpenAI 方言，不经翻译。可以直连供应商，也可以指向 LiteLLM 的
`/v1/chat/completions` 以保留 virtual key 的预算强制（§10.3）。

JSON 可靠性靠三层，而不是赌供应商的 schema 支持：
  1. system prompt 里写死 schema 并要求只输出 JSON
  2. 支持时带上 response_format={"type":"json_object"}
  3. 本地用 Runtime 的 validate_schema 校验，不合格就带着错误再问一轮
三轮都不行就抛 ModelCallFailed —— 变成硬信号交给架构师，而不是塞个空动作继续跑。
"""

from __future__ import annotations

import json
import os
import re
from typing import Any

from ..actions import AgentAction
from ..llm import ArchitectVerdict, Triage
from ..llm.errors import ModelCallFailed, from_provider_error
from ..runtime.detectors import validate_schema
from ..types import AgentContext, Signal, TaskSpec
from .anthropic_backend import (
    ACTION_SCHEMA,
    ARCHITECT_SYSTEM,
    SUBAGENT_SYSTEM,
    TRIAGE_SCHEMA,
    TRIAGE_SYSTEM,
    VERDICT_SCHEMA,
    _parse_action,
    _render_subagent_context,
)

# 已知端点。直连时用这些，走 LiteLLM 时用 http://localhost:4000/v1
PRESETS = {
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "litellm": "http://localhost:4000/v1",
}

# deepseek-reasoner 不支持 response_format / 温度等参数，只能靠提示词约束
_NO_JSON_MODE = ("deepseek-reasoner", "reasoner")

_FENCE = re.compile(r"^\s*```(?:json)?\s*(.*?)\s*```\s*$", re.S)


class OpenAICompatBackend:
    name = "openai-compat"

    def __init__(
        self,
        *,
        base_url: str | None = None,
        api_key: str | None = None,
        subagent_model: str | None = None,
        architect_model: str = "deepseek-chat",
        triage_model: str | None = None,
        max_tokens: int = 4096,
        max_retries: int = 2,
        repair_rounds: int = 1,
    ) -> None:
        import openai  # 延迟导入：跑脚本后端时不需要

        url = base_url or os.environ.get("COWORK_LLM_BASE_URL") or PRESETS["litellm"]
        key = (
            api_key
            or os.environ.get("COWORK_LLM_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("MOONSHOT_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
            or "placeholder"
        )
        self.client = openai.OpenAI(base_url=url, api_key=key, max_retries=max_retries)
        self.base_url = url
        # subagent_model 为 None 时用 TaskSpec.model —— 保留「不同模型干擅长的事」；
        # 设了就全局覆盖，方便单供应商下的联调。
        self.subagent_model = subagent_model
        self.architect_model = architect_model
        self.triage_model = triage_model or architect_model
        self.max_tokens = max_tokens
        self.repair_rounds = repair_rounds

    # ------------------------------------------------------------------ #

    def _call(
        self, *, model: str, system: str, user: str, schema: dict[str, Any]
    ) -> tuple[dict[str, Any], int]:
        import openai

        sys_prompt = (
            f"{system}\n\n"
            "只输出一个 JSON 对象，不要 markdown 代码块，不要任何解释文字。\n"
            "必须严格符合下面这个 JSON schema：\n"
            f"{json.dumps(schema, ensure_ascii=False)}"
        )
        messages: list[dict[str, str]] = [
            {"role": "system", "content": sys_prompt},
            {"role": "user", "content": user},
        ]

        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "messages": messages,
        }
        if not any(m in model for m in _NO_JSON_MODE):
            kwargs["response_format"] = {"type": "json_object"}

        tokens = 0
        errors: list[str] = []
        for attempt in range(self.repair_rounds + 1):
            try:
                resp = self.client.chat.completions.create(**kwargs)
            except openai.APIStatusError as exc:
                # 代理侧的预算拒绝在这里也要变成 BUDGET_EXCEEDED（§7.2）
                raise from_provider_error(exc.status_code, _error_text(exc)) from exc
            except openai.APIConnectionError as exc:
                raise ModelCallFailed(f"连不上模型服务: {exc}") from exc

            usage = getattr(resp, "usage", None)
            if usage:
                tokens += (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)

            text = (resp.choices[0].message.content or "").strip()
            data, errors = _parse_and_validate(text, schema)
            if data is not None:
                return data, tokens

            if attempt >= self.repair_rounds:
                break
            # 带着具体错误再问一轮
            kwargs["messages"] = [
                *messages,
                {"role": "assistant", "content": text[:4000]},
                {
                    "role": "user",
                    "content": "上面的输出不合格：\n"
                    + "\n".join(errors)
                    + "\n只重新输出修正后的 JSON 对象。",
                },
            ]

        raise ModelCallFailed(
            f"模型连续 {self.repair_rounds + 1} 轮未产出合规 JSON: " + "; ".join(errors)
        )

    # -- Subagent ---------------------------------------------------------- #

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        data, tokens = self._call(
            model=self.subagent_model or ctx.task_spec.model,
            system=SUBAGENT_SYSTEM,
            user=_render_subagent_context(ctx),
            schema=ACTION_SCHEMA,
        )
        return _parse_action(data), tokens

    # -- 架构师 ------------------------------------------------------------ #

    def triage(self, signals: list[Signal]) -> tuple[list[Triage], int]:
        if not signals:
            return [], 0
        listing = "\n".join(
            f"- id={s.id} type={s.type.value} detail={s.payload.get('detail', '')}"
            for s in signals
        )
        data, tokens = self._call(
            model=self.triage_model,
            system=TRIAGE_SYSTEM,
            user=f"软信号队列：\n{listing}",
            schema=TRIAGE_SCHEMA,
        )
        return (
            [Triage(v["signal_id"], v["verdict"], v.get("reason", "")) for v in data["verdicts"]],
            tokens,
        )

    def decide_interrupt(
        self, spec: TaskSpec, signals: list[Signal], ctx: AgentContext
    ) -> tuple[ArchitectVerdict, int]:
        evidence = "\n\n".join(
            f"[{s.type.value}] payload={json.dumps(s.payload, ensure_ascii=False)}\n"
            f"证据:\n{(s.raw_evidence or '')[:4000]}"
            for s in signals
        )
        user = (
            f"TaskSpec:\n{json.dumps(spec.to_dict(), ensure_ascii=False, indent=2)}\n\n"
            f"触发信号:\n{evidence}\n\n"
            "已产出:\n" + "\n".join(f"- {a.content_ref}" for a in ctx.produced)
        )
        data, tokens = self._call(
            model=self.architect_model,
            system=ARCHITECT_SYSTEM,
            user=user,
            schema=VERDICT_SCHEMA,
        )
        changes: dict[str, Any] = {}
        if data.get("new_goal"):
            changes["goal"] = data["new_goal"]
        if data.get("added_criteria"):
            changes["added_criteria"] = data["added_criteria"]
        return (
            ArchitectVerdict(
                action=data["action"],
                rationale=data["rationale"],
                complexity_score=float(data["complexity_score"]),
                spec_changes=changes,
            ),
            tokens,
        )

    def summarize(self, ctx: AgentContext) -> tuple[str, int]:
        files = "\n".join(f"- {a.content_ref}: {a.summary}" for a in ctx.produced)
        data, tokens = self._call(
            model=self.triage_model,
            system="把已产出成果压缩成一段摘要，供新 Subagent 作为只读上下文。",
            user=f"目标：{ctx.task_spec.goal}\n已产出：\n{files or '（无）'}",
            schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
        )
        return data["summary"], tokens

    def verify(self, spec: TaskSpec, ctx: AgentContext) -> tuple[bool, str, int]:
        manual = [c for c in spec.acceptance if not c.machine_checkable]
        if not manual:
            return True, "无需人工判定的验收标准", 0
        data, tokens = self._call(
            model=self.architect_model,
            system="你在验收 Subagent 的产出。逐条对照验收标准，给出通过与否和理由。",
            user=(
                "验收标准：\n"
                + "\n".join(f"- {c.id}: {c.description}" for c in manual)
                + f"\n\n产出摘要：{ctx.summary}\n"
                + "\n".join(f"- {a.content_ref}" for a in ctx.produced)
            ),
            schema={
                "type": "object",
                "properties": {
                    "passed": {"type": "boolean"},
                    "reason": {"type": "string"},
                },
                "required": ["passed", "reason"],
                "additionalProperties": False,
            },
        )
        return data["passed"], data["reason"], tokens


# --------------------------------------------------------------------------- #


def _parse_and_validate(
    text: str, schema: dict[str, Any]
) -> tuple[dict[str, Any] | None, list[str]]:
    """解析 + 本地校验。返回 (数据, 错误列表)。"""
    m = _FENCE.match(text)
    if m:
        text = m.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        return None, [f"不是合法 JSON: {exc}"]
    if not isinstance(data, dict):
        return None, [f"顶层必须是对象，实际是 {type(data).__name__}"]
    errs = validate_schema(data, schema)
    return (None, errs) if errs else (data, [])


def _error_text(exc) -> str:
    parts = [str(getattr(exc, "message", "") or ""), str(exc)]
    body = getattr(exc, "body", None)
    if body is not None:
        parts.append(json.dumps(body, ensure_ascii=False, default=str))
    resp = getattr(exc, "response", None)
    if resp is not None:
        try:
            parts.append(resp.text)
        except Exception:
            pass
    return "\n".join(p for p in parts if p)

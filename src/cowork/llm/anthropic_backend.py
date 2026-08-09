"""真实模型后端。官方 Anthropic SDK。

关于 §10.3 的 LiteLLM：LiteLLM 自托管代理暴露 Anthropic 兼容的 /v1/messages，
所以把 COWORK_LLM_BASE_URL 指向代理、api_key 用 virtual key，就同时拿到
「按任务归集用量并强制上限」和官方 SDK。不需要引第三方 shim。

    export COWORK_LLM_BASE_URL=http://localhost:4000
    export ANTHROPIC_API_KEY=sk-litellm-virtualkey-for-this-task

不设 base_url 时直连 Anthropic API。
"""

from __future__ import annotations

import json
import os
from typing import Any

from ..actions import AgentAction, Finish, SoftSignalAction, ToolCall
from ..llm import ArchitectVerdict, Triage
from ..llm.errors import ModelCallFailed, from_provider_error
from ..types import AgentContext, Signal, TaskSpec


def _error_text(exc) -> str:
    """把 provider 错误摊平成一段可匹配的文本。"""
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

# 结构化输出的 schema 必须 additionalProperties=false 且列全 required，
# 所以这里把所有字段都设为必填，用空值表示「不适用」。
ACTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["tool_call", "finish", "soft_signal"]},
        "thought": {"type": "string"},
        "tool": {"type": "string", "enum": ["write_file", "read_file", "run", ""]},
        "path": {"type": "string"},
        "content": {"type": "string"},
        "command": {"type": "array", "items": {"type": "string"}},
        "output_json": {"type": "string"},
        "summary": {"type": "string"},
        "signal_type": {
            "type": "string",
            "enum": [
                "AMBIGUITY",
                "ASSUMPTION_BROKEN",
                "CONFLICT_SUSPECTED",
                "RESOURCE_NEEDED",
                "PROGRESS",
                "",
            ],
        },
        "detail": {"type": "string"},
    },
    "required": [
        "kind", "thought", "tool", "path", "content", "command",
        "output_json", "summary", "signal_type", "detail",
    ],
    "additionalProperties": False,
}

VERDICT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["CONTINUE", "MODIFY_TASK", "ABANDON", "REASSIGN"],
        },
        "rationale": {"type": "string"},
        "complexity_score": {"type": "number"},
        "new_goal": {"type": "string"},
        "added_criteria": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "description": {"type": "string"},
                },
                "required": ["id", "description"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["action", "rationale", "complexity_score", "new_goal", "added_criteria"],
    "additionalProperties": False,
}

TRIAGE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "verdicts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "signal_id": {"type": "string"},
                    "verdict": {"type": "string", "enum": ["ignore", "escalate"]},
                    "reason": {"type": "string"},
                },
                "required": ["signal_id", "verdict", "reason"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["verdicts"],
    "additionalProperties": False,
}

SUBAGENT_SYSTEM = """你是一个 Subagent，只与架构师通信，不与其他 Subagent 通信。
每次只输出**一个**动作。Runtime 会执行它并把结果追加到你的上下文里。

可用动作：
- tool_call + tool=write_file，需要 path / content
- tool_call + tool=read_file，需要 path
- tool_call + tool=run，需要 command（argv 数组）
- finish，需要 summary 和 output_json（符合 output_schema 的 JSON 字符串）
- soft_signal，需要 signal_type 和 detail —— 用于歧义、前提失效、需要额外资源等。
  软信号不会立即中断你，架构师会在检查点批量消费。

不适用的字段填空字符串或空数组。只写 TaskSpec.scope 允许的路径——
越界会被 Runtime 拦截并触发 SCOPE_VIOLATION。"""

ARCHITECT_SYSTEM = """你是架构师，是系统里唯一的写入决策点。
一个 Subagent 刚被硬信号中断。基于信号证据决定：

- CONTINUE     瞬时故障，原样重试
- MODIFY_TASK  规格不清或前提变化，需要改 TaskSpec
- REASSIGN     换个 Subagent 重做
- ABANDON      方向错误，放弃

complexity_score 是你对这次决策复杂度的自评（0.0~1.0）。诚实评估：
高分会触发升级给人。注意你**不知道自己不知道什么**，涉及不可逆操作、
反复中断、触及顶层任务意图时，即使你觉得简单也应给高分。

MODIFY_TASK 时，new_goal 留空表示目标不变（只改验收标准/范围），
added_criteria 是要补充的验收标准。"""

TRIAGE_SYSTEM = """你在做廉价批量分诊。对每条软信号只输出 ignore 或 escalate。
escalate 只给「架构师必须介入才能推进」的信号。不要做完整推理。"""


class AnthropicBackend:
    name = "anthropic"

    def __init__(
        self,
        *,
        architect_model: str = "claude-opus-5",
        triage_model: str = "claude-haiku-4-5",
        effort: str = "high",
        max_tokens: int = 16000,
        base_url: str | None = None,
        api_key: str | None = None,
        max_retries: int = 2,
    ) -> None:
        import anthropic  # 延迟导入：跑脚本后端时不需要这个依赖

        kwargs: dict[str, Any] = {"max_retries": max_retries}
        url = base_url or os.environ.get("COWORK_LLM_BASE_URL")
        if url:
            kwargs["base_url"] = url
        if api_key:
            kwargs["api_key"] = api_key
        self.client = anthropic.Anthropic(**kwargs)
        self.architect_model = architect_model
        self.triage_model = triage_model
        self.effort = effort
        self.max_tokens = max_tokens

    # ------------------------------------------------------------------ #

    def _call(
        self,
        *,
        model: str,
        system: str,
        user: str,
        schema: dict[str, Any],
        thinking: bool = True,
        effort: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": system,
            "messages": [{"role": "user", "content": user}],
            "output_config": {"format": {"type": "json_schema", "schema": schema}},
        }
        if thinking:
            kwargs["thinking"] = {"type": "adaptive"}
            kwargs["output_config"]["effort"] = effort or self.effort

        import anthropic

        try:
            resp = self.client.messages.create(**kwargs)
        except anthropic.APIStatusError as exc:
            # 关键：代理侧的预算拒绝要变成 BUDGET_EXCEEDED 硬信号，
            # 而不是让整个 run 崩在这里。
            raise from_provider_error(exc.status_code, _error_text(exc)) from exc
        except anthropic.APIConnectionError as exc:
            raise ModelCallFailed(f"连不上模型服务: {exc}") from exc

        tokens = resp.usage.input_tokens + resp.usage.output_tokens
        if resp.stop_reason == "refusal":
            raise ModelCallFailed(f"模型拒绝了请求: {resp.stop_details}")
        text = next((b.text for b in resp.content if b.type == "text"), "{}")
        return json.loads(text), tokens

    # -- Subagent ---------------------------------------------------------- #

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        data, tokens = self._call(
            model=ctx.task_spec.model,
            system=SUBAGENT_SYSTEM,
            user=_render_subagent_context(ctx),
            schema=ACTION_SCHEMA,
            effort="medium",
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
        # 廉价评估走小模型，且不开 thinking —— 这一步的成本必须压住（§3.4）
        data, tokens = self._call(
            model=self.triage_model,
            system=TRIAGE_SYSTEM,
            user=f"软信号队列：\n{listing}",
            schema=TRIAGE_SCHEMA,
            thinking=False,
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
            f"已产出:\n" + "\n".join(f"- {a.content_ref}" for a in ctx.produced)
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
            system="把已产出成果压缩成一段摘要，供新 Subagent 作为只读上下文。只输出摘要。",
            user=f"目标：{ctx.task_spec.goal}\n已产出：\n{files or '（无）'}",
            schema={
                "type": "object",
                "properties": {"summary": {"type": "string"}},
                "required": ["summary"],
                "additionalProperties": False,
            },
            thinking=False,
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
                f"验收标准：\n"
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


def _parse_action(d: dict[str, Any]) -> AgentAction:
    kind = d["kind"]
    if kind == "tool_call":
        tool = d["tool"]
        if tool == "write_file":
            args = {"path": d["path"], "content": d["content"]}
        elif tool == "read_file":
            args = {"path": d["path"]}
        elif tool == "run":
            args = {"command": list(d["command"])}
        else:
            raise ValueError(f"未知工具: {tool!r}")
        return ToolCall(name=tool, args=args, thought=d.get("thought", ""))
    if kind == "finish":
        try:
            output = json.loads(d["output_json"] or "{}")
        except json.JSONDecodeError:
            output = {}
        return Finish(output=output, summary=d.get("summary", ""), thought=d.get("thought", ""))
    if kind == "soft_signal":
        return SoftSignalAction(signal_type=d["signal_type"], detail=d.get("detail", ""))
    raise ValueError(f"未知动作 kind: {kind!r}")


def _render_subagent_context(ctx: AgentContext) -> str:
    """Subagent 上下文由架构师完全构造（§8）。这里只做渲染。"""
    spec = ctx.task_spec
    parts = [
        f"# 目标\n{spec.goal}",
        "# 验收标准\n"
        + "\n".join(
            f"- {c.id}: {c.description}"
            + (f"（Runtime 会执行 `{' '.join(c.command)}` 校验）" if c.command else "")
            for c in spec.acceptance
        ),
        f"# 允许写入的路径（scope）\n{spec.scope}",
        f"# 产出结构（output_schema）\n{json.dumps(spec.output_schema, ensure_ascii=False)}",
    ]
    if ctx.injected:
        parts.append(
            "# 注入的只读上下文\n"
            + "\n".join(f"- {a.content_ref}: {a.summary}" for a in ctx.injected)
        )
    if ctx.produced:
        parts.append("# 已产出\n" + "\n".join(f"- {a.content_ref}" for a in ctx.produced))
    if ctx.reasoning_trace:
        recent = ctx.reasoning_trace[-12:]
        parts.append(
            "# 最近的执行记录\n"
            + json.dumps(recent, ensure_ascii=False, indent=2)[:8000]
        )
    return "\n\n".join(parts)

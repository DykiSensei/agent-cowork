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

import hashlib
import json
import os
import re
from typing import Any

from ..actions import AgentAction
from ..llm import ArchitectVerdict, CacheStats, SubtaskDraft, TaskProfile, Triage
from ..llm.effort import resolve
from ..llm.errors import MissingApiKey, ModelCallFailed, from_provider_error
from ..runtime.detectors import validate_schema
from ..types import AgentContext, Signal, TaskSpec
from .prompts import with_extra
from .anthropic_backend import (
    ACTION_SCHEMA,
    ARCHITECT_SYSTEM,
    DECOMPOSE_MAX_TOKENS,
    DECOMPOSE_SCHEMA,
    DECOMPOSE_SYSTEM,
    PROBE_SCHEMA,
    PROBE_SYSTEM,
    PROFILE_SCHEMA,
    PROFILE_SYSTEM,
    REVIEW_MAX_TOKENS,
    REVIEW_SCHEMA,
    REVIEW_SYSTEM,
    SPEC_REVIEW_MAX_TOKENS,
    SPEC_REVIEW_SCHEMA,
    SPEC_REVIEW_SYSTEM,
    SUBAGENT_SYSTEM,
    TRIAGE_SCHEMA,
    TRIAGE_SYSTEM,
    VERDICT_SCHEMA,
    _parse_action,
    _parse_drafts,
    _parse_profiles,
    _render_architect_context,
    _render_decompose_context,
    _render_probe_context,
    _render_profile_context,
    _render_review_context,
    _render_spec_review_context,
    _render_subagent_context,
)

# 已知端点。直连时用这些，走 LiteLLM 时用 http://localhost:4000/v1。
# 完整的一张表在 cli.PROVIDERS —— 这里只留后端自己兜底用的几个。
PRESETS = {
    "deepseek": "https://api.deepseek.com/v1",
    "moonshot": "https://api.moonshot.cn/v1",
    "litellm": "http://localhost:4000/v1",
}

# 自托管 / 本机代理：这些地址上没有 key 是正常的，占位符照发。
_SELF_HOSTED_HOSTS = ("localhost", "127.0.0.1", "::1", "0.0.0.0", "host.docker.internal")


def _is_self_hosted(url: str) -> bool:
    """这个端点是不是自己机器上的代理。

    判据只看主机名，不看端口 —— LiteLLM 默认 4000，但换端口是常事。
    解析不出来就当成**外部**端点：这里的默认要偏严，
    宁可多问一次 key，也不要把占位符发到别人的服务器上。
    """
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").lower()
    except ValueError:
        return False
    return host in _SELF_HOSTED_HOSTS


def _missing_key_message(url: str, provider: str | None) -> str:
    """没配 key 时唯一会被看到的那段文字，所以它要能直接照着做。

    三条出路刻意按「从最不需要理解到最需要」排：先给一条完全不用配置的
    （scripted），因为第一次跑的人最需要的是先看到东西动起来。
    """
    from ..cli import PROVIDERS  # 延迟导入：这里只在出错路径上用一次

    env = None
    if provider and provider in PROVIDERS:
        env = PROVIDERS[provider].get("key_env")
    who = provider or url
    env_line = (
        f"  · 环境变量 {env}=sk-...（或写进 .env，从 .env.example 复制）"
        if env
        else "  · 把对应供应商的 key 写进 .env（从 .env.example 复制）"
    )
    return (
        f"没有配置 {who} 的 API key，而这是一个真实供应商端点（{url}）。三选一：\n"
        f"  · 先跑 `cowork demo` —— 用脚本后端，不需要任何 key，"
        f"完整链路照样走一遍\n"
        f"{env_line}\n"
        f"  · 跑 `cowork serve`，在设置页里填（不用碰文件）\n"
        f"\n"
        f"（自托管代理如 LiteLLM 不需要 key —— 那种情况把 COWORK_LLM_BASE_URL "
        f"指到 localhost 即可）"
    )


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
        decompose_system: str | None = None,
        cache_key_supported: bool = False,
        effort_profile: str | None = None,
        architect_effort: str = "high",
        subagent_effort: str = "medium",
        cheap_effort: str = "off",
        provider: str | None = None,
    ) -> None:
        import openai  # 延迟导入：跑脚本后端时不需要

        url = base_url or os.environ.get("COWORK_LLM_BASE_URL") or PRESETS["litellm"]
        key = (
            api_key
            or os.environ.get("COWORK_LLM_API_KEY")
            or os.environ.get("DEEPSEEK_API_KEY")
            or os.environ.get("MOONSHOT_API_KEY")
            or os.environ.get("OPENAI_API_KEY")
        )
        if not key:
            # 自托管代理（LiteLLM 之类）常常不校验 key，占位符是合理的；
            # 打**真实供应商**端点却没有 key 则是配置没做完 —— 必须当场说清楚。
            # 曾经这里一律回退 "placeholder"，于是「你还没配 key」变成了跑到
            # 一半的 401 `Your api key: ****lder is invalid`，读起来像账号出问题。
            if not _is_self_hosted(url):
                raise MissingApiKey(_missing_key_message(url, provider))
            key = "placeholder"
        self.client = openai.OpenAI(base_url=url, api_key=key, max_retries=max_retries)
        self.base_url = url
        # subagent_model 为 None 时用 TaskSpec.model —— 保留「不同模型干擅长的事」；
        # 设了就全局覆盖，方便单供应商下的联调。
        self.subagent_model = subagent_model
        self.architect_model = architect_model
        self.triage_model = triage_model or architect_model
        self.max_tokens = max_tokens
        self.repair_rounds = repair_rounds
        # 只有拆解提示词开了这个口子：它是 M7 唯一一条「靠提示词而不是靠确定性
        # 规则」的质量来源，所以必须能被换掉做对照（§11.13）。其余提示词不给
        # 覆盖入口 —— 能换就会有人换，而没有对照组的提示词改动是没法验证的。
        self.decompose_system = decompose_system or DECOMPOSE_SYSTEM
        self.cache_key_supported = cache_key_supported
        # 推理挡位（§10.3.2）。三档分工对应三个角色：架构师要想清楚、Subagent
        # 干活、分诊/探查/摘要只判方向。**只有声明过挡位能力的供应商才会真的下发**，
        # 见 llm/effort.py —— 不认识的字段发出去要么 400、要么被静默忽略。
        self.effort_profile = effort_profile
        self.architect_effort = architect_effort
        self.subagent_effort = subagent_effort
        self.cheap_effort = cheap_effort
        # 最近一次解析的说明（「没有 medium 档，取整到 high」这类），CLI 会打出来
        self.effort_notes: list[str] = []
        # 缓存记账（§11.14）。**先有度量再谈优化** —— 在这之前
        # 「命中率高不高」在这个项目里是没有答案的，任何调整都是猜。
        self.cache_stats = CacheStats()
        # 实例级覆盖类属性：跨模型对照里两个 arm 都是这个类，只报类名等于没记
        # （§12 M7 7.2 的记录要能区分是谁复核的）。用架构师模型 —— 复核走的就是它。
        self.name = f"openai-compat:{self.architect_model}"

    # ------------------------------------------------------------------ #

    def _call(
        self, *, model: str, system: str, user: str, schema: dict[str, Any],
        max_tokens: int | None = None, effort: str | None = None,
    ) -> tuple[dict[str, Any], int]:
        import openai

        # 拼装顺序就是缓存命中率本身：**静态在前、可变在后**。
        # 各家的前缀缓存都只认「从头开始逐字节相同」的那一段，所以角色提示词 +
        # 输出约束 + schema（同一种调用里一字不变）必须整体排在最前面，
        # 随每次调用变化的 user 内容排在最后。把 schema 挪到 user 里、或者在
        # system 里插一个时间戳/任务 id，都会把命中率直接打到 0（§11.14）。
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
            "max_tokens": max_tokens or self.max_tokens,
            "messages": messages,
        }
        if effort is not None:
            resolved = resolve(self.effort_profile, effort)
            kwargs.update(resolved.body)
            if resolved.extra_body:
                # OpenAI SDK 的非标字段走 extra_body，它会原样并进请求体
                kwargs["extra_body"] = {**kwargs.get("extra_body", {}),
                                        **resolved.extra_body}
            if resolved.note and resolved.note not in self.effort_notes:
                self.effort_notes.append(resolved.note)

        if self.cache_key_supported:
            # 把同一种调用路由到同一个缓存分片。key **从 sys_prompt 自己算**：
            # 它标识的正好就是那段可缓存前缀，改了提示词 key 自动跟着变，
            # 不会出现「提示词改了、key 还指着旧分片」。只对声明支持的一家发 ——
            # 不认识的字段在严格端点上是 400。
            digest = hashlib.sha256(sys_prompt.encode("utf-8")).hexdigest()[:16]
            kwargs["prompt_cache_key"] = f"cowork-{digest}"
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
            self.cache_stats.observe(usage)
            if usage:
                tokens += (usage.prompt_tokens or 0) + (usage.completion_tokens or 0)

            text = (resp.choices[0].message.content or "").strip()

            # 截断必须单独认出来，而且**要原样重试而不是带着残文去修复**。
            # 推理型模型的 thinking 计在同一个额度里，且用量方差极大 —— 同一个
            # 拆解请求实测 reasoning 2093 ~ 12000 token，其中一次把 12000 全烧在
            # 思考上、正文 0 字符（§11.12）。这种失败是掷骰子，不是格式问题：
            # 把残文回灌只会让模型接着写它的半截 JSON，重掷一次反而更可能成。
            # 报错也必须说清是截断 —— 截断的 JSON 报出来是「不是合法 JSON」，
            # 照着那个错误去查提示词会查错方向。
            if getattr(resp.choices[0], "finish_reason", None) == "length":
                errors = [
                    f"输出被 max_tokens={kwargs['max_tokens']} 截断"
                    f"（正文 {len(text)} 字符，模型的 thinking 也计在这个额度里）"
                ]
                if attempt >= self.repair_rounds:
                    break
                kwargs["messages"] = messages  # 原样重掷，不带残文
                continue

            data, errors = _parse_and_validate(text, schema)
            if data is not None:
                return data, tokens

            if attempt >= self.repair_rounds:
                break
            # 带着具体错误再问一轮。
            # **空回复不能原样回灌**：OpenAI 兼容端点会拒绝 content 为空的 assistant 消息
            # （"must not be empty"，400），于是修复轮的请求本身就非法 —— 一次可恢复的
            # 解析失败被升级成硬失败，重试机会白白吃掉。M7 7.2 跑批里 120 次有 2 次栽在这。
            echo = (
                [{"role": "assistant", "content": text[:4000]}] if text.strip() else []
            )
            complaint = (
                "上面的输出不合格：\n" + "\n".join(errors)
                if echo
                else "上一轮没有返回任何内容。"
            )
            kwargs["messages"] = [
                *messages,
                *echo,
                {"role": "user", "content": complaint + "\n只重新输出修正后的 JSON 对象。"},
            ]

        raise ModelCallFailed(
            f"模型连续 {self.repair_rounds + 1} 轮未产出合规 JSON: " + "; ".join(errors)
        )

    # -- Subagent ---------------------------------------------------------- #

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        data, tokens = self._call(
            model=self.subagent_model or ctx.task_spec.model,
            system=with_extra(SUBAGENT_SYSTEM, "subagent"),
            user=_render_subagent_context(ctx),
            schema=ACTION_SCHEMA,
            effort=self.subagent_effort,
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
            effort=self.cheap_effort,
        )
        return (
            [Triage(v["signal_id"], v["verdict"], v.get("reason", "")) for v in data["verdicts"]],
            tokens,
        )

    def decide_interrupt(
        self,
        spec: TaskSpec,
        signals: list[Signal],
        ctx: AgentContext,
        *,
        history: list[dict] | None = None,
        review_feedback: list[str] | None = None,
    ) -> tuple[ArchitectVerdict, int]:
        user = _render_architect_context(spec, signals, ctx, history, review_feedback)
        data, tokens = self._call(
            model=self.architect_model,
            system=with_extra(ARCHITECT_SYSTEM, "architect"),
            user=user,
            schema=VERDICT_SCHEMA,
            effort=self.architect_effort,
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
            effort=self.cheap_effort,
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
            effort=self.architect_effort,
        )
        return data["passed"], data["reason"], tokens

    def review_decomposition(
        self, root_goal: str, specs: list[TaskSpec]
    ) -> tuple[bool, list[str], int]:
        data, tokens = self._call(
            model=self.architect_model,
            system=with_extra(REVIEW_SYSTEM, "reviewer"),
            user=_render_review_context(root_goal, specs),
            schema=REVIEW_SCHEMA,
            max_tokens=REVIEW_MAX_TOKENS,
            effort=self.architect_effort,
        )
        return data["sufficient"], list(data["missing"]), tokens

    def review_spec_change(
        self, spec: TaskSpec, signals: list[Signal], verdict: ArchitectVerdict
    ) -> tuple[bool, list[str], int]:
        data, tokens = self._call(
            model=self.architect_model,
            system=with_extra(SPEC_REVIEW_SYSTEM, "reviewer"),
            user=_render_spec_review_context(spec, signals, verdict),
            schema=SPEC_REVIEW_SCHEMA,
            max_tokens=SPEC_REVIEW_MAX_TOKENS,
            effort=self.architect_effort,
        )
        return data["ok"], list(data["findings"]), tokens

    def decompose(
        self, root_goal: str, *, feedback: list[str] | None = None,
        existing: str | None = None,
    ) -> tuple[list[SubtaskDraft], int]:
        data, tokens = self._call(
            model=self.architect_model,
            system=with_extra(self.decompose_system, "architect"),
            user=_render_decompose_context(root_goal, feedback, existing),
            schema=DECOMPOSE_SCHEMA,
            # 拆解是这里最长的一次输出：6 个子任务 × 若干条带命令的验收标准，
            # 加上推理型模型自己要烧掉的那部分，4096 实测不够（见 §11.12）。
            max_tokens=DECOMPOSE_MAX_TOKENS,
            effort=self.architect_effort,
        )
        return _parse_drafts(data), tokens

    def profile_tasks(self, specs: list[TaskSpec]) -> tuple[list[TaskProfile], int]:
        # 走廉价档 + 廉价模型：描述任务而已，不是做决策
        data, tokens = self._call(
            model=self.triage_model,
            system=PROFILE_SYSTEM,
            user=_render_profile_context(specs),
            schema=PROFILE_SCHEMA,
            effort=self.cheap_effort,
        )
        return _parse_profiles(data, specs), tokens

    def probe(
        self, spec: TaskSpec, ctx: AgentContext, excerpts: dict[str, str]
    ) -> tuple[bool, str, int]:
        # 用 triage_model：探查只判方向，不做完整推理，成本必须压住（§3.2.1）
        data, tokens = self._call(
            model=self.triage_model,
            system=PROBE_SYSTEM,
            user=_render_probe_context(spec, ctx, excerpts),
            schema=PROBE_SCHEMA,
            effort=self.cheap_effort,
        )
        return data["on_track"], data["reason"], tokens


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

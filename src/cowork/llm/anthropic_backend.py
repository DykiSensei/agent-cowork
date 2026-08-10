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
from ..llm import ArchitectVerdict, CacheStats, SubtaskDraft, Triage
from ..llm.errors import ModelCallFailed, from_provider_error
from ..signals import SOFT_SIGNALS, SignalType
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
        "tool": {
            "type": "string",
            "enum": ["write_file", "read_file", "list_files", "run", ""],
        },
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
- tool_call + tool=list_files，需要 path（目录，用 "." 表示工作区根）。
  **想看工作区里有什么就用它，不要去 run 一个 ls** —— ls 不在 allowed_binaries 里，
  调它只会触发 SCOPE_VIOLATION。
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
- ABANDON      继续下去不可能成功，停下来等人

**先判断证据的性质，再选动作。这一步不能跳过：**

**证据具体、指向一个可修补的规格缺口 → MODIFY_TASK。**
「某个用例期望 X 实际得到 Y」这类信息本身就说明了缺什么。这是最常见的情形，
也是这个系统存在的意义：把失败信号变成更清楚的规格。**不要因为失败了就放弃。**

**证据具体，但问题在实现而不在规格 → CONTINUE 或 REASSIGN。**
规格没毛病，Subagent 没做对，再来一次即可。

**只有同时满足下面两条才选 ABANDON：**

1. 继续下去不可能成功 —— 证据为空（失败了却没留下任何可依据的信息）、
   验收标准自相矛盾、或者缺的东西在任务范围内根本拿不到（依赖不存在、权限不够）；
2. **且**改 TaskSpec 也解决不了 —— 你能想出的任何规格修改都只是猜测。

第 2 条是关键：能靠改规格推进就不要放弃。放弃意味着这个任务要占用人的时间，
而人的时间比 token 贵得多。反过来，**在没有依据的情况下反复改规格更糟** ——
那是在拿猜测覆盖原本清楚的要求。

如果上面给了你此前的裁决记录：同样的信号在你改过规格之后又原样出现了，
说明那次修改没有奏效，此时才轮到第 2 条成立。

complexity_score 是你对这次决策复杂度的自评（0.0~1.0）。诚实评估：
高分会触发升级给人。注意你**不知道自己不知道什么**，涉及不可逆操作、
反复中断、触及顶层任务意图时，即使你觉得简单也应给高分。
证据为空却要改规格，是最该给高分的情形。

MODIFY_TASK 时，new_goal 留空表示目标不变（只改验收标准/范围），
added_criteria 是要补充的验收标准。"""

TRIAGE_SYSTEM = """你在做廉价批量分诊。对每条软信号只输出 ignore 或 escalate。
escalate 只给「架构师必须介入才能推进」的信号。不要做完整推理。"""

PROBE_SYSTEM = """你在做中途探查（PROBE），不是验收。

这个任务没有客观判据可以让 Runtime 自动检查，所以只能靠你定期看一眼。
你看到的产出**本来就是不完整的**——它还在写。

只回答一个问题：**它在正确的方向上吗？**

判 off_track 的情形：跑题、违反明确写死的约束、产出结构明显不对、
内容与 goal 无关。**不要**因为「还没写完」「篇幅不够」「不够完善」判 off_track，
那是验收的事，不是探查的事。拿不准就判在轨——误报的代价是白打断一次。"""

DECOMPOSE_SYSTEM = """你在把一个目标拆成若干可以独立派发的子任务。

**先做一件事：把原始目标里的限定词逐个划出来**——产物、格式、边界情况、性能、
篇幅、兼容性、"必须/不得"这类约束。每一个限定词都要能指到某个子任务的某一条
验收标准。这是 §11.11 实测出来的方法：拆解出问题时，漏掉的几乎总是限定词，
而不是主干功能。主干谁都不会忘，限定词天天被忘。

验收标准的写法决定这个拆解有没有用：

- 写**行为**，不写存在性。「format_row(('a',1)) 返回 'a = 1'」是判据，
  「formatter.py 存在且能 import」不是 —— 后者随便写点什么都能通过。
- 能用一条命令判定就给 command（例如 ["python", "verify_x.py"]），
  Runtime 会自己跑它并在失败时产生硬信号；判不了就留空，交给人或模型判。
- **子任务之间的衔接也要有人验收**。每个部件各自正确、拼起来不工作，
  是这类拆解最常见的失败。

结构上的硬要求：

1. 每个子任务必须有 scope（它被允许写的文件），**两个子任务的 scope 不能相交**——
   相交会被调度器判定为冲突并强制串行，拆了等于没拆；
2. depends_on 只能引用本次拆解里的其它 id，不能有环；
3. **至少要有两个子任务能同时开跑**（即存在两个互不依赖的任务）。做不到就说明
   这个目标是顺序依赖的，那时候宁可只拆成 1 个子任务，也不要拆成一条链 ——
   顺序依赖强的任务用多 agent 最差会掉 70% 的效果。
4. 粒度：2~6 个子任务。拆到 10 个以上说明你在拆步骤，不是拆任务。

如果上面给了你**上一轮复核发现的缺口**，那是必须修掉的东西，不是参考意见。
针对每一条缺口，要么加子任务，要么加/改验收标准，别原样再交一遍。"""

REVIEW_SYSTEM = """你在复核一个任务拆解，用的方法是**验收标准反推**。

给你：一个原始目标，和拆解出来的若干子任务（每个带自己的验收标准）。

只回答一个问题：**假设所有子任务的验收标准都满足了，原始目标是不是就算完成了？**

不要评价拆解得好不好、粒度合不合适、能不能更优雅 —— 那些都是意见。
只找**客观的缺口**：原始目标里有、而所有子任务的验收标准合起来仍然覆盖不到的东西。

典型缺口：目标要求的某个产物没有任何子任务负责；子任务之间的衔接没有人验收；
目标里的某个限定词（性能、格式、兼容性）在所有验收标准里都找不到对应。

没有缺口就回答 sufficient=true、missing 留空数组。**不要为了显得有用而编缺口。**"""

# 复核要读完整份拆解再推理，输入长、thinking 也长。默认的 4096 实测会被吃满：
# kimi-k3 复核一份 4 子任务的拆解时正文 0 字符、全烧在 reasoning 上（§11.13）。
REVIEW_MAX_TOKENS = 8_000

REVIEW_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "sufficient": {"type": "boolean"},
        "missing": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["sufficient", "missing"],
    "additionalProperties": False,
}


# 拆解的输出比其它调用长一个量级（子任务 × 带命令的验收标准），而**推理型模型的
# thinking 也计在这个额度里**：deepseek-v4-flash 实测同一个目标 completion
# 2544（reasoning 2093）~ 9643（reasoning 8840），kimi-k3 6823（reasoning 5854）。
# 4096 必然截断，12000 也见过一次烧光在 reasoning 上、正文 0 字符（§11.12）。
DECOMPOSE_MAX_TOKENS = 16_000

DECOMPOSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "subtasks": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "goal": {"type": "string"},
                    "task_class": {
                        "type": "string",
                        "enum": ["CODE", "TOOL_CALL", "GENERATIVE"],
                    },
                    "scope": {"type": "array", "items": {"type": "string"}},
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                    "acceptance": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "id": {"type": "string"},
                                "description": {"type": "string"},
                                # 空数组 = 这条判不了命令，交给人或模型判
                                "command": {"type": "array", "items": {"type": "string"}},
                            },
                            "required": ["id", "description", "command"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["id", "goal", "task_class", "scope", "depends_on", "acceptance"],
                "additionalProperties": False,
            },
        },
    },
    "required": ["subtasks"],
    "additionalProperties": False,
}


def _render_decompose_context(root_goal: str, feedback: list[str] | None) -> str:
    parts = [f"# 原始目标\n{root_goal}"]
    if feedback:
        # 复核缺口放在最后：模型对上下文末尾的要求执行得更实在，而这一段是
        # 「必须修掉的东西」。同 _render_architect_context 里裁决历史的处理。
        parts.append(
            "# 上一轮复核发现的缺口（必须逐条修掉）\n"
            + "\n".join(f"- {x}" for x in feedback)
        )
    return "\n\n".join(parts)


def _parse_drafts(data: dict[str, Any]) -> list[SubtaskDraft]:
    """把模型返回的拆解 JSON 变成 SubtaskDraft。

    失败抛 ModelCallFailed，理由同 `_parse_action`（§11.3c）：schema 校验通过不
    等于语义有效 —— acceptance 可以是空数组、id 可以是空串，两者都会在下游
    以更难懂的方式炸掉。TaskSpec 的硬约束在这里先挡一道。
    """
    drafts: list[SubtaskDraft] = []
    seen: set[str] = set()
    for raw in data["subtasks"]:
        tid = (raw["id"] or "").strip()
        if not tid:
            raise ModelCallFailed("拆解里有子任务没有 id")
        if tid in seen:
            raise ModelCallFailed(f"拆解里 id 重复: {tid!r}")
        seen.add(tid)
        if not (raw["goal"] or "").strip():
            raise ModelCallFailed(f"子任务 {tid} 的 goal 为空")
        if not raw["acceptance"]:
            raise ModelCallFailed(f"子任务 {tid} 没有验收标准（§4.1 硬约束）")
        drafts.append(
            SubtaskDraft(
                id=tid,
                goal=raw["goal"],
                acceptance=[
                    {
                        "id": c["id"] or f"c{i}",
                        "description": c["description"],
                        "command": list(c.get("command") or []) or None,
                    }
                    for i, c in enumerate(raw["acceptance"], start=1)
                ],
                scope=list(raw["scope"]),
                depends_on=list(raw["depends_on"]),
                task_class=raw["task_class"],
            )
        )
    if not drafts:
        raise ModelCallFailed("拆解产出为空")
    return drafts


def _render_review_context(root_goal: str, specs: list[TaskSpec]) -> str:
    parts = [f"# 原始目标\n{root_goal}", "# 拆解出的子任务"]
    for s in specs:
        deps = f"（依赖 {', '.join(s.depends_on)}）" if s.depends_on else ""
        criteria = "\n".join(f"    - {c.id}: {c.description}" for c in s.acceptance)
        parts.append(f"## {s.id} {deps}\n  目标：{s.goal}\n  验收标准：\n{criteria}")
    return "\n\n".join(parts)


PROBE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "on_track": {"type": "boolean"},
        "reason": {"type": "string"},
    },
    "required": ["on_track", "reason"],
    "additionalProperties": False,
}


def _render_architect_context(
    spec: TaskSpec,
    signals: list[Signal],
    ctx: AgentContext,
    history: list[dict] | None = None,
) -> str:
    """架构师的中断决策上下文。

    `# 你此前对这个任务的裁决` 这一段是 M5a 加的。没有它，架构师每次都在
    「第一次见到这个问题」的状态下决策 —— M2 实测里 `e1_silent_failure` 五次运行
    全是 CONTINUE / REASSIGN 轮流试、一次 ABANDON 都没有（§11.9b）。
    它不保证架构师会做对，只是让它**有机会**发现自己在原地打转。
    """
    evidence = "\n\n".join(
        f"[{s.type.value}] payload={json.dumps(s.payload, ensure_ascii=False)}\n"
        f"证据:\n{(s.raw_evidence or '') [:4000] or '（空 —— 这次失败没有留下任何证据）'}"
        for s in signals
    )
    parts = [
        f"TaskSpec:\n{json.dumps(spec.to_dict(), ensure_ascii=False, indent=2)}",
        f"触发信号:\n{evidence}",
    ]
    if history:
        lines = []
        for i, h in enumerate(history, start=1):
            lines.append(
                f"{i}. 信号 {'/'.join(h['signals'])} -> 你选了 {h['action']}：{h['rationale']}"
            )
        parts.append(
            "你此前对这个任务的裁决（第 "
            + str(len(history) + 1)
            + " 次中断）:\n"
            + "\n".join(lines)
            + "\n\n如果同样的信号又出现了，说明上一次的裁决没有奏效。**不要重复它**。"
        )
    parts.append("已产出:\n" + ("\n".join(f"- {a.content_ref}" for a in ctx.produced) or "（无）"))
    return "\n\n".join(parts)


def _render_probe_context(spec: TaskSpec, ctx: AgentContext, excerpts: dict[str, str]) -> str:
    parts = [
        f"# 目标\n{spec.goal}",
        "# 约束（验收标准，供判断方向用，不要拿来判完成度）\n"
        + "\n".join(f"- {c.id}: {c.description}" for c in spec.acceptance),
    ]
    if excerpts:
        parts.append(
            "# 当前产出\n"
            + "\n\n".join(f"## {path}\n{text}" for path, text in excerpts.items())
        )
    else:
        parts.append("# 当前产出\n（还没有任何产出）")
    return "\n\n".join(parts)


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
        self.cache_stats = CacheStats()

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
        # Anthropic 的提示词缓存是**显式**的：不打 cache_control 断点就一次都不命中，
        # 这和 OpenAI 系「够长就自动缓存」不是一回事（§11.14）。断点打在 system 上 ——
        # 角色提示词在同一种调用里一字不变，而 user 每次都不同，缓存的边界正好在这。
        # 断点之前的内容才进缓存，所以 system 必须是完整的一块，不能把可变内容拼进去。
        kwargs: dict[str, Any] = {
            "model": model,
            "max_tokens": self.max_tokens,
            "system": [
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},
                }
            ],
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
        # Anthropic 把缓存读写单独计在两个字段里，且**它们不含在 input_tokens 里**，
        # 所以要显式加回去，否则打开缓存之后账面 token 会凭空变少（§11.14）。
        cache_read = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
        cache_write = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
        tokens += cache_read + cache_write
        self.cache_stats.calls += 1
        self.cache_stats.calls_with_usage += 1
        self.cache_stats.prompt_tokens += resp.usage.input_tokens + cache_read + cache_write
        self.cache_stats.cached_tokens += cache_read
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
        self,
        spec: TaskSpec,
        signals: list[Signal],
        ctx: AgentContext,
        *,
        history: list[dict] | None = None,
    ) -> tuple[ArchitectVerdict, int]:
        user = _render_architect_context(spec, signals, ctx, history)
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

    def review_decomposition(
        self, root_goal: str, specs: list[TaskSpec]
    ) -> tuple[bool, list[str], int]:
        # 用架构师主模型：这是「找出自己可能漏掉的东西」，属于需要推理的判断，
        # 不是分诊那种廉价过滤。它只在拆解时跑一次，成本可接受（§12 M5 的候选方案表）。
        data, tokens = self._call(
            model=self.architect_model,
            system=REVIEW_SYSTEM,
            user=_render_review_context(root_goal, specs),
            schema=REVIEW_SCHEMA,
        )
        return data["sufficient"], list(data["missing"]), tokens

    def decompose(
        self, root_goal: str, *, feedback: list[str] | None = None
    ) -> tuple[list[SubtaskDraft], int]:
        # 用架构师主模型：拆解是这个系统里最有杠杆的一次判断，拆错了后面全白干。
        data, tokens = self._call(
            model=self.architect_model,
            system=DECOMPOSE_SYSTEM,
            user=_render_decompose_context(root_goal, feedback),
            schema=DECOMPOSE_SCHEMA,
        )
        return _parse_drafts(data), tokens

    def probe(
        self, spec: TaskSpec, ctx: AgentContext, excerpts: dict[str, str]
    ) -> tuple[bool, str, int]:
        # 探查用便宜的模型：它只判方向，不做完整推理（同 §3.4 分诊的理由）。
        # PROBE 本来就是拿 token 换观测能力，单次成本必须压住。
        data, tokens = self._call(
            model=self.triage_model,
            system=PROBE_SYSTEM,
            user=_render_probe_context(spec, ctx, excerpts),
            schema=PROBE_SCHEMA,
            thinking=False,
        )
        return data["on_track"], data["reason"], tokens


# --------------------------------------------------------------------------- #


def _parse_action(d: dict[str, Any]) -> AgentAction:
    """把模型返回的动作 JSON 变成 AgentAction。

    失败时抛 **ModelCallFailed 而不是 ValueError**，理由同 §11.3c：
    schema 校验通过不等于语义有效 —— ACTION_SCHEMA 允许 tool / signal_type 取空串
    （「不适用」的表示法），模型却会在 kind=tool_call 时把 tool 留空。M2 跑批里
    75 次运行有 3 次因此崩掉整个 run（§11.6b）。解析失败必须变成硬信号交给架构师，
    和模型调用失败走同一条路。
    """
    kind = d["kind"]
    if kind == "tool_call":
        tool = d["tool"]
        if tool == "write_file":
            args = {"path": d["path"], "content": d["content"]}
        elif tool == "read_file":
            args = {"path": d["path"]}
        elif tool == "list_files":
            args = {"path": d["path"] or "."}
        elif tool == "run":
            args = {"command": list(d["command"])}
        else:
            raise ModelCallFailed(f"kind=tool_call 但 tool 无效: {tool!r}")
        return ToolCall(name=tool, args=args, thought=d.get("thought", ""))
    if kind == "finish":
        try:
            output = json.loads(d["output_json"] or "{}")
        except json.JSONDecodeError:
            output = {}
        return Finish(output=output, summary=d.get("summary", ""), thought=d.get("thought", ""))
    if kind == "soft_signal":
        raw = d["signal_type"]
        try:
            signal_type = SignalType(raw)
        except ValueError:
            raise ModelCallFailed(f"kind=soft_signal 但 signal_type 无效: {raw!r}") from None
        if signal_type not in SOFT_SIGNALS:
            raise ModelCallFailed(f"soft_signal 不能用硬信号类型: {raw!r}")
        return SoftSignalAction(signal_type=signal_type.value, detail=d.get("detail", ""))
    raise ModelCallFailed(f"未知动作 kind: {kind!r}")


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

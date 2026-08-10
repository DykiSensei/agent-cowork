"""模型后端协议。

三个角色各自需要模型，但需求不同：
  - Subagent  决定下一个 step
  - 架构师主模型  中断决策 / 验收
  - 廉价评估模型  软信号批量分诊（§3.4）
后端把三者收在一个协议里，实现方决定用哪个模型跑哪个角色
（TaskSpec.model 承载「不同模型干擅长的事」）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from ..actions import AgentAction
from ..types import AgentContext, Signal, TaskSpec


@dataclass
class Triage:
    signal_id: str
    verdict: Literal["ignore", "escalate"]
    reason: str = ""


@dataclass
class CacheStats:
    """提示词缓存的记账（§11.14）。

    各家报的字段名不一样，但语义都是「这次请求的输入里有多少 token 命中了缓存」。
    实测三种形状（`usage.model_dump()` 原样抓的）：

        OpenAI 系   usage.prompt_tokens_details.cached_tokens
        DeepSeek    上面那个 + usage.prompt_cache_hit_tokens（两个都给，值相同）
        Moonshot    上面那个 + **顶层 usage.cached_tokens**；而且**第一次调用时
                    prompt_tokens_details 整个是 null**，第二次才出现

    所以三个位置挨个试，谁有读谁 —— 只认一个字段就会把「这家换了个名字」
    读成「一次都没命中」。

    `calls_with_usage` 单独记的理由同上：**「这家不报」和「没命中」在账面上
    长得一样**，但结论完全相反，不能混。
    """

    calls: int = 0
    calls_with_usage: int = 0
    prompt_tokens: int = 0
    cached_tokens: int = 0

    @property
    def hit_rate(self) -> float | None:
        if not self.prompt_tokens:
            return None
        return self.cached_tokens / self.prompt_tokens

    def observe(self, usage) -> None:
        self.calls += 1
        if usage is None:
            return
        prompt = getattr(usage, "prompt_tokens", 0) or 0
        # details 可能整个是 None（Moonshot 第一次调用就是），所以 getattr 要挡住
        details = getattr(usage, "prompt_tokens_details", None)
        cached = getattr(details, "cached_tokens", None) if details is not None else None
        if cached is None:
            cached = getattr(usage, "prompt_cache_hit_tokens", None)
        if cached is None:
            cached = getattr(usage, "cached_tokens", None)
        if prompt:
            self.calls_with_usage += 1
            self.prompt_tokens += prompt
            self.cached_tokens += int(cached or 0)

    def to_dict(self) -> dict[str, Any]:
        return {
            "calls": self.calls,
            "calls_with_usage": self.calls_with_usage,
            "prompt_tokens": self.prompt_tokens,
            "cached_tokens": self.cached_tokens,
            "hit_rate": round(self.hit_rate, 4) if self.hit_rate is not None else None,
        }


@dataclass
class SubtaskDraft:
    """生成者产出的一个子任务（§12 M7 7.3）。

    **这不是 TaskSpec**，是它的一部分：只有模型有权决定的那些字段。
    sandbox / tools / token 上限由 `SpecTemplate` 填，模型碰不到 ——
    让被隔离方给自己配隔离边界是没有意义的。
    """

    id: str
    goal: str
    acceptance: list[dict]          # {id, description, command?}
    scope: list[str]
    depends_on: list[str] = field(default_factory=list)
    task_class: str = "CODE"


@dataclass
class TaskProfile:
    """一个子任务的「这活儿是什么性质」（§10.3.3）。

    **架构师在这里是顾问，不是决策者** —— 它只描述任务特点，选哪家由人定
    （`HumanGate.assign_models`）。和复核者的关系完全一样：产出 findings，人拍板。

    刻意不含「建议用哪家」：模型不认识你账号里有哪些 key、也不知道你的成本约束，
    让它推荐等于让它猜，而猜出来的东西摆在人面前会变成默认答案。
    """

    task_id: str
    kind: str            # 一个短标签：backend / frontend / docs / test / data ...
    summary: str         # 一句话说清这个子任务在干什么
    demands: list[str]   # 对模型的要求：长上下文 / 严格格式 / 中文写作 / 算法推理…


@dataclass
class ArchitectVerdict:
    """架构师对一次中断的裁决。resume_mode 留空则由 §6.2 规则推导。"""

    action: str  # CONTINUE | MODIFY_TASK | ABANDON | REASSIGN
    rationale: str
    complexity_score: float = 0.0
    spec_changes: dict[str, Any] = field(default_factory=dict)


class Backend(Protocol):
    name: str

    def next_step(self, ctx: AgentContext) -> tuple[AgentAction, int]:
        """Subagent 视角：给定上下文，决定下一步。返回 (动作, token 消耗)。"""
        ...

    def triage(self, signals: list[Signal]) -> tuple[list[Triage], int]:
        """廉价批量评估（§3.4）：只输出 ignore | escalate。"""
        ...

    def decide_interrupt(
        self,
        spec: TaskSpec,
        signals: list[Signal],
        ctx: AgentContext,
        *,
        history: list[dict] | None = None,
        review_feedback: list[str] | None = None,
    ) -> tuple[ArchitectVerdict, int]:
        """架构师主模型：完整中断决策。

        history 是**本任务此前的裁决记录**（每条含信号类型、动作、理由）。
        没有它的话架构师每次都在「第一次见到这个问题」的状态下决策 ——
        M2 实测里 CONTINUE → CONTINUE → CONTINUE 的循环就是这么来的（§11.9b）。

        review_feedback 是**写入侧复核报出的问题**，重做这一轮时喂回去（§12 M8）。
        和 `decompose(feedback=...)` 是同一个设计、同一个理由：不喂回去的话
        架构师会在「第一次看到这个中断」的状态下重做，复核意见等于没提。
        **它与 history 分开传**：history 参与「同一指纹连续出现」的计数，
        而被驳回的草稿不是一次真的裁决，混进去会把那个计数搞脏。
        """
        ...

    def summarize(self, ctx: AgentContext) -> tuple[str, int]:
        """REBASE 第 2 步：对 produced 生成压缩摘要（本身消耗 token，计入预算）。"""
        ...

    def verify(self, spec: TaskSpec, ctx: AgentContext) -> tuple[bool, str, int]:
        """架构师验收非机器可检的验收标准。"""
        ...

    def review_decomposition(
        self, root_goal: str, specs: list[TaskSpec]
    ) -> tuple[bool, list[str], int]:
        """拆解复核（§12 M5b）：**验收标准反推**。

        问一个问题：满足这些子任务的全部验收标准，是不是就等于完成了原始目标？
        这个方向是刻意的 —— 正向问「这个拆解好不好」得到的是复述，
        反推问「按这些标准验收完，还缺什么」才逼出遗漏。

        返回 (是否充分, 缺失项列表, token)。复核者可以换独立供应商
        （M7 7.1 的 `Architect(reviewer_backend=...)`），实测见 §11.11。
        """
        ...

    def review_spec_change(
        self,
        spec: TaskSpec,
        signals: list[Signal],
        verdict: ArchitectVerdict,
    ) -> tuple[bool, list[str], int]:
        """**写入侧复核**（§12 M8）：这次改 TaskSpec 改得对不对。

        和 `review_decomposition` 是同一个角色在另一层：复核者**没有写权**，
        只产出 findings，改不了 spec —— 写权仍然只在 `decide()/_apply_changes()`
        那一条路上（§2.3）。给它写权 = 两个写入点 = 不变量破了。

        与确定性升级判据**分工明确、不重叠**：`escalation.py` 判的是**上下文**
        （谁改的、改过几次、烧了多少钱、有没有越界信号），它从不看改动的内容；
        这里判的正是**内容本身**。所以这不是把确定性规则再实现一遍。

        问的问题按 M5b 那条经验反着来 —— 不问「这个改动好不好」（会得到复述），
        而问「按改完之后的规格验收，失败证据指的那个问题会被挡住吗；
        以及这次改动有没有把原始目标改松」。

        返回 (是否通过, 缺口列表, token)。
        """
        ...

    def decompose(
        self, root_goal: str, *, feedback: list[str] | None = None
    ) -> tuple[list[SubtaskDraft], int]:
        """把一个自然语言目标拆成子任务（§12 M7 7.3）。

        `feedback` 是**上一轮复核报出的缺口**，重生成时喂回去。它和
        `decide_interrupt(history=...)` 是同一个设计：没有它，生成者每一轮都在
        「第一次看到这个目标」的状态下重拆，复核意见等于没提（§11.9b 的教训）。

        模型只填**它有权决定的字段**：goal / acceptance / scope / depends_on /
        task_class。sandbox、tools、各类上限由调用方的模板决定 ——
        让模型给自己配沙箱和工具白名单，等于把隔离边界交给被隔离方。
        """
        ...

    def profile_tasks(self, specs: list[TaskSpec]) -> tuple[list[TaskProfile], int]:
        """一次调用，描述整批子任务各自的性质（§10.3.3）。

        **一次，不是每个任务一次**：拆解已经定了，这一步只是给人做选择题时的
        参考信息，不值得为它花 N 次调用。而且放在一次调用里，模型能看到彼此的
        对比（「这个偏前端、那个偏算法」），分类反而更稳。

        返回的是**描述**，不是建议。选哪家由人定 —— 见 `TaskProfile` 的说明。
        """
        ...

    def probe(
        self, spec: TaskSpec, ctx: AgentContext, excerpts: dict[str, str]
    ) -> tuple[bool, str, int]:
        """PROBE 中间探查（§3.2.1）：现在这个产出还在轨道上吗。

        和 verify 是两个问题，不能合并：verify 问「完成了吗」，probe 问
        「方向对吗」。中途产出**本来就不完整**，拿完成度去判它必然误报。

        excerpts 是 {产出路径: 内容片段}。**由 Runtime 的 sandbox 读出来再传进来**——
        架构师没有也不该有文件系统访问权，读文件是确定性操作，归 Runtime。
        返回 (是否在轨, 理由, token)。
        """
        ...


from .scripted import ScriptedBackend  # noqa: E402

__all__ = ["Backend", "Triage", "ArchitectVerdict", "SubtaskDraft", "TaskProfile",
           "CacheStats", "ScriptedBackend"]

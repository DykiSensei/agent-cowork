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
    ) -> tuple[ArchitectVerdict, int]:
        """架构师主模型：完整中断决策。

        history 是**本任务此前的裁决记录**（每条含信号类型、动作、理由）。
        没有它的话架构师每次都在「第一次见到这个问题」的状态下决策 ——
        M2 实测里 CONTINUE → CONTINUE → CONTINUE 的循环就是这么来的（§11.9b）。
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

__all__ = ["Backend", "Triage", "ArchitectVerdict", "SubtaskDraft", "ScriptedBackend"]

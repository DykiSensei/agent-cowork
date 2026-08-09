"""§6：恢复模式的选择规则与 REBASE 的上下文处理。"""

import unittest

from cowork.llm.scripted import ScriptedBackend
from cowork.resume import apply_resume, choose_resume_mode, rebase, restart
from cowork.types import (
    AgentContext,
    Artifact,
    Criterion,
    ResumeMode,
    SandboxProfile,
    TaskClass,
    TaskSpec,
)


def make_spec(**kw) -> TaskSpec:
    base = dict(
        goal="实现 foo",
        acceptance=[Criterion("c1", "能跑")],
        task_class=TaskClass.CODE,
        sandbox=SandboxProfile(workspace="."),
        scope=["foo.py"],
    )
    base.update(kw)
    return TaskSpec(**base)


class TestChooseResumeMode(unittest.TestCase):
    def test_revision_unchanged_resume(self):
        s = make_spec()
        self.assertIs(choose_resume_mode(s, s), ResumeMode.RESUME)

    def test_only_acceptance_changed_rebase(self):
        s = make_spec()
        new = s.bump(acceptance=[*s.acceptance, Criterion("c2", "还要快")])
        self.assertIs(choose_resume_mode(s, new), ResumeMode.REBASE)

    def test_goal_changed_but_scope_overlaps_rebase(self):
        s = make_spec()
        new = s.bump(goal="改成实现 bar")
        self.assertIs(choose_resume_mode(s, new), ResumeMode.REBASE)

    def test_goal_and_scope_both_changed_restart(self):
        s = make_spec()
        new = s.bump(goal="做完全不同的事", scope=["other.py"])
        self.assertIs(choose_resume_mode(s, new), ResumeMode.RESTART)


class TestRebaseContext(unittest.TestCase):
    def setUp(self):
        self.spec = make_spec()
        self.ctx = AgentContext(
            task_spec=self.spec,
            injected=[Artifact(self.spec.id, "file", "readme.md", "背景")],
            produced=[Artifact(self.spec.id, "file", "foo.py", "第一版")],
            reasoning_trace=[
                {"role": "assistant", "step": 1, "action": {"kind": "tool_call"}},
                {"role": "tool", "step": 1, "ok": True},
            ],
        )
        self.backend = ScriptedBackend({})

    def test_rebase_drops_trace_keeps_produced(self):
        new_spec = self.spec.bump(acceptance=[*self.spec.acceptance, Criterion("c2", "x")])
        new_ctx, tokens = rebase(self.ctx, new_spec, self.backend)

        self.assertEqual(new_ctx.reasoning_trace, [], "旧目标的推理痕迹必须清空")
        self.assertEqual(
            [a.content_ref for a in new_ctx.produced], ["foo.py"], "产出必须保留"
        )
        self.assertEqual(new_ctx.task_spec.revision, 2)
        self.assertGreater(tokens, 0, "摘要压缩本身消耗 token，要计入预算")

        kinds = [a.kind for a in new_ctx.injected]
        self.assertEqual(kinds, ["file", "summary"], "摘要作为只读上下文追加在原 injected 后")

    def test_original_context_not_mutated(self):
        new_spec = self.spec.bump()
        rebase(self.ctx, new_spec, self.backend)
        self.assertEqual(len(self.ctx.reasoning_trace), 2, "REBASE 不应就地改旧 context")

    def test_restart_drops_everything_but_reference(self):
        new_spec = self.spec.bump(goal="换方向", scope=["bar.py"])
        new_ctx = restart(self.ctx, new_spec)
        self.assertEqual(new_ctx.produced, [])
        self.assertEqual(new_ctx.reasoning_trace, [])
        self.assertEqual([a.content_ref for a in new_ctx.injected], ["foo.py"])

    def test_resume_keeps_everything(self):
        new_ctx, tokens = apply_resume(ResumeMode.RESUME, self.ctx, self.spec, self.backend)
        self.assertEqual(len(new_ctx.reasoning_trace), 2)
        self.assertEqual(tokens, 0)


if __name__ == "__main__":
    unittest.main()

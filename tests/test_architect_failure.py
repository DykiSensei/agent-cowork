"""架构师侧的模型调用失败 —— 每一处都必须挂起等人，不能抛穿 run()。

§10.1 的地基是「控制流自己持有」，而 `llm/errors.py` 那条纪律是它的另一半：
**模型调用失败要变成信号或终局，不能变成异常**。原来只有 `Architect.decide()`
被接住，另外三处（验收 / 探查 / 软信号分诊）会一路抛出 `Orchestrator.run()`。

后果不只是「崩了」——服务层把执行放在 daemon 线程里、异常只落一行日志，于是：

  库里的状态停在 RUNNING
    → `POST /tasks/{id}/cancel` 回 409（它已经不在活任务注册表里）
    → `POST /tasks/{id}/ruling` 回 409（它不是 AWAITING_HUMAN）
    → 这条线程从界面上再也动不了

所以这组测试断言的是**两件事**：run() 不抛，以及库里留下的是可继续的终局。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork.actions import Finish, SoftSignalAction
from cowork.llm.errors import BudgetExceeded, ModelCallFailed
from cowork.llm.scripted import ScriptedBackend
from cowork.orchestrator import Orchestrator
from cowork.store import SqliteStore
from cowork.types import (
    Criterion,
    SandboxProfile,
    SilencePolicy,
    TaskClass,
    TaskSpec,
    TaskStatus,
)


class ArchitectFailureFixture(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-archfail-"))
        self.store = SqliteStore()

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def spec(self, **kw) -> TaskSpec:
        base = dict(
            id="t_archfail",
            parent_id="p_root",
            goal="写一个文件",
            # 非机器可检 = 收尾时必然要走一次架构师验收
            acceptance=[Criterion("c1", "内容读起来合理")],
            task_class=TaskClass.CODE,
            sandbox=SandboxProfile(workspace=str(self.ws), allowed_binaries=("python",)),
            scope=["out.txt"],
        )
        base.update(kw)
        return TaskSpec(**base)

    def run_with(self, backend, spec=None, **kw) -> "object":
        orch = Orchestrator(
            spec or self.spec(), backend=backend, store=self.store,
            log=lambda _m: None, **kw,
        )
        return orch.run(max_cycles=2)

    def assert_suspended(self, result, task_id="t_archfail"):
        """挂起 + 库里也是挂起。第二条是重点：服务层只认库里那一份。"""
        self.assertIs(result.state.status, TaskStatus.AWAITING_HUMAN)
        stored = self.store.load_task(task_id)
        self.assertIs(
            stored.status, TaskStatus.AWAITING_HUMAN,
            "库里留下 RUNNING 的话，界面上既停不掉也裁决不了",
        )


class TestVerifyFailure(ArchitectFailureFixture):
    """验收是**每次 COMPLETED 都会走**的一次模型调用，最容易被忘记包。"""

    def test_verify_failure_suspends_instead_of_crashing(self):
        class Backend(ScriptedBackend):
            def verify(self, spec, ctx):
                raise BudgetExceeded("会话 token 上限已用尽")

        result = self.run_with(
            Backend({(1, 0): Finish(output={}, summary="做完了")})
        )
        self.assert_suspended(result)

    def test_a_finished_task_does_not_get_recorded_as_completed(self):
        """任务确实跑完了，但没人验收得了 —— 不能当成 COMPLETED 收尾。"""

        class Backend(ScriptedBackend):
            def verify(self, spec, ctx):
                raise ModelCallFailed("上游 500")

        result = self.run_with(
            Backend({(1, 0): Finish(output={}, summary="做完了")})
        )
        self.assertIsNot(result.state.status, TaskStatus.COMPLETED)


class TestProbeFailure(ArchitectFailureFixture):
    def test_probe_failure_suspends_instead_of_crashing(self):
        """PROBE 的探查调用同样是架构师在花钱，它失败不该打挂整条链。"""

        class Backend(ScriptedBackend):
            def probe(self, spec, ctx, excerpts):
                raise BudgetExceeded("会话 token 上限已用尽")

        spec = self.spec(
            task_class=TaskClass.GENERATIVE,
            silence_policy=SilencePolicy.PROBE,
            probe_interval_s=0.0001,   # 下一个 step 边界就到点
            max_steps=6,
        )
        result = self.run_with(
            Backend({(1, 0): SoftSignalAction(signal_type="PROGRESS", detail="写了一段")}),
            spec=spec,
        )
        self.assert_suspended(result)


class TestTriageFailure(ArchitectFailureFixture):
    def test_soft_signal_triage_failure_suspends_instead_of_crashing(self):
        """软信号分诊走的是廉价模型，但它照样会撞上耗尽的 key。"""

        class Backend(ScriptedBackend):
            def triage(self, signals):
                raise BudgetExceeded("会话 token 上限已用尽")

        result = self.run_with(
            Backend(
                {
                    (1, 0): SoftSignalAction(signal_type="AMBIGUITY", detail="需求有歧义"),
                    (1, 1): Finish(output={}, summary="做完了"),
                }
            )
        )
        self.assert_suspended(result)


class TestSuspendedTimelineIsAnswerable(ArchitectFailureFixture):
    """挂起之后界面必须能给出裁决表单 —— 判据是「最后一条状态事件」。

    前端把 AWAITING_HUMAN 的 status 事件当作出表单的锚点。原来 orchestrator 在
    状态迁移**之后**又写了一行 `[STOP]` 日志，于是那条 status 不再是最后一条，
    表单降级成一行灰字，人看得见「挂起了」却无处答复。
    """

    def test_awaiting_human_is_the_last_status_event(self):
        class Backend(ScriptedBackend):
            def verify(self, spec, ctx):
                raise ModelCallFailed("上游 500")

        self.run_with(Backend({(1, 0): Finish(output={}, summary="做完了")}))

        events = self.store.events_for("t_archfail", 0)
        status_events = [e for e in events if e.kind == "status"]
        self.assertEqual(status_events[-1].payload["status"], "AWAITING_HUMAN")
        self.assertIs(
            status_events[-1], events[-1],
            "AWAITING_HUMAN 是终局态，它之后不该再有事件 —— "
            "有的话前端的裁决表单就不出现了",
        )

    def test_pending_ruling_is_answerable(self):
        """views.pending_ruling 要给得出东西，否则界面连「等什么」都说不出。"""
        from cowork import views

        class Backend(ScriptedBackend):
            def verify(self, spec, ctx):
                raise ModelCallFailed("上游 500")

        self.run_with(Backend({(1, 0): Finish(output={}, summary="做完了")}))
        pending = views.pending_ruling(self.store, "t_archfail")
        self.assertIsNotNone(pending)
        self.assertTrue(pending["reason"])


if __name__ == "__main__":
    unittest.main()

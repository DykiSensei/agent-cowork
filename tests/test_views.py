"""M6 §9 那四条后端缺口（前端落地时发现的）。

四条各自钉在这里：

  1. 挂起时 LLM 的建议要持久化   TestPendingSuggestion
  2. spec_changes 要进存储        TestSpecChanges
  3. 列表投影（GET /tasks）       TestThreadList
  4. 时间线要在 Store 里          TestEventTimeline

**形状是对外承诺**：界面层照着 `M6-界面层接口.md` 写死了字段名，所以这里断言的
是字段名和语义，不只是「跑得通」。
"""

from __future__ import annotations

import shutil
import tempfile
import unittest

from cowork import demo, views
from cowork.agent.architect import Architect, AutoApproveGate, HumanRuling
from cowork.llm import ArchitectVerdict
from cowork.llm.scripted import ScriptedBackend
from cowork.policy import Policy
from cowork.store import SqliteStore
from cowork.types import (
    Action,
    AgentContext,
    Criterion,
    SandboxProfile,
    TaskClass,
    TaskEvent,
    TaskSpec,
    TaskState,
    TaskStatus,
)


def spec(tid="t1", *, parent=None, goal="干活") -> TaskSpec:
    return TaskSpec(
        id=tid, parent_id=parent, goal=goal,
        acceptance=[Criterion("c1", "做完")], task_class=TaskClass.CODE,
        sandbox=SandboxProfile(workspace=tempfile.mkdtemp()),
    )


class TestPendingSuggestion(unittest.TestCase):
    """缺口 1：挂起那条记录里，action/rationale 是系统的兜底行为，不是模型的意见。"""

    def _architect(self, gate):
        backend = ScriptedBackend({}, verdict_for=lambda s, sig: ArchitectVerdict(
            action="ABANDON", rationale="证据为空，继续下去没有意义",
            complexity_score=0.9, spec_changes={"goal": "换个目标"},
        ))
        # ABANDON 必然升级（policy.escalate_on_abandon）
        return Architect(backend, SqliteStore(), policy=Policy(), human_gate=gate)

    def test_no_gate_still_records_what_the_model_suggested(self):
        target = spec()
        rec = self._architect(None).decide(
            TaskState(spec=target), [], AgentContext(task_spec=target))

        self.assertEqual(rec.action, Action.CONTINUE, "系统的兜底行为是挂起")
        self.assertIsNotNone(rec.suggestion, "模型的意见不能丢")
        self.assertEqual(rec.suggestion["action"], "ABANDON")
        self.assertIn("证据为空", rec.suggestion["rationale"])
        self.assertEqual(rec.suggestion["complexity_score"], 0.9)
        self.assertEqual(rec.suggestion["spec_changes"], {"goal": "换个目标"})

    def test_gate_returning_none_also_records_it(self):
        class Silent:
            def review(self, spec, signals, verdict, reason):
                return None

        target = spec()
        rec = self._architect(Silent()).decide(
            TaskState(spec=target), [], AgentContext(task_spec=target))
        self.assertEqual(rec.suggestion["action"], "ABANDON")

    def test_human_answered_keeps_both_sides(self):
        """人接手之后，模型当时的建议也要留着 —— 复盘要对照两边。"""
        class Override:
            def review(self, spec, signals, verdict, reason):
                return HumanRuling(Action.CONTINUE, "我觉得还能救")

        target = spec()
        rec = self._architect(Override()).decide(
            TaskState(spec=target), [], AgentContext(task_spec=target))

        self.assertEqual(rec.action, Action.CONTINUE)
        self.assertEqual(rec.rationale, "我觉得还能救")
        self.assertEqual(rec.suggestion["action"], "ABANDON")

    def test_escalated_paths_apply_nothing_so_spec_changes_stays_empty(self):
        """挂起时什么都没应用 —— 想改什么在 suggestion.spec_changes 里，不在这。

        两个字段分开是有意义的：`spec_changes` 是**已经生效的改动**，
        `suggestion.spec_changes` 是**模型提议但还没被采纳的**。混成一个，
        界面就分不清「这条验收标准已经加上了」和「系统建议加一条」。
        """
        target = spec()  # 顶层 + ABANDON，必然升级
        rec = self._architect(None).decide(
            TaskState(spec=target), [], AgentContext(task_spec=target))

        self.assertEqual(rec.spec_changes, {}, "没应用就是空")
        self.assertEqual(rec.suggestion["spec_changes"], {"goal": "换个目标"})

    def test_no_escalation_means_no_suggestion(self):
        """没升级的裁决不挂 suggestion —— action/rationale 本来就是模型说的。"""
        backend = ScriptedBackend({}, verdict_for=lambda s, sig: ArchitectVerdict(
            action="CONTINUE", rationale="再试一次", complexity_score=0.0))
        target = spec()
        rec = Architect(backend, SqliteStore(), policy=Policy()).decide(
            TaskState(spec=target), [], AgentContext(task_spec=target))
        self.assertIsNone(rec.suggestion)


class TestSpecChanges(unittest.TestCase):
    """缺口 2：只存 new_spec 的话，「哪条验收标准是新增的」重建不出来。"""

    def test_changes_are_recorded_and_survive_the_store(self):
        changes = {"added_criteria": [{"id": "c2", "description": "还要处理空行"}]}
        backend = ScriptedBackend({}, verdict_for=lambda s, sig: ArchitectVerdict(
            action="MODIFY_TASK", rationale="补一条验收标准",
            complexity_score=0.1, spec_changes=changes,
        ))
        store = SqliteStore()
        # 顶层任务的 MODIFY_TASK 一律升级给人（policy.escalate_on_toplevel_modify），
        # 那条路径上什么都没应用，spec_changes 本来就该是空的。要测「改动被记下来」
        # 得用子任务。
        target = spec(parent="root")
        rec = Architect(backend, store, policy=Policy()).decide(
            TaskState(spec=target), [], AgentContext(task_spec=target))
        store.save_decision(rec)

        self.assertEqual(rec.spec_changes, changes)
        self.assertEqual(rec.to_dict()["spec_changes"], changes)
        back = store.decisions_for(target.id)[0]
        self.assertEqual(back.spec_changes, changes)
        self.assertEqual(len(back.new_spec.acceptance), 2, "new_spec 仍然照旧")


class TestThreadList(unittest.TestCase):
    """缺口 3：GET /tasks 的列表投影。"""

    def setUp(self):
        self.store = SqliteStore()

    def test_summary_has_the_contract_fields_and_no_spec(self):
        self.store.save_task(TaskState(spec=spec(goal="做一个报告工具\n第二行不该进标题")))
        row = views.thread_list(self.store)[0]

        for key in ("task_id", "title", "status", "composite",
                    "tokens_used", "revision", "current_step"):
            self.assertIn(key, row)
        self.assertEqual(row["title"], "做一个报告工具", "标题只取第一行")
        self.assertNotIn("spec", row, "列表项不该驮完整 spec")

    def test_children_fold_into_their_parent(self):
        self.store.save_task(TaskState(spec=spec("root", goal="根任务")))
        self.store.save_task(TaskState(spec=spec("kid1", parent="root")))
        self.store.save_task(TaskState(spec=spec("kid2", parent="root")))

        rows = views.thread_list(self.store)
        self.assertEqual([r["task_id"] for r in rows], ["root"])
        self.assertTrue(rows[0]["composite"])

    def test_composite_without_a_parent_row_still_shows_up(self):
        """Scheduler 拿到的是一组现成 spec，没人建过父任务 —— 不能让它整个消失。"""
        self.store.save_task(TaskState(spec=spec("kid1", parent="task_comp")))
        self.store.save_task(TaskState(
            spec=spec("kid2", parent="task_comp"), status=TaskStatus.AWAITING_HUMAN))

        rows = views.thread_list(self.store)
        self.assertEqual([r["task_id"] for r in rows], ["task_comp"])
        self.assertTrue(rows[0]["composite"])
        self.assertEqual(rows[0]["status"], "AWAITING_HUMAN",
                         "一个子任务等人，整件事就是等人")

    def test_terminal_flag(self):
        self.store.save_task(TaskState(spec=spec("done"), status=TaskStatus.COMPLETED))
        self.store.save_task(TaskState(spec=spec("run"), status=TaskStatus.RUNNING))
        rows = {r["task_id"]: r["terminal"] for r in views.thread_list(self.store)}
        self.assertTrue(rows["done"])
        self.assertFalse(rows["run"])


class TestEventTimeline(unittest.TestCase):
    """缺口 4：时间线要落在 Store 里，且顺序由 seq 保证。"""

    def setUp(self):
        self.store = SqliteStore()

    def test_seq_is_assigned_monotonically_per_task(self):
        a = self.store.append_event(TaskEvent(task_id="t1", kind="log", text="一"))
        b = self.store.append_event(TaskEvent(task_id="t1", kind="log", text="二"))
        c = self.store.append_event(TaskEvent(task_id="t2", kind="log", text="别的任务"))

        self.assertEqual((a.seq, b.seq), (1, 2))
        self.assertEqual(c.seq, 1, "seq 是每个任务各自数的")

    def test_after_seq_gives_the_increment(self):
        for i in range(5):
            self.store.append_event(TaskEvent(task_id="t1", kind="log", text=str(i)))
        tail = self.store.events_for("t1", after_seq=3)
        self.assertEqual([e.seq for e in tail], [4, 5])

    def test_events_reference_bodies_instead_of_copying_them(self):
        """事件是到达序的索引，不是内容的第二份拷贝。"""
        ev = self.store.append_event(
            TaskEvent(task_id="t1", kind="signal", ref_id="sig_abc",
                      payload={"type": "TEST_FAILED"}))
        back = self.store.events_for("t1")[0]
        self.assertEqual(back.ref_id, "sig_abc")
        self.assertEqual(back.text, "", "信号正文不进事件表")
        self.assertEqual(back.payload["type"], "TEST_FAILED")
        self.assertEqual(back.id, ev.id)


class TestTimelineFromARealRun(unittest.TestCase):
    """跑一次真链路，看时间线能不能重建出「发生了什么」。"""

    def tearDown(self):
        shutil.rmtree(getattr(self, "ws", ""), ignore_errors=True)

    def test_demo_run_produces_a_readable_timeline(self):
        store = SqliteStore()
        orch, self.ws = demo.build(store=store)
        orch.log = lambda _m: None
        result = orch.run()

        detail = views.task_detail(store, result.state.spec.id)
        kinds = [e["kind"] for e in detail["events"]]

        self.assertIn("log", kinds)
        self.assertIn("status", kinds)
        self.assertIn("signal", kinds, "demo 场景本来就会中断一次")
        self.assertIn("decision", kinds)
        # 顺序稳定：seq 严格递增
        seqs = [e["seq"] for e in detail["events"]]
        self.assertEqual(seqs, sorted(seqs))
        self.assertEqual(len(seqs), len(set(seqs)))
        # 事件里的 ref_id 都能在正文索引里查到
        for e in detail["events"]:
            if e["kind"] == "signal":
                self.assertIn(e["ref_id"], detail["signals"])
            if e["kind"] == "decision":
                self.assertIn(e["ref_id"], detail["decisions"])

    def test_detail_shape_matches_the_contract(self):
        store = SqliteStore()
        orch, self.ws = demo.build(store=store)
        orch.log = lambda _m: None
        result = orch.run()

        detail = views.task_detail(store, result.state.spec.id)
        self.assertEqual(detail["kind"], "single")
        self.assertEqual(detail["state"]["task_id"], result.state.spec.id)
        self.assertIsNone(views.task_detail(store, "task_不存在"))


class TestCompositeDetail(unittest.TestCase):
    """复合任务在界面上是一条线程，它的时间线不属于任何一个子任务。"""

    def tearDown(self):
        shutil.rmtree(getattr(self, "ws", ""), ignore_errors=True)

    def test_scheduler_writes_the_composite_timeline(self):
        from cowork import demo_composite

        store = SqliteStore()
        sched, self.ws = demo_composite.build(store=store, log=lambda _m: None)
        sched.run(max_cycles=2)

        root = sched.root_id
        self.assertIsNotNone(root, "子任务有共同 parent_id 才有复合线程")

        detail = views.task_detail(store, root)
        self.assertEqual(detail["kind"], "composite")
        self.assertEqual(len(detail["tasks"]), 4)
        self.assertEqual(detail["plan"]["max_parallel"], 2, "分层图取自当时那份")
        self.assertIsNotNone(detail["review"])

        kinds = [e["kind"] for e in detail["events"]]
        self.assertIn("plan", kinds)
        self.assertIn("review", kinds)
        self.assertTrue(any("[LAYER]" in e["text"] for e in detail["events"]))

    def test_composite_thread_appears_in_the_list(self):
        from cowork import demo_composite

        store = SqliteStore()
        sched, self.ws = demo_composite.build(store=store, log=lambda _m: None)
        sched.run(max_cycles=2)

        rows = views.thread_list(store)
        self.assertEqual([r["task_id"] for r in rows], [sched.root_id])
        self.assertTrue(rows[0]["composite"])


class TestPendingRuling(unittest.TestCase):
    """「等你拍板」那张卡片要的东西，能不能从存储里取出来。"""

    def setUp(self):
        self.store = SqliteStore()

    def test_none_when_not_waiting(self):
        self.store.save_task(TaskState(spec=spec("t1"), status=TaskStatus.RUNNING))
        self.assertIsNone(views.pending_ruling(self.store, "t1"))

    def test_carries_reason_and_suggestion(self):
        from cowork.types import Decider, DecisionRecord

        self.store.save_task(TaskState(
            spec=spec("t1"), status=TaskStatus.AWAITING_HUMAN, checkpoint_id="ckpt_1"))
        self.store.save_decision(DecisionRecord(
            task_id="t1", trigger=[], decider=Decider.LLM, action=Action.CONTINUE,
            rationale="人未答复，任务挂起等待。", escalation_reason="连续 2 次指纹相同",
            suggestion={"action": "ABANDON", "rationale": "没救了",
                        "complexity_score": 0.8, "spec_changes": {}},
        ))

        pending = views.pending_ruling(self.store, "t1")
        self.assertEqual(pending["reason"], "连续 2 次指纹相同")
        self.assertEqual(pending["suggestion"]["action"], "ABANDON")
        self.assertEqual(pending["checkpoint_id"], "ckpt_1")

    def test_no_suggestion_is_reported_as_none_not_faked(self):
        """架构师连模型都调不动那条路径上没有建议，如实说没有。"""
        self.store.save_task(TaskState(spec=spec("t1"), status=TaskStatus.AWAITING_HUMAN))
        pending = views.pending_ruling(self.store, "t1")
        self.assertIsNone(pending["suggestion"])
        self.assertIsNone(pending["decision_id"])


if __name__ == "__main__":
    unittest.main()

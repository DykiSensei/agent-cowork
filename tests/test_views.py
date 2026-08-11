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

    def test_composite_title_uses_the_humans_own_words(self):
        """人的原话在 root 线程的第一条 human 事件里（M6 §9）。

        为什么不拿子任务的 goal 顶替：那是架构师写的。而父任务的 spec.goal
        在 rev>1 之后也已经不是人最初说的那句了 —— 所以只认这条事件。
        """
        from cowork.types import TaskEvent

        self.store.append_event(
            TaskEvent(task_id="task_comp", kind="human",
                      text="给我做个能查天气的小工具\n带缓存")
        )
        self.store.save_task(TaskState(spec=spec("kid1", parent="task_comp")))

        row = views.thread_list(self.store)[0]
        self.assertEqual(row["title"], "给我做个能查天气的小工具", "标题只取第一行")

    def test_composite_title_falls_back_when_there_is_no_human_event(self):
        """`cli composite` / 老库没有那条事件，退回合成标题而不是报错。"""
        self.store.save_task(TaskState(spec=spec("kid1", parent="task_comp")))
        row = views.thread_list(self.store)[0]
        self.assertEqual(row["title"], "复合任务（1 个子任务）")

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


class TestThreadIsMoreThanItsTasks(unittest.TestCase):
    """线程的存在性看**事件**，不看 tasks 行。

    `POST /tasks` 一落地就写了人的原话，而子任务要等派发之后各自的 Orchestrator
    起跑才有 tasks 行。中间那一段（拆解中、刚派发）线程只活在 events 里。

    实测撞到的就是这段真空期：派发成功、界面切到新线程，详情回 404，
    **整页变成「连不上服务」，刷新一下又好了**（那时子任务已经起来了）。
    """

    def setUp(self):
        self.store = SqliteStore()
        self.root = "task_root1"
        self.store.append_event(
            TaskEvent(task_id=self.root, kind="human", text="做一个 CSV 转换器")
        )

    def test_detail_is_available_before_any_subtask_exists(self):
        detail = views.task_detail(self.store, self.root)

        self.assertIsNotNone(detail, "有事件就是有线程，不能回 None（那会变成 404）")
        self.assertEqual(detail["kind"], "composite")
        self.assertEqual(detail["tasks"], {})
        self.assertEqual(detail["root_goal"], "做一个 CSV 转换器")
        self.assertEqual(detail["pending"], {})

    def test_it_shows_up_in_the_list_too(self):
        """详情有、列表没有的话，人刚发布的任务在侧栏里根本不出现。"""
        rows = views.thread_list(self.store)

        self.assertEqual([r["task_id"] for r in rows], [self.root])
        self.assertEqual(rows[0]["title"], "做一个 CSV 转换器")
        self.assertFalse(rows[0]["terminal"])

    def test_a_task_that_really_does_not_exist_is_still_none(self):
        self.assertIsNone(views.task_detail(self.store, "task_nope"))


class TestCompositePendingAndProgress(unittest.TestCase):
    """复合线程上，人要能看出「谁在等我、等什么」和「各自在做什么」。

    子任务被折进父线程（侧栏里点不到），所以这两样东西**只能**从复合详情里给。
    原来只有一串 `pending_children` 的 id：界面知道有人在等，却拿不到升级原因和
    系统建议，于是渲染不出裁决表单 —— 实测就卡在这里，任务停着而人无处答复。
    """

    def setUp(self):
        self.store = SqliteStore()
        self.root = "task_rootc"
        kid = TaskState(spec=spec("t_kid", parent=self.root), status=TaskStatus.AWAITING_HUMAN)
        kid.checkpoint_id = None
        self.store.save_task(kid)
        self.kid = kid
        # 挂起那条占位裁决 —— pending_ruling 要靠它给出「等的是什么」
        from cowork.types import Decider, DecisionRecord

        self.store.save_decision(
            DecisionRecord(
                task_id="t_kid", trigger=[], decider=Decider.LLM, action=Action.CONTINUE,
                rationale="需要人决策但无介入入口，任务挂起。",
                escalation_reason="决策是 ABANDON —— 放弃对该任务不可逆，需人确认",
                suggestion={"action": "ABANDON", "rationale": "证据为空",
                            "complexity_score": 0.9, "spec_changes": {}},
            )
        )

    def test_pending_carries_the_ruling_material_per_child(self):
        detail = views.task_detail(self.store, self.root)

        self.assertEqual(detail["pending_children"], ["t_kid"])
        pending = detail["pending"]["t_kid"]
        self.assertIn("ABANDON", pending["reason"])
        self.assertEqual(pending["suggestion"]["action"], "ABANDON")

    def test_progress_says_what_each_child_is_doing(self):
        detail = views.task_detail(self.store, self.root)
        p = detail["progress"]["t_kid"]

        self.assertEqual(p["status"], "AWAITING_HUMAN")
        self.assertEqual(p["max_steps"], self.kid.spec.max_steps)
        self.assertEqual(p["goal"], "干活")

    def test_progress_reports_the_last_action_from_the_checkpoint(self):
        """「在做什么」取自 reasoning_trace 末尾 —— 那是它真干过的事，比日志准。"""
        from cowork.types import Checkpoint

        ctx = AgentContext(task_spec=self.kid.spec)
        ctx.reasoning_trace = [
            {"role": "assistant", "step": 3,
             "action": {"kind": "tool_call", "name": "write_file",
                        "args": {"path": "out.py", "content": "<40 chars>"},
                        "thought": "先把骨架写出来"}},
            {"role": "tool", "step": 3, "name": "write_file", "ok": True, "exit_code": 0},
        ]
        cp = Checkpoint(task_id="t_kid", step=3, agent_context=ctx)
        self.store.save_checkpoint(cp)
        running = TaskState(spec=self.kid.spec, status=TaskStatus.RUNNING,
                            checkpoint_id=cp.id, current_step=3)
        self.store.save_task(running)

        p = views.task_detail(self.store, self.root)["progress"]["t_kid"]
        self.assertEqual(p["last_action"]["name"], "write_file")
        self.assertEqual(p["last_action"]["target"], "out.py")
        self.assertEqual(p["last_action"]["thought"], "先把骨架写出来")
        self.assertTrue(p["last_result"]["ok"])

    def test_terminal_tasks_do_not_pay_for_a_checkpoint_load(self):
        """终局任务已经不在做任何事，而 checkpoint 里带着整份上下文。"""
        done = TaskState(spec=self.kid.spec, status=TaskStatus.COMPLETED,
                         checkpoint_id="ckpt_whatever")
        self.store.save_task(done)

        loads = []
        real = self.store.load_checkpoint
        self.store.load_checkpoint = lambda cid: (loads.append(cid), real(cid))[1]
        try:
            p = views.task_progress(self.store, done)
        finally:
            self.store.load_checkpoint = real

        self.assertEqual(loads, [])
        self.assertIsNone(p["last_action"])
        self.assertTrue(p["terminal"])


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

"""OpenAI 兼容后端（DeepSeek / Kimi / 任何 OpenAI 兼容端点）。

前半段不需要任何 key：验证 JSON 解析与本地校验这条兜底链路 ——
它存在的理由是不赌供应商的结构化输出支持。

后半段打真实供应商，只在设了对应 key 时才跑：
    DEEPSEEK_API_KEY=sk-... python -m unittest tests.test_openai_compat
"""

import json
import os
import shutil
import unittest

from cowork.llm.anthropic_backend import _parse_action
from cowork.llm.errors import ModelError
from cowork.llm.openai_compat import _parse_and_validate

ACTION_LIKE = {
    "type": "object",
    "properties": {
        "kind": {"type": "string", "enum": ["tool_call", "finish"]},
        "thought": {"type": "string"},
    },
    "required": ["kind", "thought"],
}


class TestJsonSalvage(unittest.TestCase):
    """供应商的 JSON 模式各家脾气不同，本地这层兜底必须自己站得住。"""

    def test_plain_json(self):
        data, errs = _parse_and_validate('{"kind":"finish","thought":"done"}', ACTION_LIKE)
        self.assertEqual(errs, [])
        self.assertEqual(data["kind"], "finish")

    def test_markdown_fence_is_stripped(self):
        raw = '```json\n{"kind":"finish","thought":"done"}\n```'
        data, errs = _parse_and_validate(raw, ACTION_LIKE)
        self.assertEqual(errs, [])
        self.assertEqual(data["kind"], "finish")

    def test_bare_fence_is_stripped(self):
        data, _ = _parse_and_validate('```\n{"kind":"finish","thought":"x"}\n```', ACTION_LIKE)
        self.assertIsNotNone(data)

    def test_invalid_json_reports_error(self):
        data, errs = _parse_and_validate("这不是 JSON", ACTION_LIKE)
        self.assertIsNone(data)
        self.assertIn("不是合法 JSON", errs[0])

    def test_schema_violation_is_caught_locally(self):
        """供应商即使不支持 schema 约束，也不该让脏数据流进 Subagent。"""
        data, errs = _parse_and_validate('{"kind":"nope","thought":"x"}', ACTION_LIKE)
        self.assertIsNone(data)
        self.assertTrue(any("enum" in e for e in errs), errs)

    def test_missing_required_field(self):
        data, errs = _parse_and_validate('{"kind":"finish"}', ACTION_LIKE)
        self.assertIsNone(data)
        self.assertTrue(any("缺少必填字段" in e for e in errs), errs)

    def test_toplevel_array_rejected(self):
        data, errs = _parse_and_validate('[{"kind":"finish"}]', ACTION_LIKE)
        self.assertIsNone(data)
        self.assertIn("顶层必须是对象", errs[0])


class TestActionParsing(unittest.TestCase):
    """schema 通过 ≠ 语义有效。

    ACTION_SCHEMA 用空串表示「本字段不适用」，于是 kind=tool_call + tool="" 是
    合法 JSON、能过本地校验，再往下才炸。M2 跑批里 75 次运行有 3 次死在这里
    （§11.6b）。解析失败必须是 ModelError —— 那样 step 循环会把它变成硬信号交给
    架构师，而不是让异常穿透整个 run。
    """

    def _full(self, **over):
        base = {
            "kind": "finish", "thought": "", "tool": "", "path": "", "content": "",
            "command": [], "output_json": "", "summary": "", "signal_type": "", "detail": "",
        }
        base.update(over)
        return base

    def test_empty_tool_becomes_model_error(self):
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="tool_call", tool=""))

    def test_unknown_tool_becomes_model_error(self):
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="tool_call", tool="rm_rf"))

    def test_empty_signal_type_becomes_model_error(self):
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="soft_signal", signal_type=""))

    def test_hard_signal_type_rejected_as_soft(self):
        """软信号通道不能用来伪造硬信号 —— 那是 Runtime 的专属职责（§3.1）。"""
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="soft_signal", signal_type="TEST_FAILED"))

    def test_unknown_kind_becomes_model_error(self):
        with self.assertRaises(ModelError):
            _parse_action(self._full(kind="rm -rf /"))

    def test_valid_actions_still_parse(self):
        a = _parse_action(self._full(kind="tool_call", tool="write_file",
                                     path="x.py", content="y"))
        self.assertEqual((a.name, a.args["path"]), ("write_file", "x.py"))
        b = _parse_action(self._full(kind="soft_signal", signal_type="AMBIGUITY",
                                     detail="不确定"))
        self.assertEqual(b.signal_type, "AMBIGUITY")
        c = _parse_action(self._full(kind="finish", output_json='{"a":1}', summary="s"))
        self.assertEqual(c.output, {"a": 1})


class TestProviderResolution(unittest.TestCase):
    """回归：两家 key 同时存在时，各家必须拿到自己的 base_url 和 key。

    实测踩过——`--backend kimi` 曾把 DeepSeek 的 key 发到 LiteLLM 代理上，
    因为 base_url 的预设查表漏了 kimi，而 key 走的是后端内部的固定顺序回退链。
    单独设一家 key 时这个 bug 不会显形。
    """

    def setUp(self):
        self._saved = {
            k: os.environ.get(k)
            for k in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY",
                      "COWORK_LLM_BASE_URL", "COWORK_LLM_API_KEY")
        }
        os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek-fake"
        os.environ["MOONSHOT_API_KEY"] = "sk-moonshot-fake"
        os.environ.pop("COWORK_LLM_BASE_URL", None)
        os.environ.pop("COWORK_LLM_API_KEY", None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_each_provider_gets_its_own_endpoint_and_key(self):
        from cowork.cli import _make_backend

        ds = _make_backend("deepseek")
        self.assertIn("deepseek.com", str(ds.client.base_url))
        self.assertEqual(ds.client.api_key, "sk-deepseek-fake")

        km = _make_backend("kimi")
        self.assertIn("moonshot.cn", str(km.client.base_url))
        self.assertEqual(km.client.api_key, "sk-moonshot-fake")

    def test_env_override_wins(self):
        from cowork.cli import _make_backend

        os.environ["COWORK_LLM_BASE_URL"] = "http://localhost:4000/v1"
        os.environ["COWORK_LLM_API_KEY"] = "sk-virtual-key"
        b = _make_backend("deepseek")
        self.assertIn("localhost:4000", str(b.client.base_url))
        self.assertEqual(b.client.api_key, "sk-virtual-key")


class TestMissingKey(unittest.TestCase):
    """没配 key 要在**构造后端时**说清楚，不要变成跑到一半的 401。

    这里曾经一律回退 `"placeholder"` 然后拿它去打真实端点，用户看到的是
    `Your api key: ****lder is invalid` —— 读起来像账号出问题，
    而真相是「你还没配置」。首次运行最容易撞的就是这一条。
    """

    def setUp(self):
        self._saved = {
            k: os.environ.get(k)
            for k in ("DEEPSEEK_API_KEY", "MOONSHOT_API_KEY", "OPENAI_API_KEY",
                      "COWORK_LLM_BASE_URL", "COWORK_LLM_API_KEY")
        }
        for k in self._saved:
            os.environ.pop(k, None)

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_real_endpoint_without_key_is_refused(self):
        from cowork.cli import _make_backend
        from cowork.llm.errors import MissingApiKey

        with self.assertRaises(MissingApiKey) as cm:
            _make_backend("deepseek")
        msg = str(cm.exception)
        # 三条出路都要在，缺一条这段话就不完整
        self.assertIn("DEEPSEEK_API_KEY", msg)
        self.assertIn("cowork demo", msg)     # 不需要 key 的那条路
        self.assertIn("cowork serve", msg)    # 设置页那条路

    def test_missing_key_is_not_a_model_error(self):
        """**刻意不是 ModelError**：那条路会把它归成硬信号、交给架构师、
        最后以 AWAITING_HUMAN 收尾 —— 对「模型调用失败」是对的，
        对「你还没配置」是灾难。
        """
        from cowork.llm.errors import MissingApiKey, ModelError

        self.assertFalse(issubclass(MissingApiKey, ModelError))

    def test_self_hosted_proxy_still_works_without_key(self):
        """自托管代理常常不校验 key —— 占位符对它是合理的，不能一刀切。"""
        from cowork.llm.openai_compat import OpenAICompatBackend

        b = OpenAICompatBackend(
            base_url="http://localhost:4000/v1", architect_model="whatever"
        )
        self.assertEqual(b.client.api_key, "placeholder")

    def test_unparseable_host_counts_as_external(self):
        """解析不出主机名时按外部端点处理 —— 宁可多问一次 key，
        也不要把占位符发到别人的服务器上。
        """
        from cowork.llm.openai_compat import _is_self_hosted

        self.assertTrue(_is_self_hosted("http://127.0.0.1:9999/v1"))
        self.assertTrue(_is_self_hosted("http://localhost/v1"))
        self.assertFalse(_is_self_hosted("https://api.deepseek.com/v1"))
        self.assertFalse(_is_self_hosted("not a url"))


class TestPresetModels(unittest.TestCase):
    """预设里写的 model id 是不是当前真的被服务的那个。

    需要一把 key 才构造得出后端（否则 MissingApiKey），所以自己设一个假的。
    """

    def setUp(self):
        self._saved = os.environ.get("DEEPSEEK_API_KEY")
        os.environ["DEEPSEEK_API_KEY"] = "sk-deepseek-fake"

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("DEEPSEEK_API_KEY", None)
        else:
            os.environ["DEEPSEEK_API_KEY"] = self._saved

    def test_deepseek_preset_uses_a_currently_served_model(self):
        """DeepSeek v4 起只暴露 flash / pro 两个 id，chat / reasoner 只剩别名。

        这条钉的是「预设里写的是当前真的存在的 model id」，不是分工 ——
        三个角色现在统一 flash，§4.1 的「架构师用推理档」要靠
        COWORK_ARCHITECT_MODEL=deepseek-v4-pro 显式拿回来。
        """
        from cowork.cli import _make_backend

        ds = _make_backend("deepseek")
        self.assertEqual(ds.architect_model, "deepseek-v4-flash")
        self.assertEqual(ds.subagent_model, "deepseek-v4-flash")

    def test_architect_model_can_be_lifted_back_to_the_reasoning_tier(self):
        from cowork.cli import _make_backend

        os.environ["COWORK_ARCHITECT_MODEL"] = "deepseek-v4-pro"
        self.addCleanup(os.environ.pop, "COWORK_ARCHITECT_MODEL", None)
        self.assertEqual(_make_backend("deepseek").architect_model, "deepseek-v4-pro")


class TestReviewerResolution(unittest.TestCase):
    """--reviewer auto 的意图只有一条：复核者尽量不是拆解者自己（§11.11）。"""

    def test_auto_picks_the_default_reviewer_for_real_backends(self):
        from cowork.cli import DEFAULT_REVIEWER, resolve_reviewer

        self.assertEqual(resolve_reviewer("deepseek", "auto"), DEFAULT_REVIEWER)

    def test_auto_swaps_direction_when_the_generator_is_already_the_reviewer(self):
        from cowork.cli import DEFAULT_REVIEWER, resolve_reviewer

        self.assertEqual(resolve_reviewer(DEFAULT_REVIEWER, "auto"), "deepseek")

    def test_scripted_backend_does_not_pay_for_a_review(self):
        from cowork.cli import resolve_reviewer

        self.assertIsNone(resolve_reviewer("scripted", "auto"))

    def test_none_falls_back_to_same_model_review(self):
        from cowork.cli import resolve_reviewer

        self.assertIsNone(resolve_reviewer("deepseek", "none"))

    def test_explicit_choice_wins(self):
        from cowork.cli import resolve_reviewer

        self.assertEqual(resolve_reviewer("deepseek", "anthropic"), "anthropic")


class TestRepairRound(unittest.TestCase):
    """回归：空回复不能被原样回灌进修复轮。

    OpenAI 兼容端点拒绝 content 为空的 assistant 消息（400 "must not be empty"），
    于是修复轮的**请求本身**非法 —— 一次可恢复的解析失败被升级成硬失败，
    重试机会白白吃掉。M7 7.2 的 120 次复核调用里有 2 次栽在这（§11.11）。
    """

    def _backend(self, replies):
        from cowork.llm.openai_compat import OpenAICompatBackend

        b = OpenAICompatBackend(base_url="http://localhost:1/v1", api_key="sk-fake")
        b.client = _FakeClient(replies)
        return b

    def test_empty_reply_does_not_produce_an_empty_assistant_turn(self):
        b = self._backend(["", '{"sufficient": true, "missing": []}'])
        sufficient, missing, _ = b.review_decomposition("目标", [])

        self.assertTrue(sufficient)
        self.assertEqual(missing, [])
        second = b.client.calls[1]["messages"]
        self.assertTrue(
            all(m["content"].strip() for m in second),
            f"修复轮里出现了空消息：{second}",
        )

    def test_truncated_reply_is_retried_clean_not_repaired(self):
        """截断是掷骰子（thinking 吃掉了额度），把残文回灌只会让它接着写半截 JSON。"""
        b = self._backend([])
        b.client = _FakeClient(
            ['{"sufficient": tr', '{"sufficient": true, "missing": []}'],
            finish_reasons=["length", "stop"],
        )
        sufficient, _, _ = b.review_decomposition("目标", [])

        self.assertTrue(sufficient)
        self.assertEqual(
            b.client.calls[1]["messages"], b.client.calls[0]["messages"],
            "截断重试应原样重发，不带残文",
        )

    def test_truncation_says_so_instead_of_blaming_the_json(self):
        b = self._backend([])
        b.client = _FakeClient(["", ""], finish_reasons=["length", "length"])
        with self.assertRaises(ModelError) as caught:
            b.review_decomposition("目标", [])
        self.assertIn("截断", str(caught.exception))

    def test_non_empty_bad_reply_is_still_echoed_back(self):
        """有内容的坏输出要原样回灌 —— 模型得看见自己写错了什么。"""
        b = self._backend(["这不是 JSON", '{"sufficient": false, "missing": ["x"]}'])
        sufficient, missing, _ = b.review_decomposition("目标", [])

        self.assertFalse(sufficient)
        self.assertEqual(missing, ["x"])
        roles = [m["role"] for m in b.client.calls[1]["messages"]]
        self.assertIn("assistant", roles)


class _FakeClient:
    """按脚本依次返回 content 的假 client，记录每轮请求。"""

    def __init__(self, replies, finish_reasons=None):
        self.replies = list(replies)
        self.finish_reasons = list(finish_reasons or [])
        self.calls = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.replies.pop(0) if self.replies else ""
        reason = self.finish_reasons.pop(0) if self.finish_reasons else "stop"
        msg = type("M", (), {"content": text})()
        choice = type("C", (), {"message": msg, "finish_reason": reason})()
        return type("R", (), {"choices": [choice], "usage": None})()


class TestPromptCaching(unittest.TestCase):
    """缓存命中率靠的是拼装顺序，而顺序是一条**沉默的**不变量（§11.14）。

    把 schema 挪进 user、或者在 system 里插一个任务 id / 时间戳，功能测试全绿，
    命中率直接归零，账单要下个月才告诉你。所以在这里钉死。
    """

    def _backend(self, **over):
        from cowork.llm.openai_compat import OpenAICompatBackend

        kw = dict(base_url="http://localhost:1/v1", api_key="sk-fake")
        kw.update(over)
        b = OpenAICompatBackend(**kw)
        b.client = _FakeClient(['{"sufficient": true, "missing": []}'] * 8)
        return b

    def test_static_first_variable_last(self):
        b = self._backend()
        b.review_decomposition("这个目标每次都不一样", [])
        msgs = b.client.calls[0]["messages"]

        self.assertEqual([m["role"] for m in msgs], ["system", "user"])
        self.assertNotIn("这个目标每次都不一样", msgs[0]["content"], "可变内容混进了前缀")
        self.assertIn("sufficient", msgs[0]["content"], "schema 应该待在可缓存的前缀里")

    def test_prefix_is_identical_across_calls_of_the_same_kind(self):
        b = self._backend()
        b.review_decomposition("目标 A", [])
        b.review_decomposition("目标 B", [])
        self.assertEqual(
            b.client.calls[0]["messages"][0], b.client.calls[1]["messages"][0],
            "同一种调用的 system 块必须逐字节相同，否则前缀缓存一次都不命中",
        )

    def test_cache_key_is_derived_from_the_prefix(self):
        """key 跟着提示词走：改了提示词自动换分片，不会指着旧的。"""
        b = self._backend(cache_key_supported=True)
        b.review_decomposition("目标 A", [])
        b.review_decomposition("目标 B", [])
        keys = [c["prompt_cache_key"] for c in b.client.calls]
        self.assertEqual(keys[0], keys[1])

        other = self._backend(cache_key_supported=True)
        other.client = _FakeClient([json.dumps({"subtasks": [{
            "id": "t1", "goal": "做 t1", "task_class": "CODE", "scope": ["t1.py"],
            "depends_on": [], "acceptance": [
                {"id": "c1", "description": "行为判据", "command": []}],
        }]})])
        other.decompose("随便一个目标")
        self.assertNotEqual(other.client.calls[0]["prompt_cache_key"], keys[0],
                            "不同调用类型的前缀不同，key 也该不同")

    def test_cache_key_not_sent_to_providers_that_did_not_declare_it(self):
        """不认识的字段在严格端点上是 400 —— 为一点路由收益打挂整条链不划算。"""
        b = self._backend()
        b.review_decomposition("目标", [])
        self.assertNotIn("prompt_cache_key", b.client.calls[0])

    def test_skills_go_into_the_static_prefix_and_only_when_picked(self):
        """skill 是**明知的**前缀分叉（M12，§11.31）：只带勾选的那几份。

        两件事要钉住 ——
        没勾时提示词逐字节不变（不用这个功能的人的命中率不该被动下降），
        勾了时正文进 **system**（进 user 的话它每次都要重付费，且缓存不覆盖）。
        """
        import os
        import shutil
        import tempfile
        from unittest import mock

        from cowork.actions import Finish
        from cowork.types import (
            AgentContext, Criterion, SandboxProfile, TaskClass, TaskSpec,
        )

        root = tempfile.mkdtemp(prefix="cowork-skills-cache-")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        ws = tempfile.mkdtemp(prefix="cowork-skills-ws-")
        self.addCleanup(shutil.rmtree, ws, ignore_errors=True)
        with open(os.path.join(root, "py-style.md"), "w", encoding="utf-8") as f:
            f.write("---\nname: py-style\n---\n缩进用四个空格")

        def ctx(skills):
            spec = TaskSpec(
                goal="干活", acceptance=[Criterion("c1", "做完")],
                task_class=TaskClass.CODE,
                sandbox=SandboxProfile(workspace=ws), scope=["a.py"], skills=skills,
            )
            return AgentContext(task_spec=spec)

        action = json.dumps({
            "kind": "finish", "thought": "", "tool": "", "path": "", "content": "",
            "append": False, "command": [], "pattern": "", "glob": "", "to": "", "url": "",
            "query": "", "recursive": False, "signal_type": "", "detail": "",
            "summary": "做完了", "output_json": "{}",
        })
        with mock.patch.dict(os.environ, {"COWORK_SKILLS_DIR": root}):
            b = self._backend()
            b.client = _FakeClient([action, action])
            b.next_step(ctx([]))
            b.next_step(ctx(["py-style"]))

        plain, withskill = (c["messages"][0]["content"] for c in b.client.calls)
        self.assertNotIn("缩进用四个空格", plain, "没勾的人一个字都不该多付")
        self.assertIn("缩进用四个空格", withskill, "勾了就要真的进提示词")
        self.assertTrue(withskill.startswith(plain), "分叉只能发生在前缀的**尾部**")
        self.assertNotIn(
            "缩进用四个空格", b.client.calls[1]["messages"][1]["content"],
            "skill 是静态的，进 user 就等于每次重付费且缓存不覆盖",
        )


class TestCacheStats(unittest.TestCase):
    """各家报缓存用量的字段名不一样，两种形状都要认（§11.14）。"""

    def _usage(self, **kw):
        return type("U", (), kw)()

    def test_openai_shape(self):
        from cowork.llm import CacheStats

        s = CacheStats()
        s.observe(self._usage(
            prompt_tokens=1000,
            prompt_tokens_details=type("D", (), {"cached_tokens": 768})(),
        ))
        self.assertEqual(s.cached_tokens, 768)
        self.assertAlmostEqual(s.hit_rate, 0.768)

    def test_deepseek_shape(self):
        from cowork.llm import CacheStats

        s = CacheStats()
        s.observe(self._usage(prompt_tokens=868, prompt_cache_hit_tokens=768))
        self.assertEqual(s.cached_tokens, 768)

    def test_moonshot_top_level_field(self):
        """Moonshot 还额外挂一个顶层 cached_tokens —— 实测抓到的第三种形状。"""
        from cowork.llm import CacheStats

        s = CacheStats()
        s.observe(self._usage(prompt_tokens=1786, cached_tokens=1536))
        self.assertEqual(s.cached_tokens, 1536)

    def test_null_details_on_the_first_call(self):
        """Moonshot 第一次调用 prompt_tokens_details 整个是 null，不能炸。"""
        from cowork.llm import CacheStats

        s = CacheStats()
        s.observe(self._usage(prompt_tokens=1786, prompt_tokens_details=None))
        self.assertEqual(s.cached_tokens, 0)
        self.assertEqual(s.calls_with_usage, 1)

    def test_missing_usage_is_not_a_zero_hit_rate(self):
        """「这家不报」和「一次没命中」不是一回事，混了就会得出错误结论。"""
        from cowork.llm import CacheStats

        s = CacheStats()
        s.observe(None)
        s.observe(self._usage(prompt_tokens=0))
        self.assertEqual(s.calls, 2)
        self.assertEqual(s.calls_with_usage, 0)
        self.assertIsNone(s.hit_rate)

    def test_accumulates_across_calls(self):
        from cowork.llm import CacheStats

        s = CacheStats()
        s.observe(self._usage(prompt_tokens=100, prompt_cache_hit_tokens=0))
        s.observe(self._usage(prompt_tokens=100, prompt_cache_hit_tokens=100))
        self.assertEqual((s.prompt_tokens, s.cached_tokens), (200, 100))
        self.assertEqual(s.hit_rate, 0.5)


class TestAnthropicCaching(unittest.TestCase):
    """Anthropic 的缓存是**显式**的：不打断点就一次都不命中（§11.14）。

    这条和 OpenAI 系「够长就自动缓存」不是一回事，删掉 cache_control 之后
    功能一切正常、命中率静默归零 —— 所以要有测试盯着。
    """

    def _backend(self):
        from cowork.llm.anthropic_backend import AnthropicBackend

        b = AnthropicBackend(api_key="sk-fake")
        b.client = _FakeAnthropic()
        return b

    def test_system_block_carries_a_cache_breakpoint(self):
        b = self._backend()
        b.review_decomposition("目标", [])
        system = b.client.calls[0]["system"]

        self.assertIsInstance(system, list, "system 要是块列表才挂得上 cache_control")
        self.assertEqual(system[0]["cache_control"], {"type": "ephemeral"})

    def test_variable_content_stays_out_of_the_cached_block(self):
        b = self._backend()
        b.review_decomposition("这个目标每次都不一样", [])
        self.assertNotIn("这个目标每次都不一样", b.client.calls[0]["system"][0]["text"])

    def test_cache_tokens_are_added_back_into_the_total(self):
        """缓存读写不含在 input_tokens 里，不加回去的话账面 token 会凭空变少。"""
        b = self._backend()
        b.client.usage = type("U", (), {
            "input_tokens": 100, "output_tokens": 50,
            "cache_read_input_tokens": 900, "cache_creation_input_tokens": 0,
        })()
        _, _, tokens = b.review_decomposition("目标", [])

        self.assertEqual(tokens, 1050)
        self.assertEqual(b.cache_stats.cached_tokens, 900)
        self.assertAlmostEqual(b.cache_stats.hit_rate, 0.9)


class _FakeAnthropic:
    def __init__(self):
        self.calls = []
        self.messages = self
        self.usage = type("U", (), {
            "input_tokens": 10, "output_tokens": 5,
            "cache_read_input_tokens": 0, "cache_creation_input_tokens": 0,
        })()

    def create(self, **kwargs):
        self.calls.append(kwargs)
        block = type("B", (), {"type": "text", "text": '{"sufficient": true, "missing": []}'})()
        return type("R", (), {
            "content": [block], "usage": self.usage, "stop_reason": "end_turn",
        })()


class TestBackendWiring(unittest.TestCase):
    def test_reasoner_skips_json_mode(self):
        """deepseek-reasoner 不支持 response_format，只能靠提示词约束。"""
        from cowork.llm.openai_compat import _NO_JSON_MODE

        self.assertTrue(any(m in "deepseek-reasoner" for m in _NO_JSON_MODE))

    def test_presets(self):
        from cowork.llm.openai_compat import PRESETS

        self.assertIn("deepseek", PRESETS)
        self.assertIn("moonshot", PRESETS)


def _live_provider() -> tuple[str, str, str] | None:
    """返回 (base_url, api_key, model)，没配 key 就返回 None。"""
    if os.environ.get("DEEPSEEK_API_KEY"):
        return ("https://api.deepseek.com/v1", os.environ["DEEPSEEK_API_KEY"], "deepseek-chat")
    if os.environ.get("MOONSHOT_API_KEY"):
        return ("https://api.moonshot.cn/v1", os.environ["MOONSHOT_API_KEY"], "kimi-k3")
    return None


@unittest.skipUnless(_live_provider(), "未设 DEEPSEEK_API_KEY / MOONSHOT_API_KEY")
class TestLiveProvider(unittest.TestCase):
    """打真实供应商。验证的是「结构化输出这条路走不走得通」，不是模型聪不聪明。"""

    @classmethod
    def setUpClass(cls):
        from cowork.llm.openai_compat import OpenAICompatBackend

        base, key, model = _live_provider()
        cls.model = model
        cls.backend = OpenAICompatBackend(
            base_url=base, api_key=key, subagent_model=model,
            architect_model=model, triage_model=model,
        )

    def test_structured_output_roundtrip(self):
        data, tokens = self.backend._call(
            model=self.model,
            system="你是一个测试桩。",
            user="返回 kind=finish，thought 写「测试」。",
            schema=ACTION_LIKE,
        )
        self.assertEqual(data["kind"], "finish")
        self.assertGreater(tokens, 0, "usage 必须能读出来，否则预算统计是假的")

    def test_full_chain_with_real_model(self):
        """M1.3 出口：demo 场景用真实模型跑通。

        断言只到「终局状态合法且有账可查」为止。原来这里要求必须
        COMPLETED 或 AWAITING_HUMAN，M2 实测证明那是错的：同一 spec 同一模型
        跨 75 次运行中断次数落在 0–5，跑满 max_cycles 后 FAILED 是**设计内的**
        终局，不是缺陷（§11.6d）。拿概率性结果做断言只会得到一个偶发红灯的测试。
        """
        from cowork import demo
        from cowork.types import TaskStatus

        orch, ws = demo.build(backend=self.backend)
        orch.log = lambda _m: None
        try:
            result = orch.run(max_cycles=4)
            self.assertIn(
                result.state.status,
                (TaskStatus.COMPLETED, TaskStatus.AWAITING_HUMAN,
                 TaskStatus.FAILED, TaskStatus.ABANDONED),
                "终局状态必须是状态机里定义过的那几个",
            )
            self.assertGreater(result.state.tokens_used, 0)
            if result.state.status is not TaskStatus.COMPLETED:
                # 没成不要紧，但必须留下为什么没成的记录 —— 这是 §7.3 可见性的底线
                self.assertTrue(
                    result.decisions,
                    "非 COMPLETED 的终局必须有 DecisionRecord 解释原因",
                )
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

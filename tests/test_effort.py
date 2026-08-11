"""推理挡位的映射（§10.3.2）。

这一层最容易出的错不是「算错」，是**静默失效**：发一个对方不认识的字段，
宽松端点会照单全收然后忽略它 —— 于是旋钮看着能调、实际没接上，
而账单和延迟都不会告诉你（M2 已经有两个这样的死参数，§11.6c）。

所以这里钉三件事：
  只给声明过的供应商下发    没声明 = 一个字段都不发
  取整必须看得见            没有 medium 档时，note 要说清楚取整到哪了
  关不掉的家要如实回落      Kimi k3 / xAI 无论如何都会思考，不假装关掉了
"""

from __future__ import annotations

import unittest

from cowork.llm.effort import LEVELS, PROFILES, EffortError, resolve


class TestVocabulary(unittest.TestCase):
    def test_levels_are_ordered_low_to_high(self):
        """取整靠的就是这个顺序，顺序错了取整方向就反了。"""
        self.assertEqual(LEVELS, ("off", "low", "medium", "high", "max"))

    def test_unknown_level_fails_loudly(self):
        """拼错的挡位如果被忽略，就又是一个静默失效的配置。"""
        with self.assertRaises(EffortError):
            resolve("deepseek", "highest")

    def test_unknown_profile_fails_loudly(self):
        with self.assertRaises(EffortError):
            resolve("没这家", "high")


class TestNoProfileSendsNothing(unittest.TestCase):
    def test_undeclared_provider_gets_no_field(self):
        """litellm 后面是谁不知道 —— 宁可旋钮不起作用，也不发可能 400 的字段。"""
        r = resolve(None, "max")
        self.assertEqual(r.body, {})
        self.assertEqual(r.extra_body, {})
        self.assertIn("未下发", r.note)


class TestRealProviderShapes(unittest.TestCase):
    """值和字段名都以各家官方文档为准（2026-08 查的，见 effort.py 的表）。"""

    def test_deepseek_three_levels(self):
        self.assertEqual(resolve("deepseek", "low").body, {"reasoning_effort": "low"})
        self.assertEqual(resolve("deepseek", "high").body, {"reasoning_effort": "high"})
        self.assertEqual(resolve("deepseek", "max").body, {"reasoning_effort": "max"})

    def test_deepseek_has_no_medium_and_says_so(self):
        r = resolve("deepseek", "medium")
        self.assertEqual(r.body, {"reasoning_effort": "high"})
        self.assertIn("没有 medium 档", r.note, "取整必须看得见")

    def test_deepseek_off_uses_the_separate_switch(self):
        """DeepSeek 的「关」不是一个档位，是另一个参数。"""
        r = resolve("deepseek", "off")
        self.assertEqual(r.extra_body, {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", r.body)

    def test_kimi_cannot_be_turned_off(self):
        """k3 永远思考 —— 如实回落到最低档，别假装关掉了。"""
        r = resolve("kimi", "off")
        self.assertEqual(r.body, {"reasoning_effort": "low"})
        self.assertIn("关不掉", r.note)

    def test_openai_has_the_full_ladder(self):
        self.assertEqual(resolve("openai", "off").body, {"reasoning_effort": "none"})
        self.assertEqual(resolve("openai", "medium").body, {"reasoning_effort": "medium"})
        self.assertEqual(resolve("openai", "max").body, {"reasoning_effort": "max"})

    def test_gemini_and_xai_have_no_max(self):
        for name in ("gemini", "xai"):
            r = resolve(name, "max")
            self.assertEqual(r.body, {"reasoning_effort": "high"}, name)
            self.assertIn("没有 max 档", r.note, name)

    def test_doubao_minimal_means_no_thinking(self):
        self.assertEqual(resolve("doubao", "off").body, {"reasoning_effort": "minimal"})

    def test_qwen_is_a_switch_in_extra_body(self):
        """通义没有档位，只有开关，而且要走 extra_body。"""
        self.assertEqual(resolve("qwen", "high").extra_body, {"enable_thinking": True})
        self.assertEqual(resolve("qwen", "off").extra_body, {"enable_thinking": False})
        self.assertEqual(resolve("qwen", "high").body, {})

    def test_zhipu_is_a_nested_switch_in_body(self):
        self.assertEqual(resolve("zhipu", "high").body, {"thinking": {"type": "enabled"}})
        self.assertEqual(resolve("zhipu", "off").body, {"thinking": {"type": "disabled"}})

    def test_every_profile_answers_every_level(self):
        """任何一家 × 任何一档都不能抛 —— 挡位是配置，不该让 run 崩掉。"""
        for name in PROFILES:
            for level in LEVELS:
                resolve(name, level)   # 不抛就算过


class TestRoundingDirection(unittest.TestCase):
    def test_rounds_up_when_equidistant(self):
        """够不着时向上取：调低是为了省钱，而省过头的代价比多花点 token 贵。"""
        # gemini 没有 max，只能向下；deepseek 没有 medium，两边等距 → 取 high
        self.assertEqual(resolve("deepseek", "medium").body["reasoning_effort"], "high")


class TestBackendWiring(unittest.TestCase):
    """挡位真的被带进请求里了没有 —— 映射对了但没接上等于白做。"""

    def _backend(self, **over):
        from cowork.llm.openai_compat import OpenAICompatBackend

        kw = dict(base_url="http://localhost:1/v1", api_key="sk-fake",
                  effort_profile="deepseek")
        kw.update(over)
        b = OpenAICompatBackend(**kw)
        b.client = _Fake()
        return b

    def test_architect_and_subagent_use_different_levels(self):
        """两个角色的需求本来就不同，共用一个挡位等于承认这件事无所谓。"""
        from cowork.types import AgentContext, Criterion, TaskClass, TaskSpec

        b = self._backend(architect_effort="max", subagent_effort="low")
        spec = TaskSpec(goal="g", acceptance=[Criterion("c1", "d")],
                        task_class=TaskClass.TOOL_CALL)
        b.client.replies = [_action_json(kind="finish", output_json="{}", summary="s")]
        b.next_step(AgentContext(task_spec=spec))
        self.assertEqual(b.client.calls[-1]["reasoning_effort"], "low")

        b.client.replies = ['{"sufficient": true, "missing": []}']
        b.review_decomposition("目标", [])
        self.assertEqual(b.client.calls[-1]["reasoning_effort"], "max")

    def test_cheap_roles_can_turn_thinking_off(self):
        """分诊 / 探查 / 摘要只判方向，默认就该关掉思考（§3.4）。"""
        b = self._backend(cheap_effort="off")
        b.client.replies = ['{"verdicts":[]}']
        b.triage([_sig()])
        sent = b.client.calls[-1]
        self.assertEqual(sent["extra_body"], {"thinking": {"type": "disabled"}})
        self.assertNotIn("reasoning_effort", sent)

    def test_nothing_is_sent_when_the_provider_did_not_declare_support(self):
        b = self._backend(effort_profile=None, architect_effort="max")
        b.client.replies = ['{"sufficient": true, "missing": []}']
        b.review_decomposition("目标", [])
        sent = b.client.calls[-1]
        self.assertNotIn("reasoning_effort", sent)
        self.assertNotIn("extra_body", sent)

    def test_rounding_note_is_recorded_for_the_operator(self):
        b = self._backend(architect_effort="medium")
        b.client.replies = ['{"sufficient": true, "missing": []}']
        b.review_decomposition("目标", [])
        self.assertTrue(any("取整" in n for n in b.effort_notes), b.effort_notes)


class TestAnthropicWiring(unittest.TestCase):
    def _backend(self, **over):
        from cowork.llm.anthropic_backend import AnthropicBackend

        b = AnthropicBackend(api_key="sk-fake", **over)
        b.client = _FakeAnthropic()
        return b

    def test_effort_lands_in_output_config(self):
        b = self._backend(effort="low")
        b.review_decomposition("目标", [])
        self.assertEqual(b.client.calls[-1]["output_config"]["effort"], "low")

    def test_max_folds_into_high(self):
        """Anthropic 没有 max 档 —— 向下并到 high，而不是原样发过去。"""
        b = self._backend(effort="max")
        b.review_decomposition("目标", [])
        self.assertEqual(b.client.calls[-1]["output_config"]["effort"], "high")

    def test_off_means_no_thinking_block(self):
        b = self._backend(effort="off")
        b.review_decomposition("目标", [])
        self.assertNotIn("thinking", b.client.calls[-1])


def _sig():
    from cowork.signals import SignalSource, SignalType
    from cowork.types import Signal

    return Signal(type=SignalType.AMBIGUITY, task_id="t1", source=SignalSource.SUBAGENT)


def _action_json(**over) -> str:
    """一条合规的动作回复。

    **按 ACTION_SCHEMA 的 required 自动补全**，不要手写字段列表 ——
    加一个工具就要加参数字段，手写的固定回复会在那时静默过期
    （这条测试就这么红过一次）。
    """
    import json

    from cowork.llm.anthropic_backend import ACTION_SCHEMA

    blank: dict = {}
    for key in ACTION_SCHEMA["required"]:
        kind = ACTION_SCHEMA["properties"][key]["type"]
        blank[key] = {"string": "", "array": [], "boolean": False}[kind]
    blank.update(over)
    return json.dumps(blank, ensure_ascii=False)


class _Fake:
    def __init__(self):
        self.calls = []
        self.replies = []
        self.chat = self

    @property
    def completions(self):
        return self

    def create(self, **kwargs):
        self.calls.append(kwargs)
        text = self.replies.pop(0) if self.replies else "{}"
        msg = type("M", (), {"content": text})()
        choice = type("C", (), {"message": msg, "finish_reason": "stop"})()
        return type("R", (), {"choices": [choice], "usage": None})()


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
        return type("R", (), {"content": [block], "usage": self.usage,
                              "stop_reason": "end_turn"})()


if __name__ == "__main__":
    unittest.main()

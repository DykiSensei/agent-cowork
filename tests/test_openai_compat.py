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

    def test_architect_uses_reasoning_model(self):
        """§4.1「不同模型干擅长的事」：架构师和 Subagent 不该是同一档。"""
        from cowork.cli import _make_backend

        ds = _make_backend("deepseek")
        self.assertEqual(ds.architect_model, "deepseek-reasoner")
        self.assertEqual(ds.subagent_model, "deepseek-chat")


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
        """M1.3 出口：demo 场景用真实模型跑通。"""
        from cowork import demo
        from cowork.types import TaskStatus

        orch, ws = demo.build(backend=self.backend)
        orch.log = lambda _m: None
        try:
            result = orch.run(max_cycles=4)
            self.assertIn(
                result.state.status,
                (TaskStatus.COMPLETED, TaskStatus.AWAITING_HUMAN),
                f"不该崩也不该无限中断；决策记录：{[d.action.value for d in result.decisions]}",
            )
            self.assertGreater(result.state.tokens_used, 0)
        finally:
            shutil.rmtree(ws, ignore_errors=True)


if __name__ == "__main__":
    unittest.main()

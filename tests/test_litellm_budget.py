"""M1.4：virtual key 的预算拒绝要能落成 BUDGET_EXCEEDED 硬信号。

出口标准是「超预算时 LiteLLM 侧真实拒绝，验证 §7.2 成本兜底不只是应用层软限制」。
所以这组测试打的是**真实代理**，不是 mock —— mock 掉的恰好是唯一需要验证的东西。

不需要有效的 ANTHROPIC_API_KEY：LiteLLM 的预算检查发生在转发上游之前。

前置：docker compose up -d litellm
"""

import json
import unittest
import urllib.error
import urllib.request

from cowork.llm.errors import (
    BudgetExceeded,
    ModelCallFailed,
    classify_provider_error,
)

PROXY = "http://localhost:4000"
MASTER_KEY = "sk-cowork-master"


def _post(path: str, payload: dict, key: str) -> tuple[int, str]:
    req = urllib.request.Request(
        f"{PROXY}{path}",
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {key}",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode()
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode()


def _proxy_available() -> bool:
    try:
        with urllib.request.urlopen(f"{PROXY}/health/liveliness", timeout=5) as r:
            return r.status == 200
    except Exception:
        return False


class TestClassifier(unittest.TestCase):
    """分类器的单元测试 —— 用的是下面实测抓到的真实错误体。"""

    REAL_BUDGET_ERROR = json.dumps(
        {
            "error": {
                "message": "Budget has been exceeded! Key=cowork-task-demo "
                "(sk-...Dwfw) Current cost: 1.0, Max budget: 0.05",
                "type": "budget_exceeded",
                "param": None,
                "code": "429",
            }
        }
    )
    REAL_AUTH_ERROR = json.dumps(
        {
            "error": {
                "message": '{"type":"error","error":{"type":"authentication_error",'
                '"message":"invalid x-api-key"}}. Received Model Group=claude-haiku-4-5',
                "type": "None",
                "code": "401",
            }
        }
    )

    def test_budget_error_maps_to_budget_exceeded(self):
        self.assertIs(classify_provider_error(429, self.REAL_BUDGET_ERROR), BudgetExceeded)

    def test_auth_error_does_not_map_to_budget(self):
        self.assertIs(classify_provider_error(401, self.REAL_AUTH_ERROR), ModelCallFailed)

    def test_plain_rate_limit_is_not_budget(self):
        """预算拒绝和真实限流同为 429 —— 不能靠状态码判断。"""
        body = json.dumps({"error": {"type": "rate_limit_error", "message": "slow down"}})
        self.assertIs(classify_provider_error(429, body), ModelCallFailed)

    def test_signal_type_is_hard(self):
        from cowork.signals import HARD_SIGNALS

        self.assertIn(BudgetExceeded.signal_type, HARD_SIGNALS)
        self.assertIn(ModelCallFailed.signal_type, HARD_SIGNALS)


@unittest.skipUnless(_proxy_available(), f"LiteLLM 代理不可达 ({PROXY})")
class TestLiveBudgetEnforcement(unittest.TestCase):
    """打真实代理：证明拒绝发生在代理侧，而不是应用层自己数数。"""

    @classmethod
    def setUpClass(cls):
        status, body = _post(
            "/key/generate",
            {
                "models": ["claude-haiku-4-5"],
                "max_budget": 0.05,
                "key_alias": "cowork-budget-test",
            },
            MASTER_KEY,
        )
        assert status == 200, f"建 key 失败: {status} {body}"
        cls.key = json.loads(body)["key"]

    @classmethod
    def tearDownClass(cls):
        _post("/key/delete", {"keys": [cls.key]}, MASTER_KEY)

    def test_budget_rejection_is_classified_as_budget_exceeded(self):
        # 把 spend 顶到预算之上（真实用量累积需要花钱，这里直接改账）
        status, _ = _post("/key/update", {"key": self.key, "spend": 1.0}, MASTER_KEY)
        self.assertEqual(status, 200)

        status, body = _post(
            "/v1/messages",
            {
                "model": "claude-haiku-4-5",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
            self.key,
        )

        self.assertEqual(status, 429, body)
        self.assertIs(
            classify_provider_error(status, body),
            BudgetExceeded,
            f"代理返回的预算错误没被识别出来: {body}",
        )
        # 拒绝必须发生在转发上游之前 —— 否则「兜底」就已经花钱了
        self.assertNotIn("request_id", body, "不应该已经打到上游")

    def test_messages_endpoint_forwards_upstream(self):
        """/v1/messages 是真转发，不是桩。

        上游 key 是占位符，所以预期拿回 Anthropic 原生的鉴权错误 ——
        这恰好证明「官方 SDK + base_url 指向代理」这条路是通的。
        """
        status, body = _post(
            "/v1/messages",
            {
                "model": "claude-haiku-4-5",
                "max_tokens": 16,
                "messages": [{"role": "user", "content": "hi"}],
            },
            MASTER_KEY,
        )
        if status == 200:
            self.skipTest("上游 key 有效，跳过占位符断言")
        self.assertEqual(status, 401, body)
        self.assertIn("authentication_error", body)
        self.assertIs(classify_provider_error(status, body), ModelCallFailed)


if __name__ == "__main__":
    unittest.main()

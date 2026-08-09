"""密钥加载与脱敏。

脱敏这条不是洁癖：signals.raw_evidence 存的是第三方错误体，
内容我们控制不了，而它会长期留在 Postgres 里。
"""

import os
import tempfile
import unittest
from pathlib import Path

from cowork.config import load_env, parse_env, redact


class TestParseEnv(unittest.TestCase):
    def test_basic(self):
        got = parse_env("A=1\nB=hello world\n")
        self.assertEqual(got, {"A": "1", "B": "hello world"})

    def test_comments_and_blanks(self):
        got = parse_env("# 注释\n\n  # 缩进注释\nA=1\n")
        self.assertEqual(got, {"A": "1"})

    def test_quotes_stripped(self):
        got = parse_env('A="sk-abc"\nB=\'sk-def\'\n')
        self.assertEqual(got, {"A": "sk-abc", "B": "sk-def"})

    def test_export_prefix_tolerated(self):
        self.assertEqual(parse_env("export A=1\n"), {"A": "1"})

    def test_value_containing_equals(self):
        got = parse_env("DSN=postgresql://u:p@h:5433/db?x=1\n")
        self.assertEqual(got["DSN"], "postgresql://u:p@h:5433/db?x=1")


class TestLoadEnv(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = self.tmp / ".env"
        self._saved = {k: os.environ.get(k) for k in ("COWORK_T1", "COWORK_T2")}

    def tearDown(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_loads_and_returns_key_names_only(self):
        self.f.write_text("COWORK_T1=sk-secret\n", encoding="utf-8")
        applied = load_env(self.f)
        self.assertEqual(applied, ["COWORK_T1"])
        self.assertEqual(os.environ["COWORK_T1"], "sk-secret")
        # 返回值只有键名，调用方打日志时不会漏出值
        self.assertNotIn("sk-secret", "".join(applied))

    def test_real_env_wins(self):
        """容器 / CI 里用环境变量覆盖，不应被文件顶掉。"""
        os.environ["COWORK_T1"] = "from-env"
        self.f.write_text("COWORK_T1=from-file\n", encoding="utf-8")
        load_env(self.f)
        self.assertEqual(os.environ["COWORK_T1"], "from-env")

    def test_empty_value_does_not_clobber(self):
        """.env.example 里的空占位不该覆盖真实环境变量。"""
        os.environ["COWORK_T1"] = "real"
        self.f.write_text("COWORK_T1=\n", encoding="utf-8")
        load_env(self.f)
        self.assertEqual(os.environ["COWORK_T1"], "real")

    def test_missing_file_is_not_an_error(self):
        self.assertEqual(load_env(self.tmp / "nope.env"), [])


class TestRedact(unittest.TestCase):
    def test_sk_key_is_redacted(self):
        out = redact("Error: invalid key sk-abc123DEF456ghi789 at line 1")
        self.assertNotIn("sk-abc123DEF456ghi789", out)
        self.assertIn("REDACTED", out)

    def test_bearer_token(self):
        out = redact("Authorization: Bearer eyJhbGciOiJIUzI1NiJ9.payload")
        self.assertNotIn("eyJhbGciOiJIUzI1NiJ9", out)

    def test_json_api_key_field(self):
        out = redact('{"api_key": "dsk-9f8e7d6c5b4a3210"}')
        self.assertNotIn("9f8e7d6c5b4a3210", out)

    def test_keeps_surrounding_diagnostics(self):
        """脱敏不能把诊断信息一起抹掉 —— 架构师要靠证据做决策。"""
        out = redact(
            "Budget has been exceeded! Key=cowork-task (sk-abcdefgh1234) "
            "Current cost: 1.0, Max budget: 0.05"
        )
        self.assertIn("Budget has been exceeded", out)
        self.assertIn("Current cost: 1.0", out)
        self.assertNotIn("sk-abcdefgh1234", out)

    def test_none_and_empty(self):
        self.assertIsNone(redact(None))
        self.assertEqual(redact(""), "")


class TestSignalEvidenceIsRedacted(unittest.TestCase):
    def test_bus_redacts_on_emit(self):
        """所有信号的唯一入口，脱敏放在这一处。"""
        from cowork.runtime.bus import SignalBus
        from cowork.signals import SignalType

        bus = SignalBus()
        sig = bus.emit_hard(
            SignalType.TOOL_FAILURE,
            "task_x",
            evidence="401 unauthorized, key=sk-live12345678abcd",
        )
        self.assertNotIn("sk-live12345678abcd", sig.raw_evidence)
        self.assertIn("401 unauthorized", sig.raw_evidence)


if __name__ == "__main__":
    unittest.main()

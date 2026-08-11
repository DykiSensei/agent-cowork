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


class TestUpdateEnvWritesBack(unittest.TestCase):
    """设置页写 .env（`server/settings_io.update_env`）。

    这里不需要 fastapi —— 写文件那半是纯函数，而它正是密钥纪律的落点。
    """

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.f = self.tmp / ".env"
        self._saved = os.environ.get("COWORK_ENV_FILE")
        os.environ["COWORK_ENV_FILE"] = str(self.f)
        self._touched = ["COWORK_T_KEY", "COWORK_LLM_BASE_URL"]
        self._before = {k: os.environ.get(k) for k in self._touched}

    def tearDown(self):
        if self._saved is None:
            os.environ.pop("COWORK_ENV_FILE", None)
        else:
            os.environ["COWORK_ENV_FILE"] = self._saved
        for k, v in self._before.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_existing_key_is_replaced_in_place(self):
        from cowork.server.settings_io import update_env

        self.f.write_text("# 注释保留\nCOWORK_T_KEY=old\nOTHER=1\n", encoding="utf-8")
        update_env({"COWORK_T_KEY": "new"})
        text = self.f.read_text(encoding="utf-8")

        self.assertIn("# 注释保留", text)
        self.assertEqual(parse_env(text)["COWORK_T_KEY"], "new")
        self.assertEqual(text.count("COWORK_T_KEY"), 1, "不该多写一行")

    def test_export_prefixed_line_is_replaced_not_duplicated(self):
        """`config.parse_env` 容忍 `export KEY=`，写回这边也必须认得出来。

        不认的话每存一次设置就在文件末尾多一行同名 KEY —— 靠「后面的赢」结果
        碰巧还是对的，但几轮之后没人看得懂这份 .env 到底哪一行在生效。
        """
        from cowork.server.settings_io import update_env

        self.f.write_text("export COWORK_T_KEY=old\n", encoding="utf-8")
        update_env({"COWORK_T_KEY": "new"})
        text = self.f.read_text(encoding="utf-8")

        self.assertEqual(text.count("COWORK_T_KEY"), 1, text)
        self.assertEqual(parse_env(text)["COWORK_T_KEY"], "new")

    def test_newline_in_value_is_refused(self):
        """值里一个换行 = 往 .env 多写一行 = 任意环境变量注入。"""
        from cowork.server.settings_io import update_env

        self.f.write_text("", encoding="utf-8")
        with self.assertRaises(ValueError):
            update_env(
                {"COWORK_T_KEY": "sk-x\nCOWORK_LLM_BASE_URL=http://攻击者/"}
            )
        self.assertNotIn("攻击者", self.f.read_text(encoding="utf-8"))


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

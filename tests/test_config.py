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


class TestEnvExampleCoversEverySetting(unittest.TestCase):
    """设置页能写的每一个环境变量，`.env.example` 里都要有一行。

    这是实测撞到的一类漂移：M10 加了三个 `COWORK_*_PROVIDER`（按角色选供应商），
    代码、设置页、CLAUDE.md 都有，**只有 `.env.example` 漏了整整一个里程碑** ——
    而那份文件是不用界面的人唯一会打开的那一份。同 §11.20 那条：
    「契约写了什么，就要有一条从调用方那侧发起的检查」，这里的调用方是人。
    """

    def test_every_writable_key_has_a_line(self):
        from pathlib import Path

        from cowork.server.settings_io import GLOBAL_KEYS, SEARCH_KEY_ENV

        text = (Path(__file__).resolve().parents[1] / ".env.example").read_text(
            encoding="utf-8"
        )
        declared = {
            line.split("=", 1)[0].strip()
            for line in text.splitlines()
            if "=" in line and not line.lstrip().startswith("#")
        }
        missing = sorted(
            {*GLOBAL_KEYS.values(), SEARCH_KEY_ENV} - declared
        )
        self.assertEqual(missing, [], f".env.example 漏了这些设置项: {missing}")


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


class TestMultilineSettings(unittest.TestCase):
    """自定义提示词是多行的，而 `.env` 是一行一个 KEY=value（M11）。

    换行必须转义成字面 `\\n` 再写 —— 直接写会多出一行，那正是设置页那条
    注入防线挡的形态（一次「设置提示词」的请求顺手改掉 `COWORK_LLM_BASE_URL`）。
    """

    def test_newlines_survive_a_round_trip(self):
        from cowork.config import decode_multiline, encode_multiline

        name = "COWORK_SUBAGENT_PROMPT"
        text = "第一行\n第二行\n\n带空行的第四行"
        stored = encode_multiline(name, text)
        self.assertNotIn("\n", stored, "存进 .env 的值里不能有真换行")
        self.assertEqual(decode_multiline(name, stored), text)

    def test_backslashes_are_not_eaten(self):
        """Windows 路径和正则里全是反斜杠，转义必须可逆。"""
        from cowork.config import decode_multiline, encode_multiline

        name = "COWORK_ARCHITECT_PROMPT"
        text = "用 C:\\work\\out 这个目录\n正则写成 \\d+"
        self.assertEqual(
            decode_multiline(name, encode_multiline(name, text)), text
        )

    def test_only_the_prompt_keys_are_transformed(self):
        """别的变量一个字都不能动 —— key 里恰好有 `\\n` 两个字符也不该被改。"""
        from cowork.config import decode_multiline, encode_multiline

        for name in ("COWORK_LLM_BASE_URL", "ZHIPUAI_API_KEY"):
            raw = "abc\\ndef"
            self.assertEqual(encode_multiline(name, raw), raw)
            self.assertEqual(decode_multiline(name, raw), raw)


class TestNoTokenCap(unittest.TestCase):
    """**默认不发 `max_tokens`**（M11）。

    任何猜出来的数字都会在「这次想得多」的时候把正文挤没：thinking 和正文在
    同一个额度里竞争，而 Subagent 默认开着思考。成本改看界面上的 token 计数。
    """

    def _kwargs(self, **backend_kw) -> dict:
        """抓一次真实请求的 kwargs，不打网络。"""
        from unittest import mock

        from cowork.llm.openai_compat import OpenAICompatBackend

        be = OpenAICompatBackend(
            base_url="http://x/v1", api_key="k", architect_model="m",
            subagent_model="m", triage_model="m", **backend_kw,
        )
        captured: dict = {}

        class _Resp:
            choices = [
                type("C", (), {
                    "message": type("M", (), {"content": '{"verdicts": []}'})(),
                    "finish_reason": "stop",
                })()
            ]
            usage = None

        def fake_create(**kw):
            captured.update(kw)
            return _Resp()

        with mock.patch.object(be.client.chat.completions, "create", fake_create):
            be._call(
                model="m", system="s", user="u",
                schema={"type": "object", "properties": {}},
            )
        return captured

    def test_max_tokens_is_not_sent_by_default(self):
        self.assertNotIn(
            "max_tokens", self._kwargs(),
            "默认不该给端点设上限 —— 猜出来的数字会把正文挤没",
        )

    def test_an_explicit_cap_is_still_honoured(self):
        """想设仍然设得上：这是取消默认值，不是删掉能力。"""
        self.assertEqual(self._kwargs(max_tokens=1234).get("max_tokens"), 1234)

    def test_zero_is_treated_as_unlimited_not_as_zero(self):
        """**不发 ≠ 发 0**：后者在多数端点上是「一个 token 都不许生成」。"""
        self.assertNotIn("max_tokens", self._kwargs(max_tokens=0))


class TestRolePromptExtra(unittest.TestCase):
    """追加而不是替换 —— 内置提示词里带着输出契约和工具清单。"""

    def test_no_config_means_byte_identical_prompt(self):
        """没配的时候**一个字都不能变**：变了缓存前缀就换了，命中率归零。"""
        import os
        from unittest import mock

        from cowork.llm.prompts import with_extra

        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("COWORK_SUBAGENT_PROMPT", None)
            self.assertEqual(with_extra("原样", "subagent"), "原样")

    def test_extra_is_appended_after_the_builtin(self):
        import os
        from unittest import mock

        from cowork.llm.prompts import with_extra

        with mock.patch.dict(os.environ, {"COWORK_SUBAGENT_PROMPT": "注释写中文"}):
            out = with_extra("内置提示词", "subagent")
        self.assertTrue(out.startswith("内置提示词"), "自定义必须在内置之后")
        self.assertIn("注释写中文", out)
        self.assertIn("以上面的为准", out, "冲突时的优先级要写明")

    def test_stored_newlines_are_restored(self):
        """设置页存的是转义过的，喂给模型时必须是真换行。"""
        import os
        from unittest import mock

        from cowork.llm.prompts import role_extra

        with mock.patch.dict(os.environ, {"COWORK_ARCHITECT_PROMPT": "一行\\n二行"}):
            self.assertEqual(role_extra("architect"), "一行\n二行")


if __name__ == "__main__":
    unittest.main()

"""工作区：产物落在哪，以及接手时那儿已经有什么（§12 M10）。

两件事在这里钉住：

1. **「我的产物在哪」必须有答案。** 原来没配工作区就 `tempfile.mkdtemp()`，
   任务跑完东西在一个随机命名的临时目录里，界面也不显示路径。
2. **接手已有项目和从零开始是两件事。** 差别不在参数上，在于架构师**知不知道
   那儿已经有东西** —— 不告诉它，它会把一个有内容的目录当空目录重建一遍。
"""

import shutil
import tempfile
import unittest
from pathlib import Path

from cowork import workspace


class TestResolve(unittest.TestCase):
    """路径在**起跑之前**校验：别等到 Subagent 写第一个文件才发现目录不能用。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="cowork-ws-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_absolute_path_passes_through(self):
        got = workspace.resolve_workspace(str(self.tmp))
        self.assertEqual(got, Path(str(self.tmp)))

    def test_user_home_is_expanded(self):
        got = workspace.resolve_workspace("~/cowork-x")
        self.assertTrue(got.is_absolute())
        self.assertNotIn("~", str(got))

    def test_quotes_are_tolerated(self):
        """从资源管理器复制路径会带引号 —— 那不是用户的错。"""
        got = workspace.resolve_workspace(f'"{self.tmp}"')
        self.assertEqual(got, Path(str(self.tmp)))

    def test_relative_path_is_refused(self):
        """服务进程的 cwd 不是用户以为的那个，`./out` 会落在谁也找不到的地方。"""
        with self.assertRaises(ValueError) as cm:
            workspace.resolve_workspace("./out")
        self.assertIn("绝对路径", str(cm.exception))

    def test_filesystem_root_is_refused(self):
        root = Path(str(self.tmp.anchor))
        with self.assertRaises(ValueError):
            workspace.resolve_workspace(str(root))

    def test_a_file_is_not_a_workspace(self):
        f = self.tmp / "a.txt"
        f.write_text("x", encoding="utf-8")
        with self.assertRaises(ValueError):
            workspace.resolve_workspace(str(f))

    def test_typo_that_would_create_a_whole_tree_is_refused(self):
        with self.assertRaises(ValueError) as cm:
            workspace.resolve_workspace(str(self.tmp / "nope" / "deeper"))
        self.assertIn("上一级目录不存在", str(cm.exception))

    def test_empty_is_refused(self):
        with self.assertRaises(ValueError):
            workspace.resolve_workspace("   ")


class TestLayout(unittest.TestCase):
    def test_new_task_gets_its_own_subdirectory(self):
        root = Path("/tmp/ws") if Path("/tmp").exists() else Path("C:/ws")
        got = workspace.task_workspace(root, "task_abc", takeover=False)
        self.assertEqual(got, root / "task_abc")

    def test_takeover_writes_into_the_directory_itself(self):
        """接手时落进子目录的话，改的就不是人手上那份代码，而是它的拷贝。"""
        root = Path("/tmp/ws") if Path("/tmp").exists() else Path("C:/ws")
        got = workspace.task_workspace(root, "task_abc", takeover=True)
        self.assertEqual(got, root)

    def test_default_root_is_somewhere_a_person_can_find(self):
        got = workspace.default_root()
        self.assertTrue(got.is_absolute())
        self.assertNotIn("Temp", str(got))
        self.assertNotIn("tmp", str(got).lower().replace(str(Path.home()).lower(), ""))


class TestSnapshot(unittest.TestCase):
    def setUp(self):
        self.ws = Path(tempfile.mkdtemp(prefix="cowork-snap-"))
        (self.ws / "src").mkdir()
        (self.ws / "src" / "app.py").write_text("print(1)", encoding="utf-8")
        (self.ws / "README.md").write_text("# hi", encoding="utf-8")
        # 这些不该出现在清单里
        (self.ws / ".git").mkdir()
        (self.ws / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")
        (self.ws / "node_modules").mkdir()
        (self.ws / "node_modules" / "big.js").write_text("x" * 100, encoding="utf-8")
        (self.ws / ".env").write_text("SECRET=1", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.ws, ignore_errors=True)

    def test_lists_real_files_with_sizes(self):
        got = workspace.snapshot(self.ws)
        paths = [e["path"] for e in got]
        self.assertEqual(paths, ["README.md", "src/app.py"])
        self.assertEqual(dict(zip(paths, [e["bytes"] for e in got]))["src/app.py"], 8)

    def test_tooling_noise_is_skipped(self):
        """漏掉一两个的代价是清单长一点；错杀的代价是看不见真正的代码。"""
        paths = [e["path"] for e in workspace.snapshot(self.ws)]
        for noise in (".git/HEAD", "node_modules/big.js", ".env"):
            self.assertNotIn(noise, paths)

    def test_secrets_do_not_leak_into_the_prompt(self):
        """`.env` 就在工作区里的情况太常见了 —— 点开头的一律不列。"""
        text = workspace.render_snapshot(workspace.snapshot(self.ws))
        self.assertNotIn(".env", text)

    def test_empty_directory_renders_to_nothing(self):
        empty = Path(tempfile.mkdtemp(prefix="cowork-empty-"))
        try:
            self.assertEqual(workspace.snapshot(empty), [])
            self.assertEqual(workspace.render_snapshot([]), "")
        finally:
            shutil.rmtree(empty, ignore_errors=True)

    def test_missing_directory_is_not_an_error(self):
        self.assertEqual(workspace.snapshot(self.ws / "nope"), [])

    def test_entry_cap_is_enforced(self):
        many = Path(tempfile.mkdtemp(prefix="cowork-many-"))
        try:
            for i in range(workspace.MAX_ENTRIES + 40):
                (many / f"f{i:04d}.txt").write_text("x", encoding="utf-8")
            got = workspace.snapshot(many)
            self.assertEqual(len(got), workspace.MAX_ENTRIES)
            self.assertIn("清单已截断", workspace.render_snapshot(got))
        finally:
            shutil.rmtree(many, ignore_errors=True)

    def test_render_says_this_is_not_a_fresh_start(self):
        """措辞是这段文本的全部作用：模型看到文件清单的默认读法是「参考资料」。"""
        text = workspace.render_snapshot(workspace.snapshot(self.ws))
        self.assertIn("不是一个空目录", text)
        self.assertIn("已经存在", text)
        self.assertIn("不要再拆一个子任务去重做", text)


if __name__ == "__main__":
    unittest.main()

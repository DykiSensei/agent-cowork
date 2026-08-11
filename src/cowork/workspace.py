"""工作区：产物落在哪，以及接手时那里已经有什么。

**这个模块里没有 LLM，也不该有**。它回答的全是确定性问题：这个路径能不能用、
产物该落在哪一级、目录里现在有什么。架构师负责「照着现状怎么拆」，
这里只负责把现状如实摆出来。

两种起法（§12 M10）：

    new       从零开始 → 产物落在 <工作区>/<任务id>/，互不干扰
    takeover  接手已有项目 → **直接写进你选的那个目录**，否则改不到已有文件

`takeover` 是这两种里唯一需要额外信息的：架构师必须先知道那儿已经有什么，
否则它会把一个有内容的目录当空目录，从零重建一遍 —— 而那正是「半路接手」
和「从零开始」的全部区别。
"""

from __future__ import annotations

import os
from pathlib import Path

# 不该出现在现状清单里的东西：它们要么是工具生成的，要么大到没有意义。
# 漏掉一两个的代价只是清单长一点；错杀的代价是架构师看不见真正的代码。
SKIP_DIRS = frozenset(
    {
        ".git", ".hg", ".svn", ".idea", ".vscode",
        "node_modules", "__pycache__", ".pytest_cache", ".mypy_cache", ".ruff_cache",
        "venv", ".venv", "env", "dist", "build", "target", ".next", ".cache",
    }
)

# 清单的规模上限。给架构师看的是**结构**，不是全文 —— 一个几千文件的仓库
# 逐条列出去只会挤掉提示词里真正重要的部分（同 §8「传引用不传全文」）。
MAX_ENTRIES = 200
MAX_DEPTH = 4


class WorkspaceError(ValueError):
    """路径不能用。**在起跑之前抛**，别等到 Subagent 写第一个文件才发现。"""


def resolve_workspace(raw: str) -> Path:
    """把用户填的路径变成一个能用的绝对路径。

    这个服务是**本机单人**的（`server/bind.py` 硬拦非回环），所以「让人指定
    一个本机目录」是合理的。但仍然要挡住几类明显不该发生的：

    - 相对路径：服务进程的 cwd 不是用户以为的那个，`./out` 会落在谁也找不到的地方
    - 文件系统根：scope 限制的是**写哪些文件**，不是「写在哪一层」——
      把根目录当工作区，一次越界就是系统盘
    - 指向一个已存在的文件：那不是目录
    - 父目录都不存在：多半是打错了，而不是真想造一棵新树
    """
    text = (raw or "").strip().strip('"')
    if not text:
        raise WorkspaceError("工作区不能为空")
    path = Path(os.path.expandvars(os.path.expanduser(text)))
    if not path.is_absolute():
        raise WorkspaceError(
            f"要一个绝对路径（收到 {text!r}）—— 相对路径会落在服务进程的当前目录下，"
            "那多半不是你想放东西的地方"
        )
    path = Path(os.path.normpath(str(path)))
    if path.parent == path:
        raise WorkspaceError(f"不能把文件系统根目录当工作区：{path}")
    if path.exists() and not path.is_dir():
        raise WorkspaceError(f"这是一个文件，不是目录：{path}")
    if not path.exists() and not path.parent.is_dir():
        raise WorkspaceError(
            f"上一级目录不存在：{path.parent} —— 检查一下路径是不是打错了"
        )
    return path


def default_root() -> Path:
    """没配过工作区时东西落在哪。

    **必须是一个人找得到的地方。** 原来 serve 用 `tempfile.mkdtemp()` ——
    任务跑完了，产物在一个随机命名的临时目录里，界面上也不显示路径，
    于是「我的产物在哪」这个问题在这套系统里没有答案（实测反馈的原话）。
    """
    return Path.home() / "cowork-workspaces"


def task_workspace(root: Path, task_id: str, *, takeover: bool) -> Path:
    """这一次任务实际写到哪一级。

    接手已有项目时**直接用那个目录**：产物落进子目录的话，改的就不是人手上
    那份代码，而是它的一份拷贝 —— 那不叫接手。
    """
    return root if takeover else root / task_id


def ensure(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    return path


def snapshot(root: Path, *, max_entries: int = MAX_ENTRIES,
             max_depth: int = MAX_DEPTH) -> list[dict]:
    """工作区现状：相对路径 + 字节数，按路径排序。

    只给**结构**不给内容：内容由 Subagent 自己 `read_file`（它本来就能读整个
    workspace），架构师要的是「这儿已经有什么」这一个事实。
    """
    if not root.is_dir():
        return []
    out: list[dict] = []
    root = root.resolve()
    for dirpath, dirnames, filenames in os.walk(root):
        here = Path(dirpath)
        depth = len(here.relative_to(root).parts)
        if depth >= max_depth:
            dirnames[:] = []
        else:
            dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS
                                 and not d.startswith("."))
        for name in sorted(filenames):
            if name.startswith("."):
                continue
            f = here / name
            try:
                size = f.stat().st_size
            except OSError:
                continue
            out.append({"path": f.relative_to(root).as_posix(), "bytes": size})
            if len(out) >= max_entries:
                return sorted(out, key=lambda e: e["path"])
    return sorted(out, key=lambda e: e["path"])


def render_snapshot(entries: list[dict], *, root_goal: str = "") -> str:
    """现状清单 → 喂给架构师的那段文本。

    措辞刻意直白地说明**这不是从零开始**：模型看到一份文件清单时，默认的
    读法是「参考资料」，而这里的意思是「这些东西已经存在，你是来接着做的」。
    """
    if not entries:
        return ""
    lines = "\n".join(f"- {e['path']}（{e['bytes']} 字节）" for e in entries)
    more = "\n（清单已截断）" if len(entries) >= MAX_ENTRIES else ""
    return (
        "# 工作区现状 —— 这不是一个空目录\n"
        "下面这些文件**已经存在**，任务是在它们的基础上继续，不是从零重建：\n"
        f"{lines}{more}\n\n"
        "因此拆解时：\n"
        "- 已经做好的部分不要再拆一个子任务去重做；\n"
        "- 要改哪个已有文件，就把它写进那个子任务的 scope；\n"
        "- 不要把已有文件整份重写，除非目标就是重写它。"
    )

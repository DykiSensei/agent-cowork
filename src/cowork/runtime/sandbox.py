"""沙箱与工具执行。不含任何 LLM —— 这是整个设计里唯一完全可信的组件（§2.1）。

v0.1 用本地子进程 + 路径白名单，只为验证 SCOPE_VIOLATION 语义（§10.4）。
use_docker=True 时改走 `docker run --network none`，接口不变，规模化时替换实现即可。
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from ..types import SandboxProfile


class ScopeViolation(Exception):
    """尝试访问 TaskSpec.scope 之外的资源。

    兼作安全边界和跑偏探测器：Subagent 开始碰不该碰的东西，
    通常意味着它已经偏离了任务理解（§3.2 设计注记）。
    """

    def __init__(self, resource: str, reason: str) -> None:
        super().__init__(f"{reason}: {resource}")
        self.resource = resource
        self.reason = reason


@dataclass
class ToolResult:
    ok: bool
    stdout: str = ""
    stderr: str = ""
    exit_code: int = 0
    detail: str = ""


class Sandbox:
    def __init__(self, profile: SandboxProfile, scope: list[str]) -> None:
        self.profile = profile
        self.scope = list(scope)
        self.root = Path(profile.workspace).resolve()
        self.root.mkdir(parents=True, exist_ok=True)

    # -- scope ------------------------------------------------------------- #

    def _resolve_in_scope(self, rel_path: str, *, write: bool) -> Path:
        target = (self.root / rel_path).resolve()
        try:
            rel = target.relative_to(self.root)
        except ValueError:
            raise ScopeViolation(rel_path, "路径逃逸出 workspace") from None
        posix = rel.as_posix()
        if write and not any(fnmatch.fnmatch(posix, pat) for pat in self.scope):
            raise ScopeViolation(posix, f"不在 TaskSpec.scope {self.scope} 内")
        return target

    # -- tools ------------------------------------------------------------- #

    def write_file(self, path: str, content: str) -> ToolResult:
        target = self._resolve_in_scope(path, write=True)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        return ToolResult(ok=True, detail=f"wrote {len(content)} chars to {path}")

    def read_file(self, path: str) -> ToolResult:
        target = self._resolve_in_scope(path, write=False)
        if not target.exists():
            return ToolResult(ok=False, exit_code=1, stderr=f"no such file: {path}")
        return ToolResult(ok=True, stdout=target.read_text(encoding="utf-8"))

    def run(self, command: list[str], timeout: float = 60.0) -> ToolResult:
        if not command:
            return ToolResult(ok=False, exit_code=2, stderr="empty command")
        binary = Path(command[0]).name.removesuffix(".exe")
        if binary not in self.profile.allowed_binaries:
            raise ScopeViolation(
                command[0],
                f"不在 allowed_binaries {list(self.profile.allowed_binaries)}",
            )
        argv = self._wrap(command)
        try:
            proc = subprocess.run(
                argv,
                cwd=str(self.root),
                capture_output=True,
                text=True,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired:
            return ToolResult(ok=False, exit_code=124, stderr="command timed out")

        result = ToolResult(
            ok=proc.returncode == 0,
            stdout=proc.stdout,
            stderr=proc.stderr,
            exit_code=proc.returncode,
        )

        # 容器边界拒绝的写入，是越界而不是普通工具失败 —— 提级为 SCOPE_VIOLATION，
        # 否则架构师看到的只是一个语焉不详的 TOOL_FAILURE。
        if self.profile.use_docker and not result.ok:
            hit = _readonly_marker(result.stderr)
            if hit:
                raise ScopeViolation(
                    _guess_path(result.stderr) or "?",
                    f"容器只读挂载拒绝写入（{hit}）",
                )
        return result

    # -- docker ------------------------------------------------------------ #

    def _wrap(self, command: list[str]) -> list[str]:
        if not self.profile.use_docker:
            if command[0] in ("python", "python3"):
                return [sys.executable, *command[1:]]
            return command

        # 整个 workspace 只读挂载，再把 scope 内的具体路径以可写方式覆盖上去。
        # 这样即使 run 执行任意代码绕过工具层，scope 外的资源在内核层面也写不动。
        mounts: list[str] = ["-v", f"{_host(self.root)}:/w:ro"]
        for rel in self._concrete_scope_paths():
            mounts += ["-v", f"{_host(self.root / rel)}:/w/{rel}"]

        return [
            "docker", "run", "--rm",
            "--network", self.profile.network,
            *mounts,
            "-w", "/w",
            self.profile.image,
            *command,
        ]

    def _concrete_scope_paths(self) -> list[str]:
        """把 scope 模式展开成可挂载的具体路径。

        bind mount 要求源文件已存在，所以无通配符的 scope 项若不存在会先建空文件。
        这些路径本就在 scope 内，创建它们不构成越界。
        """
        out: list[str] = []
        for pat in self.scope:
            if any(ch in pat for ch in "*?["):
                out.extend(
                    p.relative_to(self.root).as_posix()
                    for p in self.root.glob(pat)
                    if p.is_file()
                )
            else:
                target = self.root / pat
                if not target.exists():
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.touch()
                out.append(Path(pat).as_posix())
        return sorted(set(out))


def _host(p: Path) -> str:
    """Docker Desktop 接受正斜杠形式的 Windows 路径。"""
    return str(p).replace("\\", "/")


# 只匹配「只读文件系统」这一类明确的内核层拒绝。
# 故意不匹配泛化的 Permission denied —— 那会把应用自身的权限错误误判成越界。
_RO_MARKERS = ("Read-only file system", "read-only file system", "Errno 30")


def _readonly_marker(stderr: str) -> str | None:
    for m in _RO_MARKERS:
        if m in stderr:
            return m
    return None


def _guess_path(stderr: str) -> str | None:
    import re

    for pattern in (r"[Rr]ead-only file system: '([^']+)'", r"'(/w/[^']+)'"):
        m = re.search(pattern, stderr)
        if m:
            return m.group(1)
    return None

"""设置页的持久化：写 .env（compose 兼容格式），并同步 os.environ。

为什么落在 .env：`config.py` 本来就按「环境变量优先、.env 兜底」读它，
docker-compose 也吃同一个文件 —— 服务层不发明第二个配置库。

密钥纪律（AGENTS.md）：**只写不读回显**。GET 最多给末 4 位识别串；
key 不进日志、不进数据库。另外这组端点只该绑在 loopback 上 —— 没有权限
概念的服务把 key 的写权限暴露到局域网等于直接交出账号。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from ..config import find_env_file, parse_env

# 全局设置项 → 环境变量名。模型/挡位的默认值与 cli.py 的 _make_backend 对齐。
GLOBAL_KEYS = {
    "base_url_override": "COWORK_LLM_BASE_URL",
    "models.architect": "COWORK_ARCHITECT_MODEL",
    "models.subagent": "COWORK_SUBAGENT_MODEL",
    "models.triage": "COWORK_TRIAGE_MODEL",
    "effort.architect": "COWORK_ARCHITECT_EFFORT",
    "effort.subagent": "COWORK_SUBAGENT_EFFORT",
    "effort.cheap": "COWORK_CHEAP_EFFORT",
    # 每个角色用哪一家（从已配 key 的供应商里选，空 = 自动）。
    # **这和上面的 models.* 是两件事**：那是模型 id 的全局覆盖，这是「谁来干」。
    "providers.architect": "COWORK_ARCHITECT_PROVIDER",
    "providers.reviewer": "COWORK_REVIEWER_PROVIDER",
    "providers.subagent": "COWORK_SUBAGENT_PROVIDER",
    "workspace": "COWORK_WORKSPACE",
    # 工具面的两个闸门。**都归人，不归架构师** —— 让被隔离方给自己配隔离边界
    # 是没有意义的（同 SpecTemplate 那条）。
    "allowed_binaries": "COWORK_ALLOWED_BINARIES",
    "allow_network": "COWORK_ALLOW_NETWORK",
    "review_writes": "COWORK_REVIEW_WRITES",
}

# 环境变量名的合法形状。写文件这一步不该依赖调用方一直守白名单。
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")

DEFAULTS = {
    "base_url_override": "",
    "models": {"architect": "", "subagent": "", "triage": ""},
    # 空 = 自动：架构师用启动时那家，复核者按 `resolve_reviewer` 挑一家**不同的**
    # （§11.11：独立复核的前提就是不同家），Subagent 跟架构师同一家。
    "providers": {"architect": "", "reviewer": "", "subagent": ""},
    "workspace": "",
    "allowed_binaries": "python",
    # 默认关：取回的第三方内容会进 reasoning_trace 再进下一轮提示词，
    # 那是一条提示词注入通道
    "allow_network": "off",
    "effort": {"architect": "high", "subagent": "medium", "cheap": "off"},
    # 写入侧复核（§12 M8）。默认开，依据 §11.19；界面上留开关是因为
    # 它的**重做循环**没在真实链路上测过，只有判别力那半有数据。
    "review_writes": "on",
}


def effective_env() -> dict[str, str]:
    """环境变量 ∪ .env，与 config.load_env 同规则：环境变量赢，空值=未设置。"""
    out: dict[str, str] = {}
    path = find_env_file()
    if path is not None:
        out.update({k: v for k, v in parse_env(path.read_text("utf-8")).items() if v})
    for k, v in os.environ.items():
        if v:
            out[k] = v
    return out


def key_hint(env_name: str) -> tuple[bool, str | None]:
    """(是否配置, 末 4 位识别串)。完整值永远不出这个函数。"""
    value = effective_env().get(env_name, "")
    return (bool(value), f"····{value[-4:]}" if value else None)


def _check(name: str, value: str) -> None:
    """写进 .env 之前的输入校验。

    **换行是这里唯一真正危险的字符**：`.env` 是一行一个 `KEY=value`，值里带
    换行就等于多写了一行。于是一次「设置 API key」的请求可以顺手塞进
    `COWORK_LLM_BASE_URL=http://攻击者/` —— 之后所有请求连同 key 一起送过去。
    这个文件还会被 docker-compose 读，影响面不止本进程。

    键名同样要挡：调用方目前都从 GLOBAL_KEYS / PROVIDERS 的白名单里取，
    但这个函数是「写文件」的最后一道，不该依赖调用方一直守规矩。
    """
    if not _ENV_NAME.fullmatch(name):
        raise ValueError(f"非法的环境变量名: {name!r}")
    if "\n" in value or "\r" in value:
        raise ValueError(f"{name} 的值里不能有换行 —— 那会往 .env 多写一行")


def update_env(pairs: dict[str, str]) -> Path:
    """把 KEY=value 写进 .env：已有行原位替换（保留注释与行序），新键追加。

    空值 = 清除：文件里写 `KEY=`（load 时按未设置处理），os.environ 里直接删。
    """
    for name, value in pairs.items():
        _check(name, value)
    path = find_env_file()
    if path is None:
        path = Path.cwd() / ".env"
    lines = (
        path.read_text(encoding="utf-8").splitlines() if path.is_file() else []
    )
    remaining = dict(pairs)
    for i, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        # `export KEY=...` 也要认出来 —— `config.parse_env` 容忍这个前缀，
        # 这边不认的话就会在文件末尾再追加一行同名的 KEY=。两行都在时靠「后面的
        # 赢」恰好还是对的，但每存一次设置就多一行，最后没人看得懂这份 .env。
        key = stripped.split("=", 1)[0].strip()
        if key.startswith("export "):
            key = key[len("export "):].strip()
        if key in remaining:
            lines[i] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")

    for key, value in pairs.items():
        if value:
            os.environ[key] = value
        else:
            os.environ.pop(key, None)
    return path

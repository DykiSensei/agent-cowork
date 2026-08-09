"""配置与密钥加载。

两条规则：
  1. **环境变量优先于 .env 文件**。容器 / CI 用环境变量覆盖，本地用文件，互不打架。
  2. **密钥永远不进日志、不进数据库**。signals.raw_evidence 存的是 provider 的
     原始错误体，那里面可能带 key 片段 —— 所以在写库前统一脱敏（见 redact）。

格式故意保持 docker compose 兼容（KEY=value，无 export、无 shell 展开），
这样同一个 .env 既喂给 compose 做 ${VAR} 替换，也喂给应用。
不引 python-dotenv：这点解析逻辑不值一个依赖。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

ENV_FILENAME = ".env"
_MAX_PARENTS = 4


def find_env_file(start: Path | None = None) -> Path | None:
    """按 COWORK_ENV_FILE -> cwd -> 逐级向上 的顺序找 .env。"""
    explicit = os.environ.get("COWORK_ENV_FILE")
    if explicit:
        p = Path(explicit).expanduser()
        return p if p.is_file() else None

    here = (start or Path.cwd()).resolve()
    for candidate in [here, *list(here.parents)[:_MAX_PARENTS]]:
        f = candidate / ENV_FILENAME
        if f.is_file():
            return f
    return None


def parse_env(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):  # 容忍一下，虽然 compose 不认
            line = line[len("export ") :].lstrip()
        if "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        if key:
            out[key] = value
    return out


def load_env(path: Path | None = None, *, override: bool = False) -> list[str]:
    """把 .env 载入 os.environ。返回本次实际设置的键名（不含值）。

    已存在的环境变量默认不覆盖 —— 真实环境永远赢过文件。
    空值视为未设置，避免 .env.example 里的空占位覆盖掉真实环境变量。
    """
    f = path or find_env_file()
    if f is None or not f.is_file():
        return []
    applied: list[str] = []
    for key, value in parse_env(f.read_text(encoding="utf-8")).items():
        if not value:
            continue
        if not override and os.environ.get(key):
            continue
        os.environ[key] = value
        applied.append(key)
    return applied


# --------------------------------------------------------------------------- #
# 脱敏
# --------------------------------------------------------------------------- #

# 各家 key 都是 sk- 前缀（Anthropic / DeepSeek / Moonshot / LiteLLM virtual key）。
# 末尾不吃 LiteLLM 自己的掩码形式 sk-...Dwfw —— 那个已经是安全的。
_PATTERNS = [
    (re.compile(r"\bsk-[A-Za-z0-9][A-Za-z0-9_\-]{7,}"), "sk-***REDACTED***"),
    (re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._\-]{8,}"), "Bearer ***REDACTED***"),
    (
        re.compile(r"(?i)(\"?(?:api[_-]?key|authorization|x-api-key)\"?\s*[:=]\s*\"?)[^\"\s,}]{8,}"),
        r"\1***REDACTED***",
    ),
]


def redact(text: str | None) -> str | None:
    """抹掉文本里的密钥。用于任何要落库或打日志的 provider 原始输出。

    宁可多抹一点：signals.raw_evidence 会长期留在 Postgres 里，
    而它的内容是我们控制不了的第三方错误体。
    """
    if not text:
        return text
    out = text
    for pattern, repl in _PATTERNS:
        out = pattern.sub(repl, out)
    return out

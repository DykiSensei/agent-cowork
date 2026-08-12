"""人给三个角色追加的自定义指令（M11）。

**只追加，不替换。** 内置 system 提示词里带着两样动不得的东西：

1. **输出契约** —— 「必须返回符合 ACTION_SCHEMA 的 JSON」。写漏了就是 100%
   解析失败，而失败会走硬信号通道，看起来像模型变笨了，不像配置写错了。
2. **工具清单** —— 模型按它去调工具，和任务级白名单对不上就会撞出假
   SCOPE_VIOLATION（§11.6f 那条坑的老形态）。

所以人的内容拼在内置提示词**之后**，作为「使用者的附加要求」一段。

**位置也是有讲究的**：静态（角色提示词 + 附加指令 + 输出约束）在前、可变
（目标 / 上下文 / 执行记录）在后 —— 提示词的拼装顺序就是前缀缓存的命中率
（§11.14 实测单家 74%），把人的内容插到 user 消息里或塞在变量之间都会让
命中率归零，而那件事**功能测试全绿、账单下个月才告诉你**。

附加指令本身是静态的（只有人在设置页改它时才变），所以它待在静态段里不破坏
缓存：改一次 = 缓存重建一次，之后照旧命中。
"""

from __future__ import annotations

import os

# 角色 → 环境变量。三个角色和设置页的 `providers.*` 是同一套划分：
# architect（生成者：拆解 / 中断决策 / 验收 / 分诊）、reviewer（只看不改）、
# subagent（干活的）。
ROLE_ENV = {
    "architect": "COWORK_ARCHITECT_PROMPT",
    "reviewer": "COWORK_REVIEWER_PROMPT",
    "subagent": "COWORK_SUBAGENT_PROMPT",
}

_HEADER = "\n\n# 使用者的附加要求\n\n以下是这台机器的使用者补充的要求。**在不违反上面的输出格式与工具约束的前提下**遵守它们；两者冲突时以上面的为准。\n\n"


def role_extra(role: str) -> str:
    """这个角色的附加指令，没配就是空串。

    `.env` 一行一个 KEY=value，所以设置页写进去的换行是转义成字面 `\\n` 的
    （`server/settings_io.encode_multiline`）—— 这里还原回来。
    不还原的话，人写的多行要求会挤成一行喂给模型。
    """
    env = ROLE_ENV.get(role)
    if not env:
        return ""
    raw = (os.environ.get(env) or "").strip()
    if not raw:
        return ""
    from ..config import decode_multiline

    return decode_multiline(env, raw).strip()


def with_extra(base: str, role: str) -> str:
    """把附加指令拼在内置提示词之后。没配就原样返回 —— **一个字都不动**，
    这样没用这个功能的人的缓存前缀和以前完全一致。"""
    extra = role_extra(role)
    return f"{base}{_HEADER}{extra}" if extra else base


def skill_block(names: tuple[str, ...] | list[str]) -> str:
    """这个任务选的说明书，拼成静态段**最末尾**的一块（M12）。

    **为什么是最末尾而不是接在角色提示词后面**：`_call()` 会在 system 之后再
    追加输出约束和 schema，所以「接在角色提示词后面」等于把 skill 插进静态段的
    中间 —— 于是**勾了 skill 的任务和没勾的任务，连 schema 那一段都不再共享
    前缀**。而 schema 是这条链上最长的静态文本之一。

    按变化频率从稳到变排：内置提示词（永不变）→ 角色附加（一台机器一份）→
    输出约束 + schema（一种调用一份）→ skill（**按任务变**）。
    越靠后越易变，共享的前缀才最长（§11.14：拼装顺序就是缓存命中率）。

    没选就返回空串 —— 不用这个功能的人一个字都不多付。
    """
    if not names:
        return ""
    from ..skills import render

    return render(names)

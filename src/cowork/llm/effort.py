"""推理挡位：一套词表 → 各家自己的参数（§10.3.2）。

**为什么需要这一层**：各家的「想多深」不是同一个东西。实测各家官方文档（2026-08）：

| 供应商 | 参数 | 值域 | 能不能关 |
|---|---|---|---|
| OpenAI | `reasoning_effort` | none / low / medium / high / xhigh / max | 能（none） |
| DeepSeek | `reasoning_effort` | low / high / max（默认 high） | 能，但要发 `thinking.type=disabled` |
| Kimi k3 | `reasoning_effort` | low / high / max（默认 max） | **不能** |
| Gemini | `reasoning_effort` | low / medium / high | 不能 |
| xAI | `reasoning_effort` | low / medium / high（默认 high） | **不能** |
| 豆包 | `reasoning_effort` | minimal / low / medium / high | 能（minimal） |
| 通义 | `enable_thinking` + `thinking_budget` | bool + int | 能 |
| 智谱 | `thinking.type` | enabled / disabled | 能 |
| Anthropic | `output_config.effort` + `thinking` | 自成一套 | 能（不发 thinking） |

三件事因此必须显式处理：

1. **没有统一的中间档**。DeepSeek 和 Kimi 只有 low/high/max，没有 medium；Gemini 和
   xAI 没有 max。所以统一词表落到某一家时**要向最近的档取整**，而取整这件事必须是
   看得见的（`resolve()` 会把实际发出去的值报出来），不能让人以为设了 medium 就真是 medium。
2. **有的家关不掉**。Kimi k3 和 xAI 无论如何都会思考，`off` 在那里是做不到的事 ——
   如实回落到最低档，不假装关掉了。
3. **不认识的字段不能发**。严格端点上是 400，宽松端点上是静默忽略 —— 后者更糟：
   一个「看着能调、实际没接上」的参数，账单和延迟都不会告诉你（M2 已经有两个死参数了）。
   所以只有在 `PROFILES` 里声明过的供应商才会带上这些字段。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

# 统一词表。从 off 到 max 单调递增 —— 顺序是「向最近档取整」的依据。
LEVELS = ("off", "low", "medium", "high", "max")


class EffortError(ValueError):
    """挡位名不认识。早失败 —— 拼错的挡位如果被忽略，就变成一个静默失效的配置。"""


@dataclass(frozen=True)
class Resolved:
    """一次挡位解析的结果。

    `note` 不是装饰：它记的是「你要的档和实际发出去的值差在哪」。
    `cli` 会把它打出来，测试也断言它 —— 取整必须看得见。
    """

    body: dict[str, Any] = field(default_factory=dict)
    extra_body: dict[str, Any] = field(default_factory=dict)
    note: str = ""


@dataclass(frozen=True)
class Profile:
    """一家供应商的挡位能力。

    `ladder` 是这家**真实支持**的档位，按我们的词表从低到高列。`None` 表示这一档
    它没有，解析时向最近的邻档取整。
    """

    name: str
    ladder: dict[str, str | None]
    param: str = "reasoning_effort"
    # 关掉思考要额外发什么（DeepSeek / 智谱 那种「关」和「调档」不是同一个参数的）
    off_body: dict[str, Any] = field(default_factory=dict)
    off_extra: dict[str, Any] = field(default_factory=dict)
    can_disable: bool = True


def _ladder(low=None, medium=None, high=None, max_=None, off=None) -> dict[str, str | None]:
    return {"off": off, "low": low, "medium": medium, "high": high, "max": max_}


PROFILES: dict[str, Profile] = {
    "openai": Profile(
        "openai",
        _ladder(off="none", low="low", medium="medium", high="high", max_="max"),
    ),
    "deepseek": Profile(
        "deepseek",
        # 没有 medium：low/high/max 三档（默认 high）
        _ladder(low="low", medium=None, high="high", max_="max"),
        # 关思考不走 reasoning_effort，走 extra_body 的 thinking
        off_extra={"thinking": {"type": "disabled"}},
    ),
    "kimi": Profile(
        "kimi",
        # k3 同样只有三档，且**关不掉**
        _ladder(low="low", medium=None, high="high", max_="max"),
        can_disable=False,
    ),
    "gemini": Profile(
        "gemini",
        _ladder(low="low", medium="medium", high="high", max_=None),
        can_disable=False,
    ),
    "xai": Profile(
        "xai",
        _ladder(low="low", medium="medium", high="high", max_=None),
        can_disable=False,
    ),
    "doubao": Profile(
        "doubao",
        # minimal 就是「不思考」
        _ladder(off="minimal", low="low", medium="medium", high="high", max_=None),
    ),
    "qwen": Profile(
        "qwen",
        # 通义没有档位，只有开关（+ 一个 token 预算，这里不动它）
        _ladder(low="on", medium="on", high="on", max_="on", off="off"),
        param="enable_thinking",
    ),
    "zhipu": Profile(
        "zhipu",
        # GLM-5 是开关；reasoning_effort 只有 GLM-5.2 有，不按型号细分就别发
        _ladder(low="on", medium="on", high="on", max_="on", off="off"),
        param="thinking",
    ),
}


def resolve(profile_name: str | None, level: str) -> Resolved:
    """把统一词表的挡位翻成这家真实认识的请求字段。

    `profile_name` 为 None（没声明过挡位能力的供应商）时**什么都不发** ——
    宁可这个旋钮在那家上不起作用，也不要发一个可能 400 的字段。
    """
    if level not in LEVELS:
        raise EffortError(f"未知挡位 {level!r}，可选：{'/'.join(LEVELS)}")
    if not profile_name:
        return Resolved(note=f"这家没声明挡位能力，{level} 未下发")

    p = PROFILES.get(profile_name)
    if p is None:
        raise EffortError(f"未知的挡位方案 {profile_name!r}")

    if level == "off":
        if not p.can_disable:
            fallback = _nearest(p, "low")
            return Resolved(
                body={p.param: fallback},
                note=f"{p.name} 关不掉思考，off 回落到最低档 {fallback}",
            )
        if p.off_extra or p.off_body:
            return Resolved(body=dict(p.off_body), extra_body=dict(p.off_extra),
                            note=f"{p.name} 用专门的开关关闭思考")
        return _emit(p, p.ladder["off"], level)

    return _emit(p, _nearest(p, level), level, wanted=level)


def _nearest(p: Profile, level: str) -> str:
    """这一档没有就找最近的。同距离时**向上取** —— 少想比多想的风险大。

    「向上取」是个立场：挡位调低是为了省钱，而省钱调过头的代价（拆错、验收漏判）
    比多花一点 token 贵得多。所以够不着的时候宁可贵一点。
    """
    i = LEVELS.index(level)
    for dist in range(len(LEVELS)):
        for j in (i + dist, i - dist):          # 同距离先看高的
            if 0 <= j < len(LEVELS):
                got = p.ladder.get(LEVELS[j])
                if got and got != "off":
                    return got
    raise EffortError(f"{p.name} 没有任何可用挡位")


def _emit(p: Profile, value: str | None, level: str, wanted: str | None = None) -> Resolved:
    if value is None:
        return Resolved(note=f"{p.name} 不支持 {level}，未下发")

    note = ""
    if wanted and p.ladder.get(wanted) != value:
        note = f"{p.name} 没有 {wanted} 档，取整到 {value}"

    # 开关型（通义 / 智谱）：值不是档位名，是「开还是关」
    if p.param == "enable_thinking":
        return Resolved(extra_body={"enable_thinking": value == "on"}, note=note)
    if p.param == "thinking":
        return Resolved(
            body={"thinking": {"type": "enabled" if value == "on" else "disabled"}},
            note=note,
        )
    return Resolved(body={p.param: value}, note=note)

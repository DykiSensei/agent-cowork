"""联网搜索的后端：一个**纯搜索 API** 客户端。这里没有 LLM 调用。

为什么不用各家模型自带的联网搜索（调研见开发文档 §11.22）：

- **端点对不上**：openai / xai / doubao 三家把内置搜索搬去了 Responses API，
  而 `openai_compat` 从头到尾只说 chat/completions；gemini 的 OpenAI 兼容层
  压根不透传 grounding —— 这类缺口不报错，**只会静默不搜**。
- **产物形状相反**：内置搜索要的是「模型读完搜索结果，写一段带引文的答案」，
  而 Subagent 每步只能产出一条 `ACTION_SCHEMA` 动作。
- **最要紧的一条：内置搜索绕过工具层**。`fetch_url` 取回的第三方文本至少还经过
  `ToolResult` 落进 checkpoint，可审计、可被白名单管；内置搜索连这个记录点都没有，
  **取回了什么在库里查不到**。

所以搜索这一步归我们自己持有：调一个不经过模型的搜索端点（返回标题/摘要/链接的
结构化结果），控制流仍然是我们的（§10.1 第三条不变量），结果照旧走 `ToolResult`。
搜索端点恰好由模型厂商托管，但它不做生成 —— runtime 不含 LLM 这条没有破。

**不 import `cli.PROVIDERS`**：那张表是给模型后端用的，runtime 不该反向依赖
上层。这里重复一个 key 变量名的代价，小于把 cli 拖进 runtime 的代价。
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

from ..config import redact

# 一次搜索最多要几条。上限的理由同 `sandbox` 里那组常量：**一次工具调用不该把
# 上下文吃光** —— 单个子任务默认 60k token 预算。
DEFAULT_COUNT = 8
MAX_COUNT = 20
TIMEOUT_S = 20.0


@dataclass(frozen=True)
class SearchHit:
    title: str
    url: str
    snippet: str = ""
    source: str = ""
    published: str = ""


class SearchUnavailable(Exception):
    """搜不了：没配 key、这家不认识、或者网络/端点失败。

    **不是任务级失败** —— 调用方要把它转成 `hard_failure=False` 的 `ToolResult`
    （同 `read_file` 探一个不存在的文件那条：否定答案是有效结果，不是故障）。
    消息是给人和模型看的，所以要说清楚下一步该做什么。
    """


@dataclass(frozen=True)
class _Provider:
    """一家搜索端点。今天只有一家，但把形状摆出来，加第二家时不用改调用方。"""

    url: str
    key_env: str
    # 请求体里搜索词的字段名，以及结果列表在响应里的路径
    query_field: str
    count_field: str
    result_path: tuple[str, ...]
    fields: dict[str, str]
    extra: dict[str, str]


PROVIDERS: dict[str, _Provider] = {
    # 智谱是这 10 家里唯一把搜索**单独暴露成 API** 的（不经过模型）。
    # search_std 0.01 元/次；search_pro 0.03 元/次，召回更好。
    "zhipu": _Provider(
        url="https://open.bigmodel.cn/api/paas/v4/web_search",
        key_env="ZHIPUAI_API_KEY",
        query_field="search_query",
        count_field="count",
        result_path=("search_result",),
        fields={
            "title": "title",
            "url": "link",
            "snippet": "content",
            "source": "media",
            "published": "publish_date",
        },
        extra={"search_engine": "search_std"},
    ),
}

DEFAULT_PROVIDER = "zhipu"


def _provider_name() -> str:
    return (os.environ.get("COWORK_SEARCH_PROVIDER") or DEFAULT_PROVIDER).strip().lower()


def _api_key(prov: _Provider) -> str:
    """显式覆盖优先，否则用那家自己的 key。

    这条顺序是有意的：**已经配了智谱 key 的人不需要再配第二个东西**，
    搜索直接就能用。多一个必填配置项，就多一处「装好了但用不了」。
    """
    explicit = (os.environ.get("COWORK_SEARCH_API_KEY") or "").strip()
    return explicit or (os.environ.get(prov.key_env) or "").strip()


def configured() -> str | None:
    """能搜的话返回供应商名，否则 None。

    `runner` 用它决定**要不要把 `search_web` 放进工具白名单** —— 白名单里有一个
    调了必然失败的工具，模型会去调、会白费一步，正是 §11.6f 那种「工具面的缺口
    表现成假信号」的反面版本。
    """
    prov = PROVIDERS.get(_provider_name())
    if prov is None:
        return None
    return _provider_name() if _api_key(prov) else None


def _dig(data: object, path: tuple[str, ...]) -> object:
    for key in path:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def search(query: str, count: int = DEFAULT_COUNT) -> list[SearchHit]:
    """搜一次。失败一律抛 `SearchUnavailable`，正文永不含 key。"""
    from urllib.error import HTTPError, URLError
    from urllib.request import Request, urlopen

    query = (query or "").strip()
    if not query:
        raise SearchUnavailable("搜索词是空的")

    name = _provider_name()
    prov = PROVIDERS.get(name)
    if prov is None:
        raise SearchUnavailable(
            f"不认识的搜索供应商 {name!r}，可选：{', '.join(sorted(PROVIDERS))}"
        )
    key = _api_key(prov)
    if not key:
        raise SearchUnavailable(
            f"没配搜索 key：在 .env 里设 {prov.key_env}（或 COWORK_SEARCH_API_KEY）"
        )

    body = json.dumps(
        {
            prov.query_field: query,
            prov.count_field: max(1, min(int(count or DEFAULT_COUNT), MAX_COUNT)),
            **prov.extra,
        },
        ensure_ascii=False,
    ).encode("utf-8")
    req = Request(
        prov.url,
        data=body,
        headers={
            "Authorization": f"Bearer {key}",
            "Content-Type": "application/json",
            "User-Agent": "cowork-agent/0.1",
        },
    )
    try:
        with urlopen(req, timeout=TIMEOUT_S) as resp:
            raw = resp.read()
    except HTTPError as exc:
        # **按状态码分开说**，理由同 `probe_provider` 那条坑：把 401 和 429 混成
        # 一句「搜索失败」，人会去改一个本来就对的配置。429 反而说明 key 是对的。
        detail = redact(_body_text(exc)) or ""
        if exc.code in (401, 403):
            raise SearchUnavailable(
                f"搜索 key 被拒（HTTP {exc.code}）：检查 {prov.key_env}"
            ) from exc
        if exc.code == 429:
            raise SearchUnavailable("搜索被限流（HTTP 429），稍后再试") from exc
        raise SearchUnavailable(f"搜索端点返回 HTTP {exc.code}：{detail[:200]}") from exc
    except URLError as exc:
        raise SearchUnavailable(f"连不上搜索端点: {exc.reason}") from exc
    except Exception as exc:  # noqa: BLE001 - 网络的失败形态太多，一律当搜不了
        raise SearchUnavailable(f"搜索失败: {type(exc).__name__}: {exc}") from exc

    try:
        payload = json.loads(raw.decode("utf-8", errors="replace"))
    except json.JSONDecodeError as exc:
        raise SearchUnavailable("搜索端点返回的不是 JSON") from exc

    rows = _dig(payload, prov.result_path)
    if rows is None:
        # 端点用 200 报错是常见的（同 Anthropic web_search 那条）：没有结果列表
        # 就把它当失败，但别把整个响应体倒出来 —— 那可能很长。
        msg = str(_dig(payload, ("error", "message")) or "")[:200]
        raise SearchUnavailable(f"搜索没有返回结果列表{'：' + msg if msg else ''}")
    if not isinstance(rows, list):
        raise SearchUnavailable("搜索返回的结果列表形状不对")

    hits: list[SearchHit] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        f = prov.fields
        hits.append(
            SearchHit(
                title=str(row.get(f["title"]) or "").strip(),
                url=str(row.get(f["url"]) or "").strip(),
                snippet=str(row.get(f["snippet"]) or "").strip(),
                source=str(row.get(f["source"]) or "").strip(),
                published=str(row.get(f["published"]) or "").strip(),
            )
        )
    return hits


def _body_text(exc) -> str:
    try:
        return exc.read().decode("utf-8", errors="replace")
    except Exception:  # noqa: BLE001 - 读不出错误体不该盖掉原来的错误
        return ""

"""绑定地址的准入检查。

这个服务把三样东西放在同一个没有认证的 HTTP 面上：**能写 `.env`（含各家
API key 与 `COWORK_LLM_BASE_URL`）的设置页**、**能花钱起任务的执行端**、
以及**人的裁决通道**。`HumanGate` 不知道「哪个人」在回答 —— 系统里没有任何
地方能区分两个来访者。

所以「只绑 loopback」不是默认值的问题，是这个形态**唯一成立的前提**。
把它做成硬拦而不是一句文档：默认值只挡住不知情的人，文档谁都不读，
而这里出错的代价是把一把 key 连同改写 base_url 的能力一起交出去。

要暴露就必须显式说出来（`--i-know-its-exposed`），而且我们仍然会警告。
真要多人用，需要的是认证 + 权限模型，不是一个开关。
"""

from __future__ import annotations

import ipaddress

# 名字形式的回环地址。空串在 uvicorn 里等价于 0.0.0.0（全接口），不在此列。
_LOOPBACK_NAMES = frozenset({"localhost", "localhost.localdomain"})


def is_loopback_host(host: str) -> bool:
    """这个绑定地址是不是只有本机能连。

    IPv4 的整个 127.0.0.0/8 都是回环，不只是 127.0.0.1；IPv6 是 ::1。
    解析不了的名字一律当成**不是**回环 —— 这里的默认必须偏保守。
    """
    h = (host or "").strip().strip("[]").lower()
    if not h:
        return False  # uvicorn 把空串当全接口
    if h in _LOOPBACK_NAMES:
        return True
    try:
        return ipaddress.ip_address(h).is_loopback
    except ValueError:
        return False


def check_bind_host(host: str, *, acknowledged: bool = False) -> str | None:
    """返回 None 表示可以起；返回字符串就是拒绝理由（调用方负责打印并退出）。

    `acknowledged` 为真时不再拒绝，但仍然回一句警告 —— 通过返回值区分：
    拒绝理由由调用方当错误处理，警告只是打印。这里不 print、不 exit，
    是为了让服务层保持可测、也可被别的入口复用。
    """
    if is_loopback_host(host):
        return None
    if acknowledged:
        return None
    return (
        f"拒绝绑定 {host!r}：这个服务没有认证、没有多用户概念，而设置页能写 .env\n"
        f"（各家 API key 与 COWORK_LLM_BASE_URL）。暴露到回环之外 = 任何能访问\n"
        f"这个端口的人都可以取走你的 key、把所有请求改道，并用你的额度起任务。\n"
        f"\n"
        f"  想本机用：去掉 --host，或 --host 127.0.0.1\n"
        f"  确实要暴露（例如你自己在前面挡了一层认证代理）：加 --i-know-its-exposed\n"
    )


def exposure_warning(host: str) -> str:
    return (
        f"⚠ 正在把一个无认证的服务绑到 {host} —— 设置页可读写 .env 里的 API key。\n"
        f"  请自行确认前面有认证代理，或这个网络只有你自己。"
    )

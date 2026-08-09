"""模型调用失败 -> L0 硬信号的映射。

为什么需要这一层：Runtime 不含 LLM，但 Subagent 的每个 step 都要过模型。
模型调用失败（尤其是 LiteLLM 在代理侧拒绝预算）如果直接抛出去，整个 run 会崩，
架构师连中断决策的机会都没有。正确做法是把它变成一条可消费的硬信号。

§7.2 的成本兜底也在这里落地：应用层的 token_budget 是软限制（自己数自己的），
LiteLLM virtual key 才是硬限制（代理侧拒绝）。后者返回的错误必须能被识别成
BUDGET_EXCEEDED，否则「成本兜底」只是句口号。
"""

from __future__ import annotations

from ..signals import SignalType


class ModelError(Exception):
    """模型调用失败，且已归类到某个硬信号。"""

    signal_type: SignalType = SignalType.TOOL_FAILURE

    def __init__(self, message: str, *, status_code: int | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class BudgetExceeded(ModelError):
    """代理侧（LiteLLM virtual key）拒绝：预算已耗尽。"""

    signal_type = SignalType.BUDGET_EXCEEDED


class ModelCallFailed(ModelError):
    """其它模型调用失败：鉴权、限流、上游 5xx、网络。"""

    signal_type = SignalType.TOOL_FAILURE


# LiteLLM 预算拒绝的特征，来自实测而非猜测（litellm main-latest，2026-08）：
#
#   HTTP 429
#   {"error":{"message":"Budget has been exceeded! Key=... Current cost: 1.0,
#             Max budget: 0.05","type":"budget_exceeded","param":null,"code":"429"}}
#
# 首选结构化的 type 字段，文案串作为兜底。tests/test_litellm_budget.py 打的是
# 真实代理，LiteLLM 改文案时测试会先红。
_BUDGET_MARKERS = (
    "budget_exceeded",            # error.type，结构化，最可靠
    "budget has been exceeded",   # error.message
    "exceeded budget",
    "budgetexceedederror",
)

# 反例护栏：这些是别的失败，绝不能被归成预算耗尽。
# 401 invalid x-api-key 实测会带回 Anthropic 原生错误体，里面并无预算字样，
# 但列在这里是为了让意图显式。
_NOT_BUDGET = ("authentication_error", "invalid x-api-key")


def classify_provider_error(status_code: int | None, body: str) -> type[ModelError]:
    """把 provider 返回的错误归类成一个 ModelError 子类。

    注意 LiteLLM 的预算拒绝用的是 HTTP 429，与真实限流同码 —— 所以**不能靠状态码
    判断**，必须看错误体。Anthropic SDK 会对 429 自动重试；这些重试在代理侧就被
    拒了、不会打到上游，因此不额外花钱，只多几秒。
    """
    text = (body or "").lower()
    if any(m in text for m in _NOT_BUDGET):
        return ModelCallFailed
    if any(m in text for m in _BUDGET_MARKERS):
        return BudgetExceeded
    return ModelCallFailed


def from_provider_error(status_code: int | None, body: str) -> ModelError:
    cls = classify_provider_error(status_code, body)
    return cls(body, status_code=status_code)

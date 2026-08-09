"""信号总线。

L0 -> 抢占通道，step 边界一次状态检查即可（§10.1：外部抢占退化成「不派发下一个 step」）。
L1 -> 队列，架构师在检查点批量消费（§3.4）。
"""

from __future__ import annotations

import threading
import time

from ..config import redact
from ..signals import SignalLevel, SignalSource, SignalType, level_of
from ..types import Signal


class SignalBus:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._preempt: list[Signal] = []
        self._soft: list[Signal] = []
        self._all: list[Signal] = []

    def emit(self, sig: Signal) -> Signal:
        sig.level = level_of(sig.type)
        # raw_evidence 是第三方错误体，内容不受我们控制，而它会长期留在 Postgres 里。
        # 这里是所有信号的唯一入口，脱敏放在这一处就够。
        sig.raw_evidence = redact(sig.raw_evidence)
        with self._lock:
            self._all.append(sig)
            if sig.level is SignalLevel.L0:
                self._preempt.append(sig)
            else:
                self._soft.append(sig)
        return sig

    def emit_hard(
        self,
        type: SignalType,
        task_id: str,
        *,
        source: SignalSource = SignalSource.RUNTIME,
        payload: dict | None = None,
        evidence: str | None = None,
    ) -> Signal:
        return self.emit(
            Signal(
                type=type,
                task_id=task_id,
                source=source,
                payload=payload or {},
                raw_evidence=evidence,
            )
        )

    def human_intervention(self, task_id: str, instruction: str) -> Signal:
        """人在群聊中介入 —— 视同硬信号，最高优先级，立即抢占（§2.4 / §7.3）。"""
        return self.emit(
            Signal(
                type=SignalType.HUMAN_INTERVENTION,
                task_id=task_id,
                source=SignalSource.HUMAN,
                payload={"instruction": instruction},
            )
        )

    def take_preempt(self) -> Signal | None:
        """非阻塞：有硬信号就取一条。step 边界调用。"""
        with self._lock:
            if self._preempt:
                return self._preempt.pop(0)
        return None

    def has_preempt(self) -> bool:
        with self._lock:
            return bool(self._preempt)

    def drain_preempt(self) -> list[Signal]:
        with self._lock:
            out, self._preempt = self._preempt, []
        return out

    def drain_soft(self) -> list[Signal]:
        with self._lock:
            out, self._soft = self._soft, []
        return out

    def soft_depth(self) -> int:
        with self._lock:
            return len(self._soft)

    @property
    def all_signals(self) -> list[Signal]:
        with self._lock:
            return list(self._all)

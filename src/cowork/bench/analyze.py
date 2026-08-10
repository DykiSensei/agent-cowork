"""从跑批记录推出六个参数的依据。

每个函数只回答 §12 M2 表里的一行，且都遵守同一条纪律：
**先报分布，再给建议**。同一场景跨运行方差极大（§11.5d），
任何「取平均然后拍一个数」的做法都会把噪声当成结论。

任务集自检（`task_set_health`）放在最前面，因为它决定其余结论有没有意义：
如果 ONE_REBASE 类任务在真实模型下零中断，那测出来的一切都只是 PASS 类的重复。
"""

from __future__ import annotations

import json
import statistics
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from ..policy import DEFAULT_POLICY


def load(path: Path) -> list[dict]:
    return [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines() if ln.strip()]


def _pct(xs: list[float], q: float) -> float:
    if not xs:
        return 0.0
    s = sorted(xs)
    k = min(len(s) - 1, int(round(q * (len(s) - 1))))
    return s[k]


def _dist(xs: list[float]) -> dict[str, float]:
    if not xs:
        return {"n": 0}
    return {
        "n": len(xs),
        "min": round(min(xs), 4),
        "p50": round(_pct(xs, 0.50), 4),
        "p90": round(_pct(xs, 0.90), 4),
        "p95": round(_pct(xs, 0.95), 4),
        "p99": round(_pct(xs, 0.99), 4),
        "max": round(max(xs), 4),
        "mean": round(statistics.fmean(xs), 4),
    }


# --------------------------------------------------------------------------- #
# 0. 任务集自检
# --------------------------------------------------------------------------- #


def task_set_health(recs: list[dict]) -> dict[str, Any]:
    """任务集在真实模型下还有没有区分度（§11.5a）。

    判据：PASS 类应几乎零中断；ONE_REBASE 类应普遍恰好被中断一次；
    MULTI_REBASE 类应普遍 >=2 次。某一类塌到 0，说明模型自己避开了那个失败，
    该任务作废，不能拿它的数据支撑任何参数。
    """
    by_task: dict[str, list[dict]] = defaultdict(list)
    for r in recs:
        by_task[r["task_id"]].append(r)

    rows = []
    for tid, rs in sorted(by_task.items()):
        ints = [r["interrupts"] for r in rs]
        rows.append(
            {
                "task": tid,
                "category": rs[0]["category"],
                "runs": len(rs),
                "interrupts": sorted(ints),
                "interrupt_median": statistics.median(ints),
                "completed": sum(1 for r in rs if r["completed"]),
                "errors": sum(1 for r in rs if r["error"]),
                "tokens": _dist([r["tokens"] for r in rs]),
                # 中断次数不等于中断有用。这两列区分「架构师真的改了规格」和
                # 「架构师只说了句继续」—— 后者是纯开销，中断本可以不发生。
                "runs_with_spec_change": sum(
                    1 for r in rs if any(d["action"] == "MODIFY_TASK" for d in r["decisions"])
                ),
                "runs_only_continue": sum(
                    1
                    for r in rs
                    if r["decisions"]
                    and all(d["action"] == "CONTINUE" for d in r["decisions"])
                ),
            }
        )

    expect = {
        "PASS": (0, 0), "ONE_REBASE": (1, 3), "MULTI_REBASE": (2, 5), "ESCALATE": (1, 5),
        "PROBE_AB": (0, 5),  # 对照组不预期特定中断次数，它测的是成本
    }
    degenerate = [
        row["task"]
        for row in rows
        if not (expect[row["category"]][0] <= row["interrupt_median"] <= expect[row["category"]][1])
    ]
    return {"per_task": rows, "degenerate": degenerate}


def interrupt_sources(recs: list[dict]) -> dict[str, Any]:
    """中断按信号类型拆开。

    不拆开的话 max_interrupts measures 的是「模型自己试错的次数」而不是
    「架构师救不回来的次数」—— 两者的政策含义完全不同。
    `run` 返回非零就抢占是当前设计的明文规定（§3.2），这里量它的实际占比。
    """
    counts = Counter(s["type"] for r in recs for s in r["signals"] if s["level"] == "L0")
    acceptance = {"TEST_FAILED", "VALIDATION_FAILED"}
    per_run_tool = [
        sum(1 for s in r["signals"] if s["type"] == "TOOL_FAILURE") for r in recs
    ]
    per_run_acc = [
        sum(1 for s in r["signals"] if s["type"] in acceptance) for r in recs
    ]
    total = sum(counts.values())
    return {
        "by_type": dict(counts.most_common()),
        "tool_failure_share": round(counts.get("TOOL_FAILURE", 0) / total, 3) if total else None,
        "tool_failures_per_run": _dist(per_run_tool),
        "acceptance_interrupts_per_run": _dist(per_run_acc),
    }


# --------------------------------------------------------------------------- #
# 1. 中断响应延迟 / checkpoint 开销
#    原来这一节是给 step_soft_deadline_s 出建议值的，那个参数已删（无代码读它）。
#    测量本身留着：它证伪了风险 #1 的前提，也是将来真做切段时的起点。
# --------------------------------------------------------------------------- #


def interrupt_latency(recs: list[dict]) -> dict[str, Any]:
    steps = [s for r in recs for s in r["step_seconds"]]
    ckpt = [c for r in recs for c in r["checkpoint_seconds"]]
    step_total = sum(steps)
    ckpt_total = sum(ckpt)
    return {
        "step_seconds": _dist(steps),
        "checkpoint_seconds": _dist(ckpt),
        "checkpoint_overhead_ratio": round(ckpt_total / step_total, 6) if step_total else None,
        # 抢占只发生在 step 边界，所以「外部中断到真正停下」的延迟就是当前 step 的剩余时长，
        # 上界是一个完整 step。
        "interrupt_latency_p50": round(_pct(steps, 0.50), 3),
        "interrupt_latency_p95": round(_pct(steps, 0.95), 3),
    }


# --------------------------------------------------------------------------- #
# 2. complexity_threshold
# --------------------------------------------------------------------------- #


def complexity_roc(recs: list[dict]) -> dict[str, Any]:
    """ROC：只用 complexity_score 判「该不该找人」能做到多好。

    标签来自任务集设计时的人工标注（tasks.py 里每个任务的 should_escalate）。
    正类 = 架构师不该自己拍板的形态。
    """
    points = [
        {"score": d["score"], "label": r["should_escalate"], "task": r["task_id"],
         "kind": d["escalation_kind"]}
        for r in recs
        for d in r["decisions"]
        if d["score"] is not None
    ]
    pos = [p for p in points if p["label"]]
    neg = [p for p in points if not p["label"]]

    rows = []
    for i in range(0, 21):
        th = i / 20
        tp = sum(1 for p in pos if p["score"] >= th)
        fp = sum(1 for p in neg if p["score"] >= th)
        tpr = tp / len(pos) if pos else 0.0
        fpr = fp / len(neg) if neg else 0.0
        rows.append(
            {"threshold": round(th, 2), "tpr": round(tpr, 3), "fpr": round(fpr, 3),
             "youden": round(tpr - fpr, 3)}
        )

    auc = 0.0
    if pos and neg:
        # Mann-Whitney U，等价于 AUC；样本量小，不引 sklearn
        wins = sum(
            1.0 if p["score"] > n["score"] else 0.5 if p["score"] == n["score"] else 0.0
            for p in pos
            for n in neg
        )
        auc = wins / (len(pos) * len(neg))

    best = max(rows, key=lambda r: (r["youden"], -r["threshold"])) if pos and neg else None
    caught_by_rules = sum(1 for p in pos if p["kind"] == "deterministic")
    return {
        "n_pos": len(pos),
        "n_neg": len(neg),
        "pos_scores": _dist([p["score"] for p in pos]),
        "neg_scores": _dist([p["score"] for p in neg]),
        "auc": round(auc, 3),
        "curve": rows,
        "best_youden": best,
        "positives_caught_by_deterministic_rules": caught_by_rules,
        "current": DEFAULT_POLICY.complexity_threshold,
    }


# --------------------------------------------------------------------------- #
# 3. max_rebase
# --------------------------------------------------------------------------- #


def rebase_drift(recs: list[dict]) -> dict[str, Any]:
    """连续 REBASE 后，产出还满不满足**原始** goal。

    偏离 = 通过了被架构师改过的验收标准，却过不了独立的原始意图检查。
    """
    by_n: dict[int, list[dict]] = defaultdict(list)
    for r in recs:
        by_n[r["rebase_count"]].append(r)

    rows = []
    for n, rs in sorted(by_n.items()):
        checked = [r for r in rs if r["intent_ok"] is not None]
        drifted = [r for r in checked if r["completed"] and not r["intent_ok"]]
        rows.append(
            {
                "rebase_count": n,
                "runs": len(rs),
                "completed": sum(1 for r in rs if r["completed"]),
                "intent_checked": len(checked),
                "intent_ok": sum(1 for r in checked if r["intent_ok"]),
                "drifted": len(drifted),
                "drift_rate": round(len(drifted) / len(checked), 3) if checked else None,
                "drifted_tasks": sorted({r["task_id"] for r in drifted}),
            }
        )
    return {"by_rebase_count": rows, "current": DEFAULT_POLICY.max_rebase}


# --------------------------------------------------------------------------- #
# 4. soft_queue_threshold / soft_interval_s
# --------------------------------------------------------------------------- #


def soft_signal_economics(recs: list[dict]) -> dict[str, Any]:
    """分诊调用频次 × 单次成本。

    软信号由 Subagent 自报（L1），真实模型愿不愿意报是个经验问题 ——
    如果实测里根本没有软信号，这两个参数就无从标定，结论只能是「样本不足」，
    不能编一个数出来。
    """
    triage = [c for r in recs for c in r["calls"] if c["kind"] == "triage"]
    soft = [s for r in recs for s in r["signals"] if s["level"] == "L1"]
    by_type = Counter(s["type"] for s in soft)
    by_disp = Counter(s["disposition"] for s in soft)
    runs_with_soft = sum(1 for r in recs if any(s["level"] == "L1" for s in r["signals"]))
    return {
        "runs": len(recs),
        "runs_with_soft_signal": runs_with_soft,
        "soft_signals_total": len(soft),
        "by_type": dict(by_type),
        "by_disposition": dict(by_disp),
        "triage_calls": len(triage),
        "triage_tokens": _dist([c["tokens"] for c in triage]),
        "triage_batch_sizes": sorted(c.get("batch", 0) for c in triage),
        "current": {
            "soft_queue_threshold": DEFAULT_POLICY.soft_queue_threshold,
            "soft_interval_s": DEFAULT_POLICY.soft_interval_s,
        },
    }


# --------------------------------------------------------------------------- #
# 5. max_interrupts
# --------------------------------------------------------------------------- #


def interrupts_vs_success(recs: list[dict]) -> dict[str, Any]:
    """已经被中断 k 次之后，最终还能不能成。

    要看的是**条件成功率**：分母是「至少被中断过 k 次的运行」。
    直接按最终中断数分组会把「一次就成」和「三次才成」混在一起看不出趋势。
    """
    rows = []
    for k in range(0, 6):
        reached = [r for r in recs if r["interrupts"] >= k]
        if not reached:
            continue
        done = [r for r in reached if r["completed"]]
        rows.append(
            {
                "reached_k_interrupts": k,
                "runs": len(reached),
                "eventually_completed": len(done),
                "rate": round(len(done) / len(reached), 3),
                "extra_tokens_median": round(
                    statistics.median([r["tokens"] for r in reached]), 1
                ),
            }
        )
    return {"conditional_success": rows, "current": DEFAULT_POLICY.max_interrupts}


# --------------------------------------------------------------------------- #
# 6. budget_escalation_ratio
# --------------------------------------------------------------------------- #


def budget_ratio(recs: list[dict]) -> dict[str, Any]:
    """触发点距实际超支还有多远。

    对每个候选比例 r：统计「最终成功的运行里有多少会在中途越过 r×budget」——
    那些都是误升级。再统计真正撞上 BUDGET_EXCEEDED 的运行在越过 r 之后
    还烧了多少 token，那是这个阈值买到的提前量。
    """
    usage = [r["tokens"] / r["token_budget"] for r in recs if r["token_budget"]]
    rows = []
    for i in range(2, 11):
        ratio = i / 10
        false_trips = 0
        lead = []
        for r in recs:
            budget = r["token_budget"]
            if not budget:
                continue
            trace = [t["tokens"] for t in r["task_trace"]]
            crossed = next((t for t in trace if t > budget * ratio), None)
            if crossed is None:
                continue
            if r["completed"]:
                false_trips += 1
            lead.append(r["tokens"] - crossed)
        rows.append(
            {
                "ratio": round(ratio, 2),
                "false_escalations_on_successful_runs": false_trips,
                "runs_crossing": len(lead),
                "median_tokens_after_crossing": round(statistics.median(lead), 1) if lead else None,
            }
        )
    return {
        "peak_usage_vs_budget": _dist(usage),
        "by_ratio": rows,
        "budget_exceeded_runs": sum(
            1 for r in recs if any(s["type"] == "BUDGET_EXCEEDED" for s in r["signals"])
        ),
        "current": DEFAULT_POLICY.budget_escalation_ratio,
    }


# --------------------------------------------------------------------------- #
# M5a：架构师的停止判断
# --------------------------------------------------------------------------- #


def stopping_behavior(recs: list[dict]) -> dict[str, Any]:
    """在「本来就不该做完」的任务上，是谁把它停下来的（§11.9）。

    这是 M5a 的核心度量。区分两种停止：

      **架构师自己判断该停**（ABANDON）—— 决策能力起作用了
      **被 policy 的计数器兜住**（FAILED / AWAITING_HUMAN）—— 决策能力没起作用

    M2 的基线是 3/25 主动 ABANDON、20/25 靠上限兜住。这个函数就是拿来跟它比的。
    """
    esc = [r for r in recs if r.get("category") == "ESCALATE" and not r["error"]]
    if not esc:
        return {"runs": 0}

    decisions = [d for r in esc for d in r["decisions"]]
    stall = [
        d for d in decisions
        if (d.get("escalation_reason") or "").startswith("连续")
    ]
    return {
        "runs": len(esc),
        "status": dict(Counter(r["status"] for r in esc)),
        "self_abandon": sum(1 for r in esc if r["status"] == "ABANDONED"),
        "capped": sum(1 for r in esc if r["status"] in ("FAILED", "AWAITING_HUMAN")),
        "completed": sum(1 for r in esc if r["completed"]),
        "interrupts": _dist([r["interrupts"] for r in esc]),
        "tokens": _dist([r["tokens"] for r in esc]),
        "decisions_total": len(decisions),
        "actions": dict(Counter(d["action"] for d in decisions)),
        # M5a 新增的确定性判据命中了几次
        "stall_rule_hits": len(stall),
        "escalation_kinds": dict(Counter(d["escalation_kind"] for d in decisions)),
    }


def compare_stopping(before: list[dict], after: list[dict]) -> str:
    """M5a 的前后对比报告。样本都是 25 次，差异要按这个量级读。"""
    b, a = stopping_behavior(before), stopping_behavior(after)
    if not b.get("runs") or not a.get("runs"):
        return "两侧都需要 ESCALATE 类的运行记录"

    def pct(x, n):
        return f"{x}/{n} = {x / n:.0%}" if n else "-"

    lines = [
        f"ESCALATE 类运行：before {b['runs']} / after {a['runs']}",
        "",
        f"  架构师主动 ABANDON   {pct(b['self_abandon'], b['runs'])}"
        f"   ->   {pct(a['self_abandon'], a['runs'])}",
        f"  被确定性上限兜住     {pct(b['capped'], b['runs'])}"
        f"   ->   {pct(a['capped'], a['runs'])}",
        f"  误完成（本不该做完） {pct(b['completed'], b['runs'])}"
        f"   ->   {pct(a['completed'], a['runs'])}",
        "",
        f"  中断次数中位         {b['interrupts'].get('p50')} -> {a['interrupts'].get('p50')}",
        f"  token 中位           {b['tokens'].get('p50'):.0f} -> {a['tokens'].get('p50'):.0f}",
        f"  决策总数             {b['decisions_total']} -> {a['decisions_total']}",
        "",
        f"  动作分布 before      {b['actions']}",
        f"  动作分布 after       {a['actions']}",
        f"  「决策无效」判据命中  {b['stall_rule_hits']} -> {a['stall_rule_hits']}",
    ]
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# M3：PROBE 的成本溢价
# --------------------------------------------------------------------------- #


def probe_economics(recs: list[dict]) -> dict[str, Any]:
    """PROBE vs TRUST 的同题对照（§12 M3 的 3.3 / 3.4）。

    §3.2.1 说 PROBE「token 成本明显更高，这是必须付的代价」。这里给出那个
    「明显」是多少倍，以及探查间隔与成本的斜率 —— 后者决定 probe_interval 默认值。

    只在 PROBE_AB 类任务上有意义：三个 arm 的 goal / 验收标准 / scope 完全相同，
    差值才能归因到 PROBE 本身。
    """
    ab = [r for r in recs if r.get("category") == "PROBE_AB" and not r["error"]]
    if not ab:
        return {"runs": 0}

    arms: dict[str, list[dict]] = defaultdict(list)
    for r in ab:
        arms[r["task_id"]].append(r)

    baseline = None
    rows = []
    for tid in sorted(arms):
        rs = arms[tid]
        tokens = [r["tokens"] for r in rs]
        med = statistics.median(tokens)
        if rs[0]["silence_policy"] == "TRUST":
            baseline = med
        rows.append(
            {
                "arm": tid,
                "silence_policy": rs[0]["silence_policy"],
                "probe_interval_s": rs[0]["probe_interval_s"],
                "runs": len(rs),
                "completed": sum(1 for r in rs if r["completed"]),
                "tokens": _dist(tokens),
                "probe_calls": _dist([r["probe_count"] for r in rs]),
                "probe_tokens": _dist([r["probe_tokens"] for r in rs]),
                "steps": _dist([r["steps"] for r in rs]),
                "wall_seconds": _dist([r["wall_seconds"] for r in rs]),
            }
        )

    for row in rows:
        row["premium_vs_trust"] = (
            round(row["tokens"]["p50"] / baseline, 3) if baseline else None
        )

    probes = [c for r in ab for c in r["calls"] if c["kind"] == "probe"]
    off = [c for c in probes if not c.get("on_track", True)]
    return {
        "runs": len(ab),
        "arms": rows,
        "trust_baseline_median_tokens": baseline,
        "probe_call_tokens": _dist([c["tokens"] for c in probes]),
        "probe_excerpt_chars": _dist([c.get("excerpt_chars", 0) for c in probes]),
        "probe_calls_total": len(probes),
        "off_track_verdicts": len(off),
        "off_track_rate": round(len(off) / len(probes), 3) if probes else None,
        "current_default_interval": DEFAULT_POLICY.default_probe_interval_s,
    }


# --------------------------------------------------------------------------- #


def summarize(recs: list[dict]) -> dict[str, Any]:
    return {
        "runs": len(recs),
        "errors": sum(1 for r in recs if r["error"]),
        "backends": sorted({r["backend"] for r in recs}),
        "wall_seconds_total": round(sum(r["wall_seconds"] for r in recs), 1),
        "tokens_total": sum(r["tokens"] for r in recs),
        "status": dict(Counter(r["status"] for r in recs)),
        "task_set_health": task_set_health(recs),
        "interrupt_sources": interrupt_sources(recs),
        "interrupt_latency": interrupt_latency(recs),
        "complexity_threshold": complexity_roc(recs),
        "max_rebase": rebase_drift(recs),
        "soft_signals": soft_signal_economics(recs),
        "max_interrupts": interrupts_vs_success(recs),
        "budget_escalation_ratio": budget_ratio(recs),
        "probe_economics": probe_economics(recs),
    }


def render(summary: dict[str, Any]) -> str:
    """人读的报告。数字都带 n=，因为样本量小是这批数据的主要局限。"""
    out: list[str] = []
    w = out.append
    w(f"运行 {summary['runs']} 次，错误 {summary['errors']} 次，"
      f"后端 {'/'.join(summary['backends'])}，"
      f"累计 {summary['tokens_total']} token / {summary['wall_seconds_total']:.0f}s")
    w(f"终局状态: {summary['status']}")

    w("\n## 任务集自检")
    for row in summary["task_set_health"]["per_task"]:
        w(f"  {row['task']:<24} {row['category']:<13} 中断 {row['interrupts']} "
          f"完成 {row['completed']}/{row['runs']} token中位 {row['tokens'].get('p50', 0):.0f} "
          f"改规格 {row['runs_with_spec_change']} 仅继续 {row['runs_only_continue']}")
    deg = summary["task_set_health"]["degenerate"]
    w(f"  失去区分度的任务: {deg or '无'}")

    isrc = summary["interrupt_sources"]
    w("\n## 中断来源")
    w(f"  按信号类型: {isrc['by_type']}")
    w(f"  TOOL_FAILURE 占比 {isrc['tool_failure_share']}"
      f"（每次运行 {isrc['tool_failures_per_run'].get('p50')} 条，中位）")
    w(f"  验收级中断/次运行: {isrc['acceptance_interrupts_per_run']}")

    s = summary["interrupt_latency"]
    w("\n## 中断响应延迟 / checkpoint 开销")
    w(f"  step 耗时 s: {s['step_seconds']}")
    w(f"  checkpoint 耗时 s: {s['checkpoint_seconds']}")
    w(f"  checkpoint 开销占 step 总耗时: {s['checkpoint_overhead_ratio']}")
    w(f"  中断响应延迟 p50/p95: {s['interrupt_latency_p50']}s / {s['interrupt_latency_p95']}s")

    c = summary["complexity_threshold"]
    w("\n## complexity_threshold")
    w(f"  正类 {c['n_pos']} 条 / 负类 {c['n_neg']} 条，AUC={c['auc']}")
    w(f"  正类分数分布 {c['pos_scores']}")
    w(f"  负类分数分布 {c['neg_scores']}")
    w(f"  最佳 Youden 点 {c['best_youden']}（当前值 {c['current']}）")
    w(f"  正类中已被确定性规则拦下的: {c['positives_caught_by_deterministic_rules']}/{c['n_pos']}")

    r = summary["max_rebase"]
    w("\n## max_rebase")
    for row in r["by_rebase_count"]:
        w(f"  REBASE {row['rebase_count']} 次: {row['runs']} 运行，完成 {row['completed']}，"
          f"意图偏离 {row['drifted']}（率 {row['drift_rate']}）{row['drifted_tasks'] or ''}")
    w(f"  当前值 {r['current']}")

    sf = summary["soft_signals"]
    w("\n## soft_queue_threshold / soft_interval_s")
    w(f"  {sf['runs_with_soft_signal']}/{sf['runs']} 次运行出现过软信号，"
      f"共 {sf['soft_signals_total']} 条 {sf['by_type']}")
    w(f"  分诊调用 {sf['triage_calls']} 次，批大小 {sf['triage_batch_sizes']}，"
      f"token {sf['triage_tokens']}")

    mi = summary["max_interrupts"]
    w("\n## max_interrupts")
    for row in mi["conditional_success"]:
        w(f"  被中断 >= {row['reached_k_interrupts']} 次的 {row['runs']} 次运行中，"
          f"最终完成 {row['eventually_completed']}（{row['rate']:.0%}）")
    w(f"  当前值 {mi['current']}")

    b = summary["budget_escalation_ratio"]
    w("\n## budget_escalation_ratio")
    w(f"  实际用量/预算: {b['peak_usage_vs_budget']}")
    for row in b["by_ratio"]:
        w(f"  ratio={row['ratio']}: 越线运行 {row['runs_crossing']}，"
          f"其中最终成功（=误升级）{row['false_escalations_on_successful_runs']}，"
          f"越线后还烧 {row['median_tokens_after_crossing']} token")
    w(f"  真实 BUDGET_EXCEEDED: {b['budget_exceeded_runs']} 次，当前值 {b['current']}")

    p = summary["probe_economics"]
    if p["runs"]:
        w("\n## PROBE 成本溢价（M3）")
        for row in p["arms"]:
            w(f"  {row['arm']:<16} {row['silence_policy']:<7} "
              f"间隔={row['probe_interval_s']} 探查{row['probe_calls'].get('p50')}次 "
              f"token中位={row['tokens'].get('p50'):.0f} "
              f"溢价={row['premium_vs_trust']}x 完成 {row['completed']}/{row['runs']}")
        w(f"  单次探查 token: {p['probe_call_tokens']}")
        w(f"  探查判跑偏 {p['off_track_verdicts']}/{p['probe_calls_total']}"
          f"（率 {p['off_track_rate']}），当前默认间隔 {p['current_default_interval']}s")
    return "\n".join(out)

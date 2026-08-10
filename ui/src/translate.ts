/**
 * 翻译层：views.task_detail() 的投影 → 前端渲染用的时间线（StreamEvent[]）。
 *
 * 后端事件是**索引**（ref_id 指向 signals / decisions 里的正文），前端事件是
 * 解析好的可直接渲染对象。映射规则按 M6-界面层接口.md §10.4 的表，另有几条
 * 呈现层的取舍，都注释在下面。
 */

import { fmtTok } from "./copy";
import type { StreamEvent, TaskDetail, TaskStatus } from "./types";

/** CLI 的 _render() 打进日志的裁决正文 —— 与裁决卡片重复，呈现层滤掉。 */
function isDecisionLogNoise(text: string): boolean {
  return text.startsWith("[DEC") || text.startsWith("       ");
}

/** PROBE / 软信号分诊这类「背景音」日志，pro 里降档显示、lite 里不显示。 */
function isSoftLog(text: string): boolean {
  const t = text.trimStart();
  return t.startsWith("[PROBE") || t.startsWith("[SOFT") || t.startsWith("[REVIEW");
}

function terminalChips(detail: TaskDetail, status: TaskStatus): string[] {
  if (detail.kind === "single") {
    const s = detail.state;
    const chips = [
      `合计 ${s.tokens_used.toLocaleString()} tok`,
      `中断 ${s.interrupt_count} 次`,
    ];
    if (status === "ABANDONED") chips.unshift("人确认放弃");
    if (status === "FAILED") chips.unshift("跑满 max_cycles 仍未完成");
    return chips;
  }
  // 复合任务：root 上没有 status 事件，终局卡由翻译层在事件流末尾合成
  const tasks = Object.values(detail.tasks);
  const done = tasks.filter((t) => t.status === "COMPLETED").length;
  const conflicts = Object.values(detail.signals).filter(
    (s) => s.type === "CONFLICT_DETECTED",
  ).length;
  const conflictIds = new Set(
    Object.values(detail.signals)
      .filter((s) => s.type === "CONFLICT_DETECTED")
      .map((s) => s.id),
  );
  const arbitrations = Object.values(detail.decisions).filter((d) =>
    d.trigger.some((t) => conflictIds.has(t)),
  ).length;
  return [
    `${done}/${tasks.length} 子任务完成`,
    `合计 ${fmtTok(tasks.reduce((a, t) => a + t.tokens_used, 0))} tok`,
    `冲突 ${conflicts} · 仲裁 ${arbitrations}`,
  ];
}

export function translate(detail: TaskDetail): StreamEvent[] {
  const out: StreamEvent[] = [];
  const pending = detail.kind === "single" ? detail.pending : null;
  const lastSeq = detail.events.length
    ? detail.events[detail.events.length - 1].seq
    : 0;
  // 终局卡延后一拍：orchestrator 先写 status 事件再写 [DONE]/[STOP] 日志，
  // 卡片压在日志前面会倒序，所以等后面的日志行先出来
  let deferredTerminal: StreamEvent | null = null;
  const flushTerminal = () => {
    if (deferredTerminal) {
      out.push(deferredTerminal);
      deferredTerminal = null;
    }
  };

  // 开头那句「你发布任务」。服务层现在会在 POST /tasks 时把**人的原话**写成
  // root 线程的第一条 human 事件 —— 有真的就用真的，别再合成（合成用的是当前
  // spec.goal，而它被架构师改写过，rev>1 时那已经不是人说的话了）。
  //
  // 还需要保留合成这条退路：`cli composite` / 老库里没有那条事件，
  // 以及单任务被直接派发（不经拆解）时也没有。
  const hasRealOpening = detail.events[0]?.kind === "human";
  const firstGoal =
    hasRealOpening || detail.kind !== "single" ? null : detail.state?.spec.goal;

  for (const ev of detail.events) {
    switch (ev.kind) {
      case "human":
        flushTerminal();
        out.push({ kind: "human", text: ev.text, ts: ev.created_at });
        break;
      case "log": {
        if (isDecisionLogNoise(ev.text)) break;
        // 日志行不触发 flush —— 终局卡要等 [DONE]/[STOP] 先出来
        out.push({
          kind: "log",
          text: ev.text,
          ts: ev.created_at,
          soft: isSoftLog(ev.text),
        });
        break;
      }
      case "signal": {
        flushTerminal();
        const sig = ev.ref_id ? detail.signals[ev.ref_id] : null;
        if (sig) out.push({ kind: "signal", signal: sig });
        break;
      }
      case "decision": {
        flushTerminal();
        // 挂起占位裁决不渲成卡片 —— 「等你拍板」卡片（pending）已经覆盖它
        if (pending?.decision_id && ev.ref_id === pending.decision_id) break;
        const dec = ev.ref_id ? detail.decisions[ev.ref_id] : null;
        if (dec) out.push({ kind: "decision", decision: dec });
        break;
      }
      case "status": {
        const status = ev.payload?.status as TaskStatus | undefined;
        if (status === "AWAITING_HUMAN") {
          flushTerminal();
          // 只有「此刻仍在等」的最后一条渲染成卡片；历史上的挂起降为一行
          if (pending && ev.seq === lastSeq) {
            out.push({ kind: "awaiting", pending, ts: ev.created_at });
          } else {
            out.push({
              kind: "log",
              text: "挂起，等待人处理",
              ts: ev.created_at,
              soft: true,
            });
          }
        } else if (
          status === "COMPLETED" ||
          status === "FAILED" ||
          status === "ABANDONED"
        ) {
          deferredTerminal = {
            kind: "terminal",
            status,
            chips: terminalChips(detail, status),
          };
        }
        // RUNNING / INTERRUPTED 不渲 —— [RUN]/[STOP] 日志行已经说了同样的事
        break;
      }
      case "plan":
        if (detail.kind === "composite") {
          flushTerminal();
          out.push({
            kind: "plan",
            plan: ev.payload as never,
            tasks: detail.tasks,
            pendingChildren: detail.pending_children,
          });
        }
        break;
      case "review":
        // 结构化复核结论不进时间线：[REVIEW] 日志行已作刻度渲染，
        // 完整内容在右栏（pro）/ 方案卡头部（lite）
        break;
    }
  }
  flushTerminal();

  // 开头合成「你发布任务」的气泡（见上面的注释）
  if (firstGoal) {
    out.unshift({ kind: "human", text: firstGoal, ts: null });
  }

  // 复合任务的终局卡：root 时间线没有 status 事件，按子任务状态合成
  if (detail.kind === "composite") {
    const tasks = Object.values(detail.tasks);
    const TERMINAL: TaskStatus[] = ["COMPLETED", "FAILED", "ABANDONED"];
    if (
      tasks.length > 0 &&
      tasks.every((t) => TERMINAL.includes(t.status)) &&
      out[out.length - 1]?.kind !== "terminal"
    ) {
      const status: TaskStatus = tasks.every((t) => t.status === "COMPLETED")
        ? "COMPLETED"
        : "FAILED";
      out.push({
        kind: "terminal",
        status,
        title:
          status === "COMPLETED"
            ? `COMPLETED · 全部 ${tasks.length}/${tasks.length} 子任务`
            : undefined,
        chips: terminalChips(detail, status),
      });
    }
  }

  return out;
}

export { isSoftLog };

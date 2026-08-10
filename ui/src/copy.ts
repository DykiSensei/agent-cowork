/**
 * 文案与映射。lite 模式的「人话翻译」全部集中在这里 ——
 * 数据形状不变，只是呈现层的一张映射表。
 */

import type { DecisionRecord, Signal, TaskStatus } from "./types";

export const STATUSES: TaskStatus[] = [
  "PENDING",
  "RUNNING",
  "INTERRUPTED",
  "COMPLETED",
  "AWAITING_HUMAN",
  "FAILED",
  "ABANDONED",
];

export const LITE_STATUS: Record<TaskStatus, string> = {
  PENDING: "排队中",
  RUNNING: "进行中…",
  INTERRUPTED: "暂停了一下",
  COMPLETED: "已完成",
  AWAITING_HUMAN: "等你处理",
  FAILED: "没跑成",
  ABANDONED: "已放弃",
};

/** 裁决四选项的 lite 文案（按钮上）。 */
export const LITE_ACTION: Record<DecisionRecord["action"], string> = {
  CONTINUE: "继续试试",
  MODIFY_TASK: "改一下任务",
  REASSIGN: "换个模型重做",
  ABANDON: "放弃",
};

/** 系统建议里的 lite 文案（建议框里）。 */
export const LITE_ACTION_SUGGEST: Record<DecisionRecord["action"], string> = {
  CONTINUE: "继续试试",
  MODIFY_TASK: "改一下任务",
  REASSIGN: "换个模型重做",
  ABANDON: "先放弃",
};

export function liteSignalTitle(type: string): string {
  const m: Record<string, string> = {
    TEST_FAILED: "验收没通过",
    TOOL_FAILURE: "有工具调用失败了",
    TIMEOUT: "跑超时了",
    STEP_LIMIT: "步数用完了",
    BUDGET_EXCEEDED: "预算用完了",
    SCOPE_VIOLATION: "碰了不该碰的文件",
    VALIDATION_FAILED: "产出不符合要求",
    HUMAN_INTERVENTION: "你打断了它",
    CONFLICT_DETECTED: "两个任务改了同一个文件",
  };
  return m[type] ?? "遇到一个问题";
}

export function liteSignalBody(sig: Signal): string {
  const p = sig.payload ?? {};
  if (sig.type === "TEST_FAILED") {
    return `「${String(p.description ?? "验收标准")}」没过${
      p.exit_code != null ? `（退出码 ${String(p.exit_code)}）` : ""
    }。`;
  }
  if (typeof p.detail === "string") return p.detail;
  if (typeof p.description === "string") return String(p.description);
  if (sig.type === "TOOL_FAILURE") return `工具 ${String(p.tool ?? "")} 调用失败。`;
  return "";
}

/** pro 模式的信号正文：保留全部技术细节。 */
export function proSignalBody(sig: Signal): string {
  const p = sig.payload ?? {};
  switch (sig.type) {
    case "TEST_FAILED":
      return `验收标准 ${String(p.criterion_id ?? "")}「${String(
        p.description ?? "",
      )}」未通过 · exit_code ${String(p.exit_code ?? "?")}`;
    case "TOOL_FAILURE":
      return `工具调用任务级失败：${String(p.tool ?? "")} ${JSON.stringify(
        p.argv ?? "",
      )}`;
    default:
      return typeof p.detail === "string" ? p.detail : JSON.stringify(p);
  }
}

/**
 * 裁决理由的 lite 清理：AutoApproveGate 会把「升级原因：…；采纳 LLM 裁决：」
 * 前缀拼进 rationale（architect.py），lite 里把这段机制性前缀剥掉只留正文。
 */
export function liteRationale(d: DecisionRecord): string {
  return d.rationale.replace(/^\[AutoApproveGate\][^；;]*[；;]采纳 LLM 裁决：/, "");
}

/** 升级原因的人话版（lite 的「需要你定一下」卡片正文）。 */
export function liteWaitText(reason: string): string {
  if (reason.includes("指纹完全相同"))
    return "同一个问题补了说明还是原样出现。系统觉得自己找不到原因，不想再盲目重试下去 —— 继续还是停下，由你定。";
  if (reason.includes("interrupt_count"))
    return "已经连续中断好几次了，系统觉得自己没找到根因 —— 继续还是停下，由你定。";
  if (reason.includes("ABANDON"))
    return "系统想放弃这个任务。放弃不可逆，所以由你定。";
  if (reason.includes("parent_id"))
    return "系统想修改你最初定下的任务 —— 这触及你的原始意图，由你定。";
  if (reason.includes("SCOPE_VIOLATION"))
    return "它碰了任务范围外的东西 —— 要不要放宽边界，由你定。";
  if (reason.includes("预算"))
    return "花的 token 已经超过预算警戒线 —— 继续还是停下，由你定。";
  return reason;
}

export function fmtTime(ts: number | null | undefined): string {
  if (!ts) return "";
  const d = new Date(ts * 1000);
  const pad = (n: number) => String(n).padStart(2, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}`;
}

export function fmtTok(n: number): string {
  return n >= 1000 ? `${(n / 1000).toFixed(1).replace(/\.0$/, "")}k` : String(n);
}

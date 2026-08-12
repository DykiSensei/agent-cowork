/**
 * 文案与映射。lite 模式的「人话翻译」全部集中在这里 ——
 * 数据形状不变，只是呈现层的一张映射表。
 */

import type {
  DecisionRecord,
  Signal,
  TaskDetail,
  TaskProgress,
  TaskStatus,
} from "./types";

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

// --------------------------------------------------------------------- //
// 「此刻在做什么」（views.task_progress → 一句人话）
// --------------------------------------------------------------------- //

/**
 * 后端只回结构（`kind` / `name` / `target`），措辞在这里。
 *
 * 为什么不让后端拼句子：同一个动作在专业版要显示成 `write_file out.py`，
 * 在简洁版要显示成「正在写 out.py」—— 两种话，一份数据。
 */
const TOOL_VERB: Record<string, string> = {
  write_file: "正在写",
  read_file: "正在读",
  list_files: "正在看目录",
  search_files: "正在搜代码",
  delete_file: "正在删",
  move_file: "正在挪",
  fetch_url: "正在取网页",
  search_web: "正在搜",
  run: "正在执行",
};

export function liteDoingText(p: TaskProgress): string {
  if (p.status === "AWAITING_HUMAN") return "停下来等你拍板";
  if (p.status === "COMPLETED") return "做完了";
  if (p.status === "FAILED") return "没跑成";
  if (p.status === "ABANDONED") return "已放弃";
  if (p.status === "PENDING") {
    // 说得出「第几层、还有几个在它前面」，人才知道这是在等而不是坏了
    const q = p.queue;
    if (q) {
      return q.layer > 1
        ? `排在第 ${q.layer}/${q.layers_total} 批，等前面那批做完`
        : `轮到它了，等一个空位（这批 ${q.layer_size} 个、同时跑 ${q.parallel} 个）`;
    }
    return "排队等前面的步骤做完";
  }
  // **阶段优先于动作**（M12）：`last_action` 说的是「上一个动作是什么」，
  // 而 phase 说的是「此刻在干什么」。模型正在想下一步时，前者还停在上一步 ——
  // 于是界面显示「正在写 a.py」，其实那一步早就写完了。
  if (p.phase === "thinking") return "正在想下一步";
  if (p.phase === "verifying") return "在跑验收";
  const a = p.last_action;
  if (!a) return "刚开始，还没动手";
  if (a.kind === "finish") return "在收尾，等验收";
  if (a.kind === "soft_signal") return "报告了一个疑点，接着干";
  const verb = TOOL_VERB[a.name ?? ""] ?? "正在操作";
  return a.target ? `${verb} ${a.target}` : verb;
}

/** 专业版：把动作还原成它本来的样子，不做人话化。 */
export function proDoingText(p: TaskProgress): string {
  if (p.phase === "thinking") return "next_step → 等模型";
  if (p.phase === "verifying") return "acceptance → 跑验收命令";
  const a = p.last_action;
  if (!a) return p.status;
  if (a.kind === "finish") return "finish → 等验收";
  if (a.kind === "soft_signal") return "soft_signal";
  return `${a.name ?? "?"}${a.target ? ` ${a.target}` : ""}`;
}

// --------------------------------------------------------------------- //
// 底部输入区此刻处于什么状态
// --------------------------------------------------------------------- //

export type ComposerPhase =
  | "running"    // 有正在跑的任务，可以介入
  | "waiting"    // 停下来等人拍板 —— 去卡片里答复
  | "queued"     // 还没开始跑：拆解中、刚派发、或者在等前面的步骤
  | "done";      // 终局

/**
 * **「不在跑」不等于「已经结束」。**
 *
 * 实测撞到的：任务处于排队中，底部却写「这条任务已经结束了」——
 * 因为分支只判了 running / waiting，剩下的一律当终局。PENDING、拆解中、
 * 刚派发这三种都落在那个 else 里，而它们恰恰是「再等一会儿就好」。
 */
export function composerPhase(detail: TaskDetail): ComposerPhase {
  if (detail.kind === "single") {
    const s = detail.state.status;
    if (s === "RUNNING" || s === "INTERRUPTED") return "running";
    if (s === "AWAITING_HUMAN") return "waiting";
    return s === "PENDING" ? "queued" : "done";
  }
  const tasks = Object.values(detail.progress);
  if (tasks.some((t) => t.status === "RUNNING" || t.status === "INTERRUPTED"))
    return "running";
  if (detail.pending_children.length > 0) return "waiting";
  // 一个子任务都还没有 = 还在拆解 / 刚派发，那是最容易被误报成「结束」的一种
  if (tasks.length === 0) return "queued";
  return tasks.every((t) => t.terminal) ? "done" : "queued";
}

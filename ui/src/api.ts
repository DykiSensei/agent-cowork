/**
 * API 客户端。路径按 M6-界面层接口.md §6（/api 前缀）。
 * 开发/预览环境由 ui/mock/plugin.ts 应答；接真实服务层时改 vite 的 proxy 即可。
 *
 * **写类端点一律返回 `ActionResult`，不返回裸 Response。**
 * 原来它们把 Response 交出去，而调用方普遍只 `.then()` 不看状态码 ——
 * 于是「任务不在运行中」（409）、「值里有换行」（400）、「.env 写不进去」（500）
 * 在界面上全都长得像成功。服务端有话要说的时候，界面得把那句话拿出来。
 */

import type {
  PlanView,
  ProbeResult,
  ProviderInfo,
  Settings,
  TaskDetail,
  ThreadSummary,
} from "./types";

export interface ActionResult {
  ok: boolean;
  /** 服务端 `{error}` 里的那句话；网络层失败时是异常文本。 */
  error?: string;
}

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`${init?.method ?? "GET"} ${path} -> ${r.status}`);
  return (await r.json()) as T;
}

/** 服务端的错误正文形状统一是 `{error: string}`（app.py 的 err()）。 */
async function errorText(r: Response): Promise<string> {
  try {
    const body = (await r.json()) as { error?: unknown };
    if (body && typeof body.error === "string" && body.error) return body.error;
  } catch {
    // 不是 JSON（502/代理页之类），退回状态码
  }
  return `请求失败（HTTP ${r.status}）`;
}

async function send(
  path: string,
  body: unknown,
  method = "POST",
): Promise<ActionResult & { data?: unknown }> {
  let r: Response;
  try {
    r = await fetch(path, {
      method,
      headers: { "content-type": "application/json" },
      body: JSON.stringify(body ?? {}),
    });
  } catch (e: unknown) {
    return { ok: false, error: `连不上服务：${String(e)}` };
  }
  if (!r.ok) return { ok: false, error: await errorText(r) };
  try {
    return { ok: true, data: await r.json() };
  } catch {
    return { ok: true };
  }
}

export function fetchThreads(): Promise<ThreadSummary[]> {
  return req("/api/tasks");
}

export function fetchDetail(taskId: string): Promise<TaskDetail> {
  return req(`/api/tasks/${encodeURIComponent(taskId)}`);
}

export function postIntervene(taskId: string, instruction: string) {
  return send(`/api/tasks/${encodeURIComponent(taskId)}/intervene`, { instruction });
}

/**
 * 停掉正在跑的任务。和 ruling(ABANDON) 是两条路：那条管已经挂起的，
 * 这条管**正在烧钱的**。任务不在运行中时服务端回 409 —— 那句话要给用户看到。
 */
export function postCancel(taskId: string, reason: string) {
  return send(`/api/tasks/${encodeURIComponent(taskId)}/cancel`, { reason });
}

export function postRuling(taskId: string, action: string, rationale: string) {
  return send(`/api/tasks/${encodeURIComponent(taskId)}/ruling`, { action, rationale });
}

// --------------------------------------------------------------------- //
// 发布任务 → 拆解 → 裁决 → 派发（M6 §6 的 POST /tasks 与 /plans/*）
// --------------------------------------------------------------------- //

/** 发布一个目标。回来的不是任务而是**一次拆解**（plan_id）。 */
export async function createTask(
  goal: string,
): Promise<ActionResult & { plan_id?: string }> {
  const r = await send("/api/tasks", { goal });
  if (!r.ok) return r;
  return { ok: true, plan_id: (r.data as { plan_id?: string })?.plan_id };
}

export function fetchPlan(planId: string): Promise<PlanView> {
  return req(`/api/plans/${encodeURIComponent(planId)}`);
}

/** 人对一份拆解的裁决。`accept=false` 是否决，有后果，所以必须显式传。 */
export function rulePlan(planId: string, accept: boolean, rationale = "") {
  return send(`/api/plans/${encodeURIComponent(planId)}/ruling`, { accept, rationale });
}

/** 派发。回来的 root_id 就是那条复合线程的 id。 */
export async function dispatchPlan(
  planId: string,
  assignments?: Record<string, string>,
): Promise<ActionResult & { root_id?: string }> {
  const r = await send(`/api/plans/${encodeURIComponent(planId)}/dispatch`, {
    assignments: assignments ?? null,
  });
  if (!r.ok) return r;
  return { ok: true, root_id: (r.data as { root_id?: string })?.root_id };
}

/**
 * SSE 订阅。mock 只发心跳；真实服务推 task/event/plan/awaiting/server-log 几类。
 * 返回取消函数。
 */
export function subscribeStream(onEvent: (data: unknown) => void): () => void {
  const es = new EventSource("/api/stream");
  es.onmessage = (e) => {
    try {
      onEvent(JSON.parse(e.data as string));
    } catch {
      // 心跳等非 JSON 帧忽略
    }
  };
  return () => es.close();
}

// --------------------------------------------------------------------- //
// 设置页
// --------------------------------------------------------------------- //

export function fetchProviders(): Promise<ProviderInfo[]> {
  return req("/api/providers");
}

/**
 * 真打一次端点，回答「这个 key 现在能不能用」。
 * 和 `cowork models` 共用后端的 probe_provider()。
 */
export function testProvider(name: string): Promise<ProbeResult> {
  return req(`/api/providers/${encodeURIComponent(name)}/test`, { method: "POST" });
}

export function putProviderKey(name: string, apiKey: string) {
  return send(`/api/providers/${encodeURIComponent(name)}`, { api_key: apiKey }, "PUT");
}

export function fetchSettings(): Promise<Settings> {
  return req("/api/settings");
}

export function putSettings(s: Partial<Settings>) {
  return send("/api/settings", s, "PUT");
}

/**
 * API 客户端。路径按 M6-界面层接口.md §6（/api 前缀）。
 * 开发/预览环境由 ui/mock/plugin.ts 应答；接真实服务层时改 vite 的 proxy 即可。
 */

import type {
  ProviderInfo,
  Settings,
  TaskDetail,
  ThreadSummary,
} from "./types";

async function req<T>(path: string, init?: RequestInit): Promise<T> {
  const r = await fetch(path, init);
  if (!r.ok) throw new Error(`${init?.method ?? "GET"} ${path} -> ${r.status}`);
  return (await r.json()) as T;
}

export function fetchThreads(): Promise<ThreadSummary[]> {
  return req("/api/tasks");
}

export function fetchDetail(taskId: string): Promise<TaskDetail> {
  return req(`/api/tasks/${taskId}`);
}

export function postIntervene(taskId: string, instruction: string) {
  return fetch(`/api/tasks/${taskId}/intervene`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ instruction }),
  });
}

export function postRuling(taskId: string, action: string, rationale: string) {
  return fetch(`/api/tasks/${taskId}/ruling`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ action, rationale }),
  });
}

/**
 * SSE 订阅。mock 只发心跳；真实服务推 task/signal/decision/log 四类事件。
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

export function putProviderKey(name: string, apiKey: string) {
  return fetch(`/api/providers/${name}`, {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify({ api_key: apiKey }),
  });
}

export function fetchSettings(): Promise<Settings> {
  return req("/api/settings");
}

export function putSettings(s: Partial<Settings>) {
  return fetch("/api/settings", {
    method: "PUT",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(s),
  });
}

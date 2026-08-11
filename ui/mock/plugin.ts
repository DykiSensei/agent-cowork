/**
 * 开发/预览环境的 mock API。
 *
 * 读类端点直接透传 ui/fixtures/ —— 那是 `make_fixtures.py` 用真实运行 +
 * `cowork.views` 导出的 JSON，与将来 FastAPI 服务层同一份形状：
 *
 *   GET  /api/tasks                 → fixtures/threads.json（views.thread_list）
 *   GET  /api/tasks/:id             → fixtures/<id>.json（views.task_detail）
 *   POST /api/tasks/:id/intervene   → 202（mock 不驱动任何任务）
 *   POST /api/tasks/:id/cancel      → 202（真实服务：不在运行中回 409）
 *   POST /api/tasks/:id/ruling      → 202
 *   GET  /api/stream                → SSE，只发心跳
 *
 * 设置页端点（界面层扩展，真实语义归服务层 —— 它决定写 .env 还是自己的配置库）：
 *
 *   GET  /api/providers             → fixtures/providers.json ⊕ settings.local.json
 *                                     （key 永远只写不读，回包只有 configured + key_hint）
 *   PUT  /api/providers/:name       {api_key}（空串 = 清除）
 *   GET  /api/settings              → 全局模型 / 推理挡位（含默认值）
 *   PUT  /api/settings              → 合并保存
 *
 * mock 的持久化就是 ui/mock/settings.local.json（已 gitignore）。
 */

import type { Plugin } from "vite";
import type { IncomingMessage, ServerResponse } from "node:http";
import { readFileSync, writeFileSync, existsSync } from "node:fs";
import path from "node:path";
import { fileURLToPath } from "node:url";

type Next = (err?: unknown) => void;

const here = path.dirname(fileURLToPath(import.meta.url));
const fixturesDir = path.resolve(here, "../fixtures");
const settingsFile = path.join(here, "settings.local.json");

const EFFORT_LEVELS = ["off", "low", "medium", "high", "max"];

// 与 cli.py 的默认值对齐：架构师 high / Subagent medium / 廉价三件套 off
const DEFAULT_SETTINGS = {
  base_url_override: "",
  models: { architect: "", subagent: "", triage: "" },
  effort: { architect: "high", subagent: "medium", cheap: "off" },
  // 字符串 on/off 而不是布尔：它落到 .env，空串在那里 = 未设置 = 回落到默认
  review_writes: "on",
};

interface SettingsFile {
  providers?: Record<string, { api_key?: string }>;
  settings?: Partial<typeof DEFAULT_SETTINGS>;
}

function loadSettings(): Required<SettingsFile> {
  if (!existsSync(settingsFile)) return { providers: {}, settings: {} };
  try {
    const raw = JSON.parse(readFileSync(settingsFile, "utf8")) as SettingsFile;
    return { providers: raw.providers ?? {}, settings: raw.settings ?? {} };
  } catch {
    return { providers: {}, settings: {} };
  }
}

function saveSettings(data: Required<SettingsFile>): void {
  writeFileSync(settingsFile, JSON.stringify(data, null, 1), "utf8");
}

function sendJson(res: ServerResponse, status: number, body: unknown): void {
  res.statusCode = status;
  res.setHeader("content-type", "application/json; charset=utf-8");
  res.end(JSON.stringify(body));
}

function readFixture(name: string): unknown | null {
  const file = path.join(fixturesDir, name);
  if (!existsSync(file)) return null;
  return JSON.parse(readFileSync(file, "utf8"));
}

function drain(req: IncomingMessage): Promise<string> {
  return new Promise((resolve) => {
    let acc = "";
    req.on("data", (c) => (acc += c));
    req.on("end", () => resolve(acc));
  });
}

const handler = (req: IncomingMessage, res: ServerResponse, next: Next) => {
  void (async () => {
    const url = new URL(req.url ?? "/", "http://mock.local");
    if (!url.pathname.startsWith("/api/")) return next();

    if (url.pathname === "/api/stream") {
      res.writeHead(200, {
        "content-type": "text/event-stream",
        "cache-control": "no-cache",
        connection: "keep-alive",
      });
      res.write("retry: 3000\n\n");
      const timer = setInterval(() => res.write(": ping\n\n"), 15000);
      req.on("close", () => clearInterval(timer));
      return;
    }

    if (req.method === "GET" && url.pathname === "/api/tasks") {
      return sendJson(res, 200, readFixture("threads.json") ?? []);
    }

    const detail = url.pathname.match(/^\/api\/tasks\/([\w-]+)$/);
    if (req.method === "GET" && detail) {
      const d = readFixture(`${detail[1]}.json`);
      return d
        ? sendJson(res, 200, d)
        : sendJson(res, 404, { error: "not found" });
    }

    // 设置页的「测试连接」：mock 不打真实端点，回一个 skipped —— 它的语义正是
    // 「没测到」，不是「失败」，所以这里不用编一个假的成功
    const probe = url.pathname.match(/^\/api\/providers\/([\w-]+)\/test$/);
    if (req.method === "POST" && probe) {
      await drain(req);
      return sendJson(res, 200, {
        name: probe[1],
        status: "skipped",
        detail: "mock 不打真实端点 —— 起 cowork serve 才测得到",
      });
    }

    const act = url.pathname.match(/^\/api\/tasks\/([\w-]+)\/(intervene|cancel|ruling)$/);
    if (req.method === "POST" && act) {
      await drain(req);
      return sendJson(res, 202, { accepted: true });
    }

    // ---- 设置页 ----

    if (url.pathname === "/api/providers") {
      const providers = (readFixture("providers.json") ?? []) as Record<string, unknown>[];
      const saved = loadSettings().providers;
      const merged = providers.map((p) => {
        const key = saved[p.name as string]?.api_key ?? "";
        return {
          ...p,
          configured: key.length > 0,
          key_hint: key ? `····${key.slice(-4)}` : null,
        };
      });
      return sendJson(res, 200, merged);
    }

    const prov = url.pathname.match(/^\/api\/providers\/([\w-]+)$/);
    if (req.method === "PUT" && prov) {
      const body = JSON.parse((await drain(req)) || "{}") as { api_key?: string };
      const data = loadSettings();
      data.providers[prov[1]] = { api_key: body.api_key ?? "" };
      saveSettings(data);
      return sendJson(res, 200, { ok: true });
    }

    if (url.pathname === "/api/settings") {
      const data = loadSettings();
      if (req.method === "GET") {
        return sendJson(res, 200, {
          ...DEFAULT_SETTINGS,
          ...data.settings,
          models: { ...DEFAULT_SETTINGS.models, ...data.settings.models },
          effort: { ...DEFAULT_SETTINGS.effort, ...data.settings.effort },
        });
      }
      if (req.method === "PUT") {
        const body = JSON.parse((await drain(req)) || "{}") as Partial<typeof DEFAULT_SETTINGS>;
        for (const v of Object.values(body.effort ?? {})) {
          if (!EFFORT_LEVELS.includes(v)) {
            return sendJson(res, 400, { error: `unknown effort level: ${v}` });
          }
        }
        data.settings = { ...data.settings, ...body };
        saveSettings(data);
        return sendJson(res, 200, { ok: true });
      }
    }

    return sendJson(res, 404, { error: "not found" });
  })().catch(next);
};

export function mockApi(): Plugin {
  return {
    name: "cowork-mock-api",
    configureServer(server) {
      server.middlewares.use(handler);
    },
    configurePreviewServer(server) {
      server.middlewares.use(handler);
    },
  };
}

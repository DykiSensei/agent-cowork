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
  // 每个角色用哪一家（空 = 自动）。和 models 是两件事：那是模型 id，这是「谁来干」
  providers: { architect: "", reviewer: "", subagent: "" },
  workspace: "",
  // 反斜杠要转义：`"C:\Users\..."` 在 TS 里 `\U` 是个未知转义，会被静默吞掉，
  // 变成 `C:Usersyou...` —— 编译不报错，只是路径成了废话
  workspace_default: "C:\\Users\\you\\cowork-workspaces",
  effort: { architect: "high", subagent: "medium", cheap: "off" },
  // 字符串 on/off 而不是布尔：它落到 .env，空串在那里 = 未设置 = 回落到默认
  review_writes: "on",
  allowed_binaries: "",
  allow_network: "off",
  max_steps: "60",
  // 联网搜索（search_web）。**key 不在这里** —— 它只写不读，走
  // PUT /api/search/key，GET 只回末 4 位识别串。
  search: {
    provider: "",
    effective_provider: "zhipu",
    options: ["zhipu"],
    known: true,
    provider_key_env: "ZHIPUAI_API_KEY",
    dedicated_key_env: "COWORK_SEARCH_API_KEY",
    configured: false,
    key_source: null as "dedicated" | "provider" | null,
    key_hint: null as string | null,
  },
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

/** mock 只记住最近一次拆解 —— 它演的是形状，不是并发。 */
interface MockPlan {
  id: string;
  goal: string;
  asked: number;
  accepted?: boolean;
  takeover?: boolean;
  workspace?: string;
  /** 人改过并交上来的那份（`PlanRuling.specs`）。给了就以它为准。 */
  specs?: unknown[];
}
let mockPlan: MockPlan | null = null;

/**
 * 拆解结果直接借用 fixtures 里那条复合线程的子任务，形状因此和
 * `views.task_detail()` 里的 spec 完全一致（都出自 make_fixtures.py）。
 * 状态刻意演 AWAITING_HUMAN：那是 M7 三种终局里唯一需要界面出按钮的一种。
 */
/** 拆解草稿：和终局用同一批 spec，形状因此一致。 */
function mockPlanSpecs(): unknown[] {
  const detail = readFixture("task_comp.json") as
    | { tasks?: Record<string, { spec?: unknown }> }
    | null;
  return Object.values(detail?.tasks ?? {}).map((t) => t.spec);
}

function mockPlanResult(p: MockPlan): unknown {
  const detail = readFixture("task_comp.json") as
    | { tasks?: Record<string, { spec?: unknown }> }
    | null;
  const specs = p.specs ?? Object.values(detail?.tasks ?? {}).map((t) => t.spec);
  const accepted = p.accepted === true;
  return {
    plan_id: p.id,
    goal: p.goal,
    status: accepted ? "ACCEPTED" : "AWAITING_HUMAN",
    root_id: "task_comp",
    attempts: 2,
    tokens: 18342,
    decider: accepted ? "HUMAN" : "LLM",
    escalation_reason: accepted
      ? null
      : "已重生成 1 次仍未通过复核（阈值 max_regenerate=2）",
    rationale: accepted ? "人确认按当前拆解执行" : "需要人裁决这份拆解",
    specs,
    review: {
      structural: [],
      sufficient: false,
      missing: ["原始目标里的「一页」没有任何验收标准管它"],
      tokens: 1203,
      reviewer: "openai-compat:kimi-k3",
      independent: true,
    },
    dispatchable: accepted,
    workspace: p.takeover ? p.workspace : `${p.workspace}\\${p.id}`,
    takeover: Boolean(p.takeover),
    ruling_note: accepted ? "人确认拆解" : "",
    dispatched_root: null,
    available_providers: { deepseek: "deepseek-v4", kimi: "kimi-k3" },
  };
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

    // 发布任务 → 拆解 → 裁决 → 派发。mock 不真的拆解，只把**形状**走一遍：
    // 第一次问是 RUNNING，之后回一份取自 fixtures 的拆解。
    // 端点在这里必须存在，否则 `npm run dev` 里那个按钮是死的，而真实服务上它是活的
    // —— mock 和服务层分叉正是 fixtures 这套东西要防的事。
    if (req.method === "POST" && url.pathname === "/api/tasks") {
      const body = JSON.parse((await drain(req)) || "{}") as
        { goal?: string; workspace?: string; mode?: string };
      if (!(body.goal ?? "").trim()) {
        return sendJson(res, 400, { error: "goal 不能为空" });
      }
      mockPlan = {
        id: `plan_mock${Date.now().toString(36)}`,
        goal: body.goal!,
        asked: 0,
        takeover: body.mode === "takeover",
        workspace: body.workspace || DEFAULT_SETTINGS.workspace_default,
      };
      return sendJson(res, 202, { plan_id: mockPlan.id });
    }

    const plan = url.pathname.match(/^\/api\/plans\/([\w-]+)$/);
    if (req.method === "GET" && plan) {
      if (!mockPlan || mockPlan.id !== plan[1]) {
        return sendJson(res, 404, { error: "没有这次拆解" });
      }
      mockPlan.asked += 1;
      // 演进度：生成中 → 复核中 → 第二轮生成中 → 复核中 → 终局。
      // 不演的话 mock 上永远看不到进度面板，而它正是要验的东西。
      if (mockPlan.asked < 5) {
        const n = mockPlan.asked - 1; // 0..3
        return sendJson(res, 200, {
          plan_id: mockPlan.id,
          goal: mockPlan.goal,
          status: "RUNNING",
          error: null,
          started_at: Date.now() / 1000 - 40 - n * 30,
          progress: {
            phase: n % 2 === 0 ? "generating" : "reviewing",
            attempt: n < 2 ? 1 : 2,
            max_attempts: 3,
            tokens: 3200 * (n + 1),
          },
          // 第一轮生成完就把草稿摆出来 —— 读它比看转圈有用
          specs: n >= 1 ? mockPlanSpecs() : null,
          draft: true,
          workspace: mockPlan.workspace ?? "",
          takeover: Boolean(mockPlan.takeover),
        });
      }
      return sendJson(res, 200, mockPlanResult(mockPlan));
    }

    const planRule = url.pathname.match(/^\/api\/plans\/([\w-]+)\/ruling$/);
    if (req.method === "POST" && planRule) {
      const body = JSON.parse((await drain(req)) || "{}") as {
        accept?: boolean;
        specs?: unknown[];
      };
      if (!("accept" in body) && !body.specs) {
        return sendJson(res, 400, { error: "要么给 accept（true/false），要么给一份 specs" });
      }
      if (mockPlan) {
        // 人自己交了一份拆解：以它为准，并且直接可派发（同服务端 rule_plan）
        if (body.specs?.length) {
          mockPlan.specs = body.specs;
          mockPlan.accepted = true;
        } else {
          mockPlan.accepted = Boolean(body.accept);
        }
      }
      return sendJson(res, 200, { ok: true });
    }

    const planDispatch = url.pathname.match(/^\/api\/plans\/([\w-]+)\/dispatch$/);
    if (req.method === "POST" && planDispatch) {
      await drain(req);
      if (!mockPlan || mockPlan.id !== planDispatch[1]) {
        return sendJson(res, 404, { error: "没有这次拆解" });
      }
      // fixtures 里那条现成的复合线程 —— 派发之后界面该跳到它
      return sendJson(res, 202, { root_id: "task_comp" });
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

    // 专用搜索 key：只写不读（GET /api/settings 只回末 4 位）
    if (url.pathname === "/api/search/key" && req.method === "PUT") {
      const body = JSON.parse((await drain(req)) || "{}") as { api_key?: string };
      const key = (body.api_key ?? "").trim();
      if (key.includes("\n") || key.includes("\r")) {
        // 服务端真的会拒（.env 注入防线）—— mock 也要拒，否则前端那条
        // 「拒绝要显示出来」的分支在开发时永远走不到
        return sendJson(res, 400, { error: "值里不能有换行" });
      }
      const data = loadSettings();
      data.settings = {
        ...data.settings,
        search: {
          ...DEFAULT_SETTINGS.search,
          ...data.settings.search,
          configured: key.length > 0,
          key_source: key ? "dedicated" : null,
          key_hint: key ? `····${key.slice(-4)}` : null,
        },
      };
      saveSettings(data);
      return sendJson(res, 200, { ok: true });
    }

    if (url.pathname === "/api/search/test" && req.method === "POST") {
      const data = loadSettings();
      const on = data.settings.search?.configured ?? false;
      return sendJson(
        res,
        200,
        on
          ? {
              status: "ok",
              detail: "回了 3 条",
              sample: { title: "示例结果", url: "https://example.com" },
            }
          : { status: "failed", detail: "没配搜索 key" },
      );
    }

    if (url.pathname === "/api/settings") {
      const data = loadSettings();
      if (req.method === "GET") {
        return sendJson(res, 200, {
          ...DEFAULT_SETTINGS,
          ...data.settings,
          models: { ...DEFAULT_SETTINGS.models, ...data.settings.models },
          effort: { ...DEFAULT_SETTINGS.effort, ...data.settings.effort },
          search: { ...DEFAULT_SETTINGS.search, ...data.settings.search },
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

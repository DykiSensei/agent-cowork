/**
 * 翻译层的行为检查 —— 前端唯一一处**逻辑**，也是唯一 tsc 管不到的地方。
 *
 * 为什么值得有：`translate()` 决定「什么时候给人出裁决表单」，而它判错的方式是
 * 静默的 —— 卡片不出现，页面照样渲染，类型也全对。审计里就是这么栽的：
 * 判据写成「AWAITING_HUMAN 那条 status 事件恰好是整条时间线的最后一条」，
 * 而 orchestrator 有几条路径会在挂起之后再写一行 `[STOP]` 说明原因，
 * 于是那些情况下**人看得见挂起了却无处答复**。
 *
 * 不引测试框架：esbuild 是 vite 已有的依赖，node 自带断言，这个文件本身就是用例表。
 * 跑法：`npm run check`（`npm run build` 里也串了一道）。
 */

import assert from "node:assert/strict";
import { build } from "esbuild";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import path from "node:path";

const outfile = path.join(mkdtempSync(path.join(tmpdir(), "cowork-check-")), "t.mjs");
await build({
  entryPoints: ["src/translate.ts"],
  bundle: true,
  format: "esm",
  outfile,
  logLevel: "silent",
});
const { translate } = await import("file://" + outfile.replace(/\\/g, "/"));

const ev = (seq, kind, extra = {}) => ({
  id: `ev_${seq}`,
  task_id: "t",
  seq,
  kind,
  text: "",
  ref_id: null,
  payload: {},
  created_at: 1000 + seq,
  ...extra,
});

const AWAITING = (seq) => ev(seq, "status", { payload: { status: "AWAITING_HUMAN" } });

const detail = (events, pending) => ({
  kind: "single",
  state: {
    task_id: "t",
    parent_id: "p",
    revision: 1,
    goal: "g",
    status: "AWAITING_HUMAN",
    agent_id: null,
    current_step: 1,
    checkpoint_id: "ckpt_1",
    interrupt_count: 1,
    artifacts: [],
    signal_log: [],
    tokens_used: 10,
    started_at: 1,
    spec: { id: "t", acceptance: [], scope: [], tools: [], hard_signals: [] },
  },
  signals: {},
  decisions: {},
  events,
  pending:
    pending === undefined
      ? {
          reason: "架构师无法决策",
          suggestion: null,
          decision_id: null,
          checkpoint_id: "ckpt_1",
        }
      : pending,
});

const kinds = (events, pending) => translate(detail(events, pending)).map((e) => e.kind);

const cases = [
  [
    "status 是最后一条 —— 主路径",
    () => assert.ok(kinds([AWAITING(1)]).includes("awaiting")),
  ],
  [
    "挂起之后还有一行 [STOP] 日志 —— 老库和早期版本写出来就是这个顺序",
    () =>
      assert.deepEqual(
        kinds([AWAITING(1), ev(2, "log", { text: "[STOP] 架构师无法决策" })]),
        ["log", "awaiting"],
        "表单要跟在日志后面出现，而不是不出现",
      ),
  ],
  [
    "日志在前、status 在后 —— orchestrator 现在写出来的顺序",
    () =>
      assert.deepEqual(
        kinds([ev(1, "log", { text: "[STOP] 架构师无法决策" }), AWAITING(2)]),
        ["log", "awaiting"],
      ),
  ],
  [
    "历史上的挂起不出表单：后面又 RUNNING 了，这条已经翻篇",
    () =>
      assert.ok(
        !kinds([
          AWAITING(1),
          ev(2, "status", { payload: { status: "RUNNING" } }),
          ev(3, "log", { text: "[RUN ] cycle=2" }),
        ]).includes("awaiting"),
      ),
  ],
  [
    "服务端说不在等（pending=null）就不出表单 —— 那才是权威判据",
    () => assert.ok(!kinds([AWAITING(1)], null).includes("awaiting")),
  ],
  [
    "终局卡同样延后一拍，落在 [DONE] 日志后面",
    () =>
      assert.deepEqual(
        kinds(
          [
            ev(1, "status", { payload: { status: "COMPLETED" } }),
            ev(2, "log", { text: "[DONE] 验收通过" }),
          ],
          null,
        ),
        ["log", "terminal"],
      ),
  ],
];

let failed = 0;
for (const [name, fn] of cases) {
  try {
    fn();
    console.log(`✓ ${name}`);
  } catch (e) {
    failed++;
    console.error(`✗ ${name}\n  ${e.message}`);
  }
}
console.log(`\n${cases.length - failed}/${cases.length} 通过`);
process.exit(failed ? 1 : 0);

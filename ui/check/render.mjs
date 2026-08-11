/**
 * 渲染冒烟：拿 `fixtures/` 里的**真实**投影把每个界面渲一遍，看它出不出东西。
 *
 * 补的是 tsc 和 translate 检查都够不着的那一层：组件在真实数据上会不会炸
 * （某个字段是 undefined、某个 map 是空的、老库没有新字段），以及
 * 「该出现的字样出没出现」。
 *
 * 它不是视觉检查 —— 布局好不好看只能人看。它回答的是「打开会不会白屏」，
 * 而白屏正是实测反馈里最贵的一种（「提示服务不在运行，刷新后正常」）。
 */

import assert from "node:assert/strict";
import { build } from "esbuild";
import { readFileSync, readdirSync, mkdirSync } from "node:fs";
import path from "node:path";

// 产物必须落在项目内：react / react-dom 由 node 在运行时解析，
// 而在 node_modules 之外的临时目录里它找不到它们。这个目录本来就被忽略。
const outdir = path.resolve("node_modules/.cache/cowork-check");
mkdirSync(outdir, { recursive: true });
const outfile = path.join(outdir, "render.mjs");
await build({
  stdin: {
    contents: `
      export { renderToStaticMarkup } from "react-dom/server";
      export { default as React } from "react";
      export { default as LiteStream } from "./src/components/lite/LiteStream";
      export { default as Progress } from "./src/components/Progress";
      export { default as Details } from "./src/components/Details";
      export { default as NewTask } from "./src/components/NewTask";
      export { SearchCard } from "./src/components/Settings";
    `,
    resolveDir: process.cwd(),
    loader: "ts",
  },
  bundle: true,
  format: "esm",
  jsx: "automatic",
  platform: "node",
  // 只打包我们自己的源码；react / react-dom 交给 node 从 node_modules 解析
  // （react-dom/server 内部 require("stream")，打进来会变成「不支持动态 require」）
  packages: "external",
  define: { "process.env.NODE_ENV": '"production"' },
  outfile,
  logLevel: "silent",
});
const m = await import("file://" + outfile.replace(/\\/g, "/"));
const { renderToStaticMarkup: render, React } = m;

const fixtures = path.resolve("fixtures");
const load = (f) => JSON.parse(readFileSync(path.join(fixtures, f), "utf8"));
const details = readdirSync(fixtures)
  .filter((f) => f.startsWith("task_") && f.endsWith(".json"))
  .map((f) => [f, load(f)]);

const noop = async () => ({ ok: true });
const props = (detail) => ({
  taskId: detail.kind === "single" ? detail.state.task_id : "task_composite_root",
  detail,
  onIntervene: noop,
  onCancel: noop,
  onRuling: noop,
});

let failed = 0;
const check = (name, fn) => {
  try {
    fn();
    console.log(`✓ ${name}`);
  } catch (e) {
    failed++;
    console.error(`✗ ${name}\n  ${e.message}`);
  }
};

// 1. 每一份真实投影都要渲得出来（专业版已弃用，只剩一套界面）
for (const [file, detail] of details) {
  check(`${file} 渲染出内容`, () => {
    const html = render(React.createElement(m.LiteStream, props(detail)));
    assert.ok(html.length > 200, "渲出来是空的");
  });
}

check("技术细节抽屉在真实数据上不炸", () => {
  for (const [, detail] of details) {
    render(React.createElement(m.Details, { detail }));
  }
});

// 2. 进度面板：真实数据上要说得出「在做什么」
check("进度面板在复合线程上逐个子任务列出来", () => {
  const detail = load("task_composite_root.json");
  const html = render(React.createElement(m.Progress, { detail, lite: true }));
  const n = Object.keys(detail.progress).length;
  assert.ok(n >= 2, "这份 fixture 该有多个子任务");
  for (const p of Object.values(detail.progress)) {
    assert.ok(html.includes(p.goal.slice(0, 12)), `没列出 ${p.task_id}`);
  }
});

check("还没有子任务时，进度面板说人话而不是留白", () => {
  const html = render(
    React.createElement(m.Progress, {
      detail: {
        kind: "composite", state: null, root_goal: "x", plan: null, review: null,
        tasks: {}, events: [], signals: {}, decisions: {},
        pending_children: [], pending: {}, progress: {},
      },
      lite: true,
    }),
  );
  assert.ok(html.includes("正在拆解"), "空进度要解释「为什么还没有」");
});

// 3. 挂起的任务：表单必须真的渲出来（实测卡住的那一条）
check("挂起的任务渲得出裁决按钮", () => {
  const entry = details.find(
    ([, d]) => d.kind === "single" && d.state.status === "AWAITING_HUMAN",
  );
  assert.ok(entry, "fixtures 里该有一个挂起的任务");
  const html = render(React.createElement(m.LiteStream, props(entry[1])));
  assert.ok(html.includes("这件事需要你定一下"), "没有裁决卡");
  assert.ok(html.includes("放弃"), "没有可选的处理方式");
});

check("挂起的任务：输入框禁用，并且说清该去哪答复", () => {
  const entry = details.find(
    ([, d]) => d.kind === "single" && d.state.status === "AWAITING_HUMAN",
  );
  const html = render(React.createElement(m.LiteStream, props(entry[1])));
  assert.ok(html.includes("disabled"), "不能让人往一个必然被拒的框里打字");
  assert.ok(html.includes("上面那张卡片"), "要指出该去哪答复");
  assert.ok(!html.includes("step 边界"), "给用户看的话里不该有黑话");
});

check("排队中的任务不能被说成「已经结束」", () => {
  // 实测撞到的：任务在排队，底部却写「这条任务已经结束了」——
  // 因为分支只判了 running / waiting，剩下的一律当终局。
  const entry = details.find(([, d]) => d.kind === "single" && d.state.status === "PENDING");
  assert.ok(entry, "fixtures 里该有一个 PENDING 的任务");
  const html = render(React.createElement(m.LiteStream, props(entry[1])));
  assert.ok(!html.includes("已经结束"), "PENDING 不是终局");
  assert.ok(html.includes("还没开始跑"), "要说清它在等什么");
});

// 4. 发布任务：第一屏要有输入框和按钮
check("发布任务的第一屏可用", () => {
  const html = render(
    React.createElement(m.NewTask, { onDispatched: () => {}, onClose: () => {} }),
  );
  assert.ok(html.includes("textarea"), "没有输入框");
  assert.ok(html.includes("开始拆解"), "没有提交按钮");
});

check("发布页把「从零开始」和「接手已有项目」摆成两件事", () => {
  const html = render(
    React.createElement(m.NewTask, { onDispatched: () => {}, onClose: () => {} }),
  );
  assert.ok(html.includes("从零开始"), "缺「从零开始」");
  assert.ok(html.includes("接手已有项目"), "缺「接手已有项目」");
  // 「我的产物在哪」要在**发布之前**就看得见，而不是跑完了再去找
  assert.ok(html.includes("产物放在哪"), "没有工作区输入");
});

// 5. 联网搜索那张卡：三个问题都要有答案 —— 配哪家 / 配没配上 / 不配会怎样
const searchProps = (over = {}) => ({
  search: {
    provider: "",
    effective_provider: "zhipu",
    options: ["zhipu"],
    known: true,
    provider_key_env: "ZHIPUAI_API_KEY",
    dedicated_key_env: "COWORK_SEARCH_API_KEY",
    configured: false,
    key_source: null,
    key_hint: null,
    ...over,
  },
  networkOn: false,
  provider: "",
  onProvider: () => {},
});

check("搜索卡说得出「配哪家」和「不配会怎样」", () => {
  const html = render(React.createElement(m.SearchCard, searchProps()));
  assert.ok(html.includes("ZHIPUAI_API_KEY"), "没说该配哪个变量");
  assert.ok(html.includes("不配不影响其它任何功能"), "没说清不配的后果");
  assert.ok(html.includes("未配"), "没显示当前状态");
});

check("用的是哪一把 key 要说出来", () => {
  const html = render(
    React.createElement(
      m.SearchCard,
      searchProps({ configured: true, key_source: "provider", key_hint: "····abcd" }),
    ),
  );
  // 「已配置」而不说明用的是哪把，人就不知道该去哪儿改
  assert.ok(html.includes("····abcd"), "没显示识别串");
  assert.ok(html.includes("这家自己的 key"), "没说清用的是哪一把");
});

check("key 配好但联网还关着时，要点出来", () => {
  const html = render(
    React.createElement(
      m.SearchCard,
      { ...searchProps({ configured: true, key_source: "dedicated" }), networkOn: false },
    ),
  );
  assert.ok(html.includes("两个都要开"), "配好了却不生效，必须解释为什么");
});

console.log(`\n${failed === 0 ? "全部通过" : `${failed} 项失败`}`);
process.exit(failed ? 1 : 0);

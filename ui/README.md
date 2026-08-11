# ui/ —— M6 群聊界面层（ChatSurface）

React 18 + TypeScript + Vite。**一套界面**（原来的「专业版」已弃用 —— 两套界面
意味着每个改动都要做两遍，而实测反馈是它不好看、没人用；它独有的信息搬进了
`Details.tsx` 的折叠抽屉，没有跟着删掉）。另有独立的**设置页**：各家供应商
API Key、按角色选供应商、工作区、工具与联网闸门、全局模型/推理挡位。

## 跑起来

```bash
cd ui
npm install
npm run dev        # 开发（5173，HMR）
npm run build      # tsc --noEmit + npm run check + vite build
npm run check      # 翻译层的行为检查（见下）
npm run preview    # 预览生产构建（4173）
```

dev 和 preview 都自带 **mock API**（`mock/plugin.ts`），不需要 Python 后端在跑。

### 两道门，各管一半

`tsc --noEmit` 管形状，`npm run check` 管**行为**（两个文件：翻译层的用例表 +
拿真实 fixtures 渲一遍的冒烟）。前端逻辑几乎只有 `translate.ts`
（后端事件索引 → 前端时间线），而它判错的方式是静默的：
卡片不出现，页面照样渲染，类型也全对。
实际栽过一次：「等你拍板」的判据写成「那条 `status` 事件恰好是最后一条事件」，
而 orchestrator 会在挂起后再写一行 `[STOP]` 说明原因 ——
**人看得见挂起了，却没有表单可以答复**（开发文档 §11.20 第四条）。

不引测试框架：esbuild 是 vite 已有的依赖，node 自带 assert，那个文件本身就是用例表。

## 深链

- `#task_xxx` 选中某个线程
- `#settings` 设置页
- `#pro` 已失效（专业版弃用，链接留着不报错）

## mock 数据：全部来自真实运行

`fixtures/` 里的 JSON **不是手写的**（手写 mock 会和真实形状慢慢分叉），重新生成：

```bash
PYTHONPATH=src python ui/mock/make_fixtures.py   # 仓库根目录
```

它用脚本后端把 8 个真实场景（COMPLETED / AWAITING_HUMAN / FAILED / ABANDONED /
PENDING / RUNNING / INTERRUPTED 现场 / 复合任务）跑进同一个 sqlite，再用
`cowork.views` 导出 —— 和将来 FastAPI 服务层吐的是同一份形状。
`providers.json` 是 `cli.PROVIDERS` 预设表的导出（设置页数据源）。

## 结构

```
mock/make_fixtures.py  fixtures 生成器（见上）
mock/plugin.ts         mock API：§6 契约 + 设置页端点，透传 fixtures/
mock/settings.local.json  设置页的 mock 持久化（已 gitignore）
fixtures/              views 的真实输出（threads.json / <task_id>.json / providers.json）
src/types.ts           与 cowork.views 投影对齐的形状（M6-界面层接口.md §10）
src/translate.ts       翻译层：后端事件索引 → 前端时间线（呈现规则都注释在里面）
src/copy.ts            「人话翻译」映射表 + composerPhase（底部输入区四态）
src/api.ts             fetch 客户端；写类调用统一返回 ActionResult{ok,error}
check/translate.mjs    翻译层的行为检查
check/render.mjs       渲染冒烟：拿真实 fixtures 把界面渲一遍，断言该出现的字样出现了
src/components/lite/   界面本体（专业版已弃用，见下）
src/components/Progress.tsx   现在在干什么（每个 Subagent 一行）
src/components/Details.tsx    技术细节抽屉（spec / 验收 / 硬信号 / 预算）
src/components/FolderPicker.tsx  文件夹选择器（服务端列目录，界面上点）
src/components/NewTask.tsx   发布任务：从零开始 / 接手已有项目 → 拆解 → 裁决 → 派发
src/components/Settings.tsx  设置页
prototype.html         最初的静态视觉稿（设计定稿依据，已被 React 版取代）
shot-app-*.png         React 版的自检截图
```

## mock 端点

读类：`GET /api/tasks`、`GET /api/tasks/:id`（views 投影原样透传）。
写类：`POST /api/tasks/:id/intervene|cancel|ruling`（202，mock 不驱动任何任务；
真实服务里 cancel 对不在运行的任务回 409）。
发布任务：`POST /api/tasks` → `GET /api/plans/:id` → `POST /api/plans/:id/ruling`
→ `POST /api/plans/:id/dispatch`。**mock 不真的拆解**，只把形状和三种终局走一遍
（问三次之后回一份 AWAITING_HUMAN 的拆解，裁决同意后变 ACCEPTED、可派发）——
端点在 mock 里必须存在，否则 `npm run dev` 里那个按钮是死的而真实服务上是活的，
而 mock 与服务层分叉正是 fixtures 这套东西要防的事。
`GET /api/stream` 是 SSE，只发心跳。

**写类调用一律要看返回值。** 服务端的拒绝有意义：409 = 任务不在运行中、
400 = 值里有换行（`.env` 注入防线）。原来这些在界面上全都长得像成功，
最刺眼的是 409 之后清空输入框并弹「已告诉它」—— 那句话其实哪儿都没到。
设置页：`GET/PUT /api/providers[/:name]`、`POST /api/providers/:name/test`、
`GET/PUT /api/settings` —— key 只写不读，回包只有 `configured` + 最后 4 位识别串。

设置页上三件事是分开的，别再合并：**`preset_verified`**（我们验证过这一行的
model id）/ **`configured`**（你填了 key）/ **「测试连接」的结果**（你的 key 现在
能不能用）。另有 **写入侧复核开关**（`review_writes`，字符串 on/off 不是布尔 ——
它落进 .env，空串在那里等于未设置、会回落到默认开）。

## 真实服务层（已实现，`src/cowork/server/`）

```bash
pip install -e .[server]
python -m cowork.cli serve          # HTTP + SSE + 本目录 dist/ 的静态 UI，只绑 loopback
```

**只绑 loopback 是硬拦不是默认值**：`--host` 指向非回环地址会直接拒绝启动
（要过必须显式 `--i-know-its-exposed`）。理由是设置页能读写各家 API key。

端点与 mock 完全一致，前端不用改任何东西。与 mock 的差别：

- 写端是真的：intervene 产生 `HUMAN_INTERVENTION` 抢占；ruling 走 restore 路径
  （`Orchestrator.restore` 从 checkpoint 重建现场继续跑）；
- `POST /api/tasks {goal}` → 起拆解（`architect.plan`），`POST /api/plans/:id/dispatch`
  派发执行；plan 注册表在内存里（服务重启丢未派发的 plan）；
- SSE 推的是真事件（`TapStore` 写入处发事件）；前端收到通知后回源重拉，
  断线重连用 `task_detail(after_seq=)` 增量；
- 设置页写 .env（立即生效于新起跑的任务）。

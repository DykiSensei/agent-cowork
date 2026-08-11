# ui/ —— M6 群聊界面层（ChatSurface）

React 18 + TypeScript + Vite。双模式：**简洁版（默认，小白向）/ 专业版（开发者向）**，
同一份数据的两种呈现，右上角切换（`localStorage` 记忆）。另有独立的**设置页**
（各家供应商 API Key + 全局模型/推理挡位），两个模式的顶栏都有入口。

## 跑起来

```bash
cd ui
npm install
npm run dev        # 开发（5173，HMR）
npm run build      # tsc --noEmit + vite build
npm run preview    # 预览生产构建（4173）
```

dev 和 preview 都自带 **mock API**（`mock/plugin.ts`），不需要 Python 后端在跑。

## 深链

- `#pro` 专业模式（默认简洁）
- `#task_xxx` 选中某个线程
- `#settings` 设置页
- 可叠加：`#pro,task_comp`

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
src/copy.ts            lite 模式的「人话翻译」映射表
src/api.ts             fetch 客户端；接真实服务时改这里 + vite proxy
src/components/pro/    专业版（暗色，信息全量）
src/components/lite/   简洁版（亮色，只保留叙事线）
src/components/Settings.tsx  设置页
prototype.html         最初的静态视觉稿（设计定稿依据，已被 React 版取代）
shot-app-*.png         React 版的自检截图
```

## mock 端点

读类：`GET /api/tasks`、`GET /api/tasks/:id`（views 投影原样透传）。
写类：`POST /api/tasks/:id/intervene|cancel|ruling`（202，mock 不驱动任何任务；
真实服务里 cancel 对不在运行的任务回 409）。
`GET /api/stream` 是 SSE，只发心跳。
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

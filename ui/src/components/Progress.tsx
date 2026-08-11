import { fmtTok, liteDoingText, proDoingText } from "../copy";
import type { TaskDetail, TaskProgress } from "../types";

/**
 * 「现在在干什么」—— 一条线程里每个 Subagent 的实时进度。
 *
 * 存在的理由是实测反馈：**任务跑起来之后，界面上只有一个状态点和一串日志**，
 * 人看不出系统是在干活还是卡住了，也看不出四个子任务里哪个在跑、跑到哪了。
 * 时间线回答的是「发生过什么」，这里回答的是「此刻怎么样」——两个问题。
 *
 * 数据全部来自 `views.task_progress()`，都是确定性的：第几步、烧了多少、
 * 最后一个动作是什么。**不做判断**（「是不是卡住了」归人，「要不要干预」归架构师）。
 *
 * 架构师那一栏不在这里 —— 它的活动是跨任务的，落在时间线上（`[DEC]` / `[PROBE]`
 * / `[PLAN]` 那些行），这里只显示它最近说的那一句（`architectLine`）。
 */

function pct(used: number, total: number | null | undefined): number {
  if (!total || total <= 0) return 0;
  return Math.min(100, Math.round((used / total) * 100));
}

function Row({
  p,
  lite,
  awaiting,
}: {
  p: TaskProgress;
  lite: boolean;
  awaiting: boolean;
}) {
  const doing = lite ? liteDoingText(p) : proDoingText(p);
  const status = awaiting ? "AWAITING_HUMAN" : p.status;
  return (
    <div className={`pg-row ${status}`}>
      <span className={`pg-dot ${status}`} />
      <div className="pg-main">
        <div className="pg-goal" title={p.goal}>
          {p.goal}
        </div>
        <div className="pg-doing">
          {doing}
          {p.last_result && p.last_result.ok === false && (
            <span className="pg-bad">
              　上一步失败（exit {p.last_result.exit_code}）
            </span>
          )}
        </div>
        {p.last_action?.thought && lite && (
          <div className="pg-thought">「{p.last_action.thought}」</div>
        )}
      </div>
      <div className="pg-nums">
        <div className="pg-bar" title={`第 ${p.current_step} / ${p.max_steps} 步`}>
          <i style={{ width: `${pct(p.current_step, p.max_steps)}%` }} />
        </div>
        <div className="pg-meta">
          {p.current_step}/{p.max_steps} 步 · {fmtTok(p.tokens_used)} tok
          {p.revision > 1 && ` · 第 ${p.revision} 版`}
        </div>
      </div>
    </div>
  );
}

export default function Progress({
  detail,
  lite = false,
}: {
  detail: TaskDetail;
  lite?: boolean;
}) {
  const rows: { p: TaskProgress; awaiting: boolean }[] =
    detail.kind === "composite"
      ? Object.values(detail.progress).map((p) => ({
          p,
          awaiting: detail.pending_children.includes(p.task_id),
        }))
      : [{ p: detail.progress, awaiting: detail.state.status === "AWAITING_HUMAN" }];

  if (!rows.length) {
    return (
      <div className="pg">
        <div className="pg-hd">进度</div>
        <div className="pg-empty">
          还没有子任务在跑 —— 正在拆解这个目标，拆完并确认之后才开始执行。
        </div>
      </div>
    );
  }

  const live = rows.filter((r) => !r.p.terminal).length;
  const spent = rows.reduce((a, r) => a + r.p.tokens_used, 0);
  // 产物落在哪。子任务共用同一个工作区，所以取第一个非空的就行 ——
  // 「我的东西在哪」以前在这套界面上根本没有答案
  const ws = rows.map((r) => r.p.workspace).find(Boolean);

  // 架构师最近说的那一句：它的活动是跨任务的，落在日志行上
  const architectLine = [...detail.events]
    .reverse()
    .find((e) => e.kind === "log" && /^\s*\[(DEC|PROBE|REVIEW|PLAN|SOFT)/.test(e.text));

  return (
    <div className="pg">
      <div className="pg-hd">
        进度
        <span className="pg-sum">
          {rows.length - live}/{rows.length} 完成 · 合计 {fmtTok(spent)} tok
        </span>
      </div>
      {ws && (
        <div className="pg-ws" title={ws}>
          产物在 <code>{ws}</code>
        </div>
      )}
      {rows.map(({ p, awaiting }) => (
        <Row key={p.task_id} p={p} lite={lite} awaiting={awaiting} />
      ))}
      {architectLine && (
        <div className="pg-arch">
          <span className="pg-arch-tag">架构师</span>
          <span>{architectLine.text.trim()}</span>
        </div>
      )}
    </div>
  );
}

import { useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { ActionResult } from "../../api";
import { dispatchPlan } from "../../api";
import {
  LITE_ACTION,
  LITE_ACTION_SUGGEST,
  composerPhase,
  liteRationale,
  liteSignalBody,
  liteSignalTitle,
  liteWaitText,
} from "../../copy";
import Details from "../Details";
import Progress from "../Progress";
import { translate } from "../../translate";
import type {
  DecisionRecord,
  PendingRuling,
  PlanData,
  PlanView,
  Signal,
  StreamEvent,
  TaskDetail,
  TaskState,
} from "../../types";

/**
 * lite 模式的事件流：同一份数据，只渲染「叙事线」——
 * 日志刻度大多不渲染，信号变成平白卡片，裁决变成「你的决定 / 系统自己处理了」。
 */

function sysLine(text: string): ReactNode {
  return <div className="l-sys-line">{text}</div>;
}

/** 日志行 → lite 的系统细字；返回 null 表示不渲染。 */
function liteLogLine(text: string): string | null {
  const t = text.trimStart();
  if (t.startsWith("[RUN")) {
    const rev = /rev=(\d+)/.exec(t)?.[1];
    if (rev && Number(rev) > 1) return "已带着已有成果，按新方案继续";
    if (t.includes("cycle=1")) return "已开始执行";
    return null;
  }
  if (t.startsWith("[CONFLICT")) return "发现两个任务改了同一个文件，已交给架构师仲裁";
  return null; // STOP / PROBE / SOFT / DONE / LAYER / REVIEW 都被卡片或终局覆盖
}

function ProblemCard({ sig }: { sig: Signal }) {
  return (
    <div className="l-card l-problem">
      <div className="l-card-t">
        <span className="l-dot red" />
        {liteSignalTitle(sig.type)}
      </div>
      <div className="l-card-b">{liteSignalBody(sig)}</div>
      {sig.raw_evidence && (
        <details className="l-ev">
          <summary>技术细节</summary>
          <pre>{sig.raw_evidence}</pre>
        </details>
      )}
    </div>
  );
}

function YourDecisionCard({ d }: { d: DecisionRecord }) {
  return (
    <>
      {d.escalation_reason && sysLine("系统觉得这事不该自己拍板，把问题送了上来")}
      <div className="l-card l-yours">
        <div className="l-card-t">
          <span className="l-dot blue" />
          你的决定
        </div>
        <div className="l-card-b">{liteRationale(d)}</div>
      </div>
    </>
  );
}

/**
 * 已经答过的裁决：decision_id → 人当时的选择。
 *
 * 放在模块级而不是组件里，因为这张卡会随 detail 刷新而重建 ——
 * 组件内的状态活不过一次刷新，而「我已经处理过了」必须活过。
 */
const ANSWERED = new Map<string, string>();

/**
 * 终局之后刚说出去、还没在事件流里露面的那句话：task_id → 原话。
 *
 * **同 `ANSWERED` 的理由，是同一条坑的另一面**（§11.26）：追加要求是
 * 「202 + 后台干活」—— 收下之后才起线程 restore，restore 里还有一次模型调用
 * （REBASE 要摘要）。这期间服务端如实还在报「做完了」，人刚说的那句话
 * 在界面上无影无踪，看着像没发出去。
 *
 * 按 task_id 记是对的（不像裁决那样要按 decision_id）：一条线程同一时刻
 * 只可能有一次「刚说完还没落地」的追加。
 */
const SAID = new Map<string, string>();

/** 给行为检查用：模拟「刚追加了一句，服务端还没回声」。 */
export function markSaid(taskId: string, text: string): void {
  SAID.set(taskId, text);
}

/** 给行为检查用：模拟「人已经答过这条裁决」。 */
export function markAnswered(decisionId: string, label: string): void {
  ANSWERED.set(decisionId, label);
}

function WaitCard({
  pending,
  title,
  onSubmit,
}: {
  pending: PendingRuling;
  /** 复合线程上等人的是某个子任务 —— 要说清楚是哪一步在等 */
  title?: string;
  onSubmit: (
    action: string,
    specChanges?: Record<string, unknown>,
  ) => Promise<ActionResult>;
}) {
  // **已经答过的那次裁决，不能再弹一次。**
  //
  // `POST /ruling` 是 202：服务端收下之后才起线程去 restore，而 restore 里有
  // 模型调用。也就是说从点下按钮到 status 真的翻掉，中间有一段真空 ——
  // 这期间服务端**如实**还在报 AWAITING_HUMAN，轮询/SSE 一刷新，卡片就又回来了，
  // 看起来像「我明明处理过了」。组件自己的 done 也扛不住：父层换了 detail
  // 之后这张卡会重建，本地状态跟着没。
  //
  // 所以记在模块级、按 `decision_id` 记：人答的是**那一个问题**。
  // 下一次升级会带一个新的 decision_id，卡片照常出现 —— 抑制不会粘住。
  const answeredKey = pending.decision_id ?? "";
  const [done, setDone] = useState<string | null>(
    () => (answeredKey && ANSWERED.get(answeredKey)) || null,
  );
  // 服务端可能不收这条裁决（任务已经不在挂起、并发答复过了）。
  // 那时候必须说出来 —— 显示「已告诉系统」而实际没记下，比报错难查得多。
  const [failed, setFailed] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const suggestion = pending.suggestion;

  const remember = (label: string) => {
    if (answeredKey) ANSWERED.set(answeredKey, label);
    setDone(label);
  };

  if (done) {
    return (
      <div className="l-wait">
        <div className="l-wait-t">已告诉系统</div>
        <p>
          你的决定：<b>{done}</b>。系统会带着这个决定接着办。
        </p>
        <div className="l-note">真实链路里任务会从上次停下的地方继续。</div>
      </div>
    );
  }

  const actions: DecisionRecord["action"][] = [
    "CONTINUE",
    "MODIFY_TASK",
    "REASSIGN",
    "ABANDON",
  ];
  return (
    <div className="l-wait">
      <div className="l-wait-t">
        这件事需要你定一下
        {title && <span className="l-wait-which">{title}</span>}
      </div>
      <p>{liteWaitText(pending.reason)}</p>
      {suggestion && (
        <div className="l-suggest">
          系统的建议：<b>{LITE_ACTION_SUGGEST[suggestion.action]}</b>
          {suggestion.rationale ? `，${suggestion.rationale}` : ""}
        </div>
      )}
      {/* 撞了 `run` 的程序白名单：这不该让人跑去设置页改一行环境变量，
          那件事和「这一刻要不要放行它」根本不是同一个决定。
          放行只作用于**这个任务**，落在它的 spec 上。 */}
      {pending.blocked_binary && (
        <div className="l-grant">
          它想跑 <code>{pending.blocked_binary}</code>，而这个程序不在允许清单里。
          <button
            className="primary"
            disabled={busy}
            onClick={() => {
              setBusy(true);
              setFailed(null);
              void onSubmit("MODIFY_TASK", {
                allow_binary: pending.blocked_binary!,
              })
                .then((r) => {
                  if (r.ok) remember(`已允许 ${pending.blocked_binary}，接着跑`);
                  else setFailed(r.error ?? "没能提交");
                })
                .finally(() => setBusy(false));
            }}
          >
            允许 {pending.blocked_binary} 并继续
          </button>
          <span className="l-grant-note">只对这个任务生效</span>
        </div>
      )}
      <div className="l-choices">
        {actions.map((a) => (
          <button
            key={a}
            className={
              !pending.blocked_binary && suggestion?.action === a
                ? "primary"
                : undefined
            }
            disabled={busy}
            onClick={() => {
              setBusy(true);
              setFailed(null);
              void onSubmit(a)
                .then((r) => {
                  if (r.ok) remember(LITE_ACTION[a]);
                  else setFailed(r.error ?? "没能提交");
                })
                .finally(() => setBusy(false));
            }}
          >
            {LITE_ACTION[a]}
          </button>
        ))}
      </div>
      {failed && <div className="l-fail">没能提交：{failed}</div>}
      <div className="l-note">
        放弃后任务就停了，不可恢复。也可以先不理会 —— 挂着不花一分钱。
      </div>
    </div>
  );
}

function PlanSteps({
  plan,
  tasks,
  pendingChildren,
}: {
  plan: PlanData;
  tasks: Record<string, TaskState>;
  pendingChildren: string[];
}) {
  return (
    <>
      {sysLine(`系统把任务拆成 ${Object.keys(tasks).length} 步，并自动检查了拆分方案`)}
      <div className="l-card" style={{ maxWidth: "100%" }}>
        <div className="l-card-t">
          <span className="l-dot green" />
          执行方案
        </div>
        <div className="l-steps">
          {plan.layers.flatMap((layer) =>
            layer.map((tid, ti) => {
              const t = tasks[tid];
              const waiting = pendingChildren.includes(tid);
              const done = t?.status === "COMPLETED";
              return (
                <div className="l-step" key={tid}>
                  {waiting ? (
                    <span className="l-dot amber" />
                  ) : done ? (
                    <span className="l-ck">✓</span>
                  ) : (
                    <span
                      className={`l-dot ${t?.status === "RUNNING" ? "blue" : "gray"}`}
                    />
                  )}
                  <span className="l-step-goal">{t?.spec.goal ?? tid}</span>
                  {waiting && <span className="l-simul">等你处理</span>}
                  {layer.length > 1 && ti > 0 && (
                    <span className="l-simul">与上一步同时做</span>
                  )}
                  {t?.spec.scope[0] && (
                    <span className="l-file">{t.spec.scope[0]}</span>
                  )}
                </div>
              );
            }),
          )}
        </div>
      </div>
    </>
  );
}

function TermView({ ev }: { ev: Extract<StreamEvent, { kind: "terminal" }> }) {
  if (ev.status === "COMPLETED") {
    return (
      <div className="l-hero">
        <div className="big-ck">✓</div>
        <div className="h-t">全部完成</div>
        <div className="h-s">{ev.chips.join(" · ")}</div>
      </div>
    );
  }
  return (
    <div className="l-card">
      <div className="l-card-t">
        <span className={`l-dot ${ev.status === "FAILED" ? "red" : "gray"}`} />
        {ev.status === "FAILED" ? "没跑成" : "已放弃"}
      </div>
      <div className="l-card-b">{ev.chips.join(" · ")}</div>
    </div>
  );
}

/** 详情页上的「没派发的拆解」面板（M12 待办 #1）。
 *  拆解中 / 等拍板 / 等派发三个状态各有一个入口，发布框不再是唯一入口。 */
function PlanPanel({
  plan,
  onResume,
  onDispatched,
}: {
  plan: PlanView;
  onResume: (planId: string) => void;
  onDispatched: (rootId: string) => void;
}) {
  const [busy, setBusy] = useState(false);
  const [err, setErr] = useState<string | null>(null);

  const dispatch = () => {
    if (busy) return;
    setBusy(true);
    setErr(null);
    void dispatchPlan(plan.plan_id).then((r) => {
      if (!r.ok || !r.root_id) {
        setErr(r.error ?? "派发没有返回 root_id");
        setBusy(false);
        return;
      }
      onDispatched(r.root_id);
      setBusy(false);
    });
  };

  const n = plan.specs?.length ?? 0;
  const status = plan.status;

  return (
    <div className="l-planpanel">
      {status === "RUNNING" && (
        <>
          <div className="l-planpanel-t">正在拆解</div>
          <div className="l-planpanel-b">
            {plan.progress
              ? `第 ${plan.progress.attempt}/${plan.progress.max_attempts} 轮 · ${
                  plan.progress.phase === "generating" ? "生成中" : "复核中"
                } · 已用 ${plan.progress.tokens} token`
              : "拆解刚开始…"}
          </div>
          <div className="l-planpanel-note">
            拆完会在这里接着走 —— 不用守着发布框。
          </div>
        </>
      )}

      {status === "ERROR" && (
        <>
          <div className="l-planpanel-t">拆解没成</div>
          <div className="l-planpanel-b">{plan.error ?? "未知错误"}</div>
        </>
      )}

      {status === "AWAITING_HUMAN" && !plan.dispatchable && (
        <>
          <div className="l-planpanel-t">架构师拆完了，等你拍板</div>
          <div className="l-planpanel-b">{n} 个子任务</div>
          <button className="nt-primary" onClick={() => onResume(plan.plan_id)}>
            去裁决
          </button>
        </>
      )}

      {plan.dispatchable && !plan.dispatched_root && (
        <>
          <div className="l-planpanel-t">拆解通过，等你派发</div>
          <div className="l-planpanel-b">
            {n > 0 ? `${n} 个子任务，确认后就开始花钱执行。` : ""}
          </div>
          {plan.specs?.length ? (
            <ol className="l-planpanel-specs">
              {plan.specs.slice(0, 6).map((s, i) => (
                <li key={i}>{s.goal}</li>
              ))}
              {plan.specs.length > 6 && <li>…</li>}
            </ol>
          ) : null}
          {err && <div className="nt-line bad">{err}</div>}
          <div className="l-planpanel-row">
            <button className="nt-ghost" onClick={() => onResume(plan.plan_id)}>
              回发布框看看
            </button>
            <button className="nt-primary" disabled={busy} onClick={dispatch}>
              {busy ? "派发中…" : "开始执行"}
            </button>
          </div>
        </>
      )}

      {status === "REJECTED" && (
        <div className="l-planpanel-t">这份拆解被否决了</div>
      )}
    </div>
  );
}

export default function LiteStream({
  taskId,
  detail,
  onIntervene,
  onFollowUp,
  onCancel,
  onRuling,
  onResumePlan,
  onDispatched,
}: {
  taskId: string;
  detail: TaskDetail;
  onIntervene: (taskId: string, text: string) => Promise<ActionResult>;
  onFollowUp: (taskId: string, text: string) => Promise<ActionResult>;
  onCancel: (taskId: string, reason: string) => Promise<ActionResult>;
  onRuling: (
    taskId: string,
    action: string,
    rationale: string,
    specChanges?: Record<string, unknown>,
  ) => Promise<ActionResult>;
  /** 从详情页跳回发布框，接着裁决 / 派发那条拆解（M12 待办 #1）。 */
  onResumePlan: (planId: string) => void;
  /** 详情页直接派发成功后，选中并刷新那条线程。 */
  onDispatched: (rootId: string) => void;
}) {
  const [bar, setBar] = useState(false);
  // 刚追加、服务端还没回声的那句话 —— 状态在模块级的 `SAID` 里（见它的注释）
  const [, bump] = useState(0);
  // 介入 / 取消都可能被服务端拒（最常见的是 409「任务不在运行中」）。
  // 原来这里 `.then()` 一律当成功：清空输入框 + 弹「已告诉它」，
  // 而那条指令其实哪儿都没到。
  const [failed, setFailed] = useState<string | null>(null);
  const [stopping, setStopping] = useState(false);
  const events = useMemo(() => translate(detail), [detail]);

  // **介入要发给正在跑的那个东西。** 复合线程自己不是任务（root 没有 tasks 行），
  // 发给它必然 409 —— 实测就撞在这里：底下聊天框发不出去，而提示里全是黑话。
  // 所以这里先算出「这句话该发给谁」：单任务发给它自己，复合发给在跑的子任务。
  const targets =
    detail.kind === "composite"
      ? Object.values(detail.progress)
          .filter((p) => p.status === "RUNNING" || p.status === "INTERRUPTED")
          .map((p) => ({ id: p.task_id, label: p.goal }))
      : detail.state.status === "RUNNING" || detail.state.status === "INTERRUPTED"
        ? [{ id: taskId, label: detail.state.goal }]
        : [];
  const [pick, setPick] = useState<string | null>(null);
  const target = targets.find((t) => t.id === pick) ?? targets[0] ?? null;
  const phase = composerPhase(detail);
  const running = phase === "running" && targets.length > 0;
  // **终局之后也能说话**（M12）。原来这里只有一句「这条任务已经结束了」，
  // 而「一次做不到位、要改几轮」恰恰是最常见的形状 —— 人只能重发一个任务，
  // 把已经做出来的东西丢掉。追加要求发给**线程本身**（不是某个子任务）：
  // 复合线程那边是带着现状再拆一轮，落点得是 root。
  const canFollowUp = phase === "done";
  const canTalk = running || canFollowUp;

  const submitCancel = () => {
    if (stopping || !target) return;
    setStopping(true);
    setFailed(null);
    void onCancel(target.id, "")
      .then((r) => {
        if (!r.ok) setFailed(r.error ?? "没能停下来");
      })
      .finally(() => setStopping(false));
  };

  const submitSay = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const input = e.currentTarget.querySelector("input")!;
    const text = input.value.trim();
    if (!text) return;
    // 在跑 → 介入那个正在跑的子任务；已终局 → 追加要求发给线程本身
    if (!running && !canFollowUp) return;
    if (running && !target) return;
    setFailed(null);
    const sent = running
      ? onIntervene(target!.id, text)
      : onFollowUp(taskId, text);
    void sent.then((r) => {
      if (!r.ok) {
        // **不清空输入框**：那句话还没送到，人可能想改改再发一次
        setFailed(r.error ?? "没能送出去");
        return;
      }
      input.value = "";
      if (!running) {
        SAID.set(taskId, text);
        bump((n) => n + 1);
      }
      setBar(true);
      setTimeout(() => setBar(false), 4000);
    });
  };

  // 那句话已经落进事件流了 —— 乐观气泡功成身退。
  // **按内容比对而不是按时间清除**：restore 里那次模型调用要多久没人知道，
  // 定个秒数就是猜（同挂起那张卡「按 decision_id 记」的理由）。
  const said = SAID.get(taskId) ?? null;
  if (said !== null && events.some((e) => e.kind === "human" && e.text === said)) {
    SAID.delete(taskId);
  }
  const pendingSaid = SAID.get(taskId) ?? null;

  return (
    <main className="l-stream">
      {/* 没派发的拆解：拆解中 / 等拍板 / 等派发三个状态在这里接管（M12 待办 #1） */}
      {detail.kind === "composite" && detail.plan_entry ? (
        <PlanPanel
          plan={detail.plan_entry}
          onResume={onResumePlan}
          onDispatched={onDispatched}
        />
      ) : null}
      {/* 「现在在干什么」—— 时间线说的是发生过什么，这里说的是此刻怎么样 */}
      <Progress detail={detail} lite />
      {/* 专业版弃用之后，它独有的信息（spec / 验收标准 / 硬信号 / 预算）在这里 */}
      <Details detail={detail} />
      <div className="l-scroll">
        <div className="l-col">
          {events.map((ev, i) => {
            switch (ev.kind) {
              case "human":
                return (
                  <div className="l-me" key={i}>
                    <div className="bub">{ev.text}</div>
                  </div>
                );
              case "log": {
                const line = liteLogLine(ev.text);
                return line ? <div key={i}>{sysLine(line)}</div> : null;
              }
              case "signal":
                return ev.signal.level === "L1" ? null : (
                  <ProblemCard key={i} sig={ev.signal} />
                );
              case "decision":
                return ev.decision.decider === "HUMAN" ? (
                  <YourDecisionCard key={i} d={ev.decision} />
                ) : (
                  <div key={i}>
                    {sysLine(`系统自己处理了：${liteRationale(ev.decision)}`)}
                  </div>
                );
              case "awaiting":
                return (
                  <WaitCard
                    key={i}
                    pending={ev.pending}
                    title={ev.title}
                    // 复合线程上要发给**那个子任务**：root 没有 tasks 行，
                    // 发给它只会得到一个 404
                    onSubmit={(action, specChanges) =>
                      onRuling(ev.taskId ?? taskId, action, "", specChanges)
                    }
                  />
                );
              case "plan":
                return (
                  <PlanSteps
                    key={i}
                    plan={ev.plan}
                    tasks={ev.tasks}
                    pendingChildren={ev.pendingChildren}
                  />
                );
              case "terminal":
                return <TermView key={i} ev={ev} />;
            }
          })}
          {pendingSaid !== null && (
            <>
              <div className="l-me">
                <div className="bub">{pendingSaid}</div>
              </div>
              {sysLine("已经交给它了，正在带着现有成果重新起跑…")}
            </>
          )}
        </div>
      </div>

      <div className="l-composer">
        <div className={`l-sysbar${bar ? " show" : ""}`}>
          {canFollowUp
            ? "已经交给它了 —— 它会带着现有的成果接着做。"
            : "已告诉它，等它做完手头这一小步就照你说的办。"}
        </div>
        {failed && <div className="l-fail">{failed}</div>}
        {targets.length > 1 && (
          <div className="l-target">
            这句话说给：
            <select value={target?.id} onChange={(e) => setPick(e.target.value)}>
              {targets.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.label}
                </option>
              ))}
            </select>
          </div>
        )}
        <div className="row">
          <form className="row" style={{ display: "contents" }} onSubmit={submitSay}>
            <input
              placeholder={
                running
                  ? "插一句话，改变它接下来的做法…"
                  : canFollowUp
                    ? "还要改什么？说一句，它接着做…"
                    : phase === "queued"
                      ? "还没开始跑，等它动起来"
                      : "在上面那张卡片里答复"
              }
              autoComplete="off"
              disabled={!canTalk}
            />
            <button type="submit" disabled={!canTalk}>
              {canFollowUp ? "接着改" : "发送"}
            </button>
          </form>
          {running && (
            <button type="button" className="l-stop" onClick={submitCancel} disabled={stopping}>
              {stopping ? "正在停…" : "停下来"}
            </button>
          )}
        </div>
        <div className="l-hint">
          {/* **说不能做什么的时候，同时说该做什么。** 实测反馈：卡在「等你处理」
              时聊天框发不出去，而提示只说了「任务不在运行中（下一个 step 边界）」
              —— 既没说该去哪答复，也没人知道 step 边界是什么。 */}
          {phase === "running" ? (
            <>
              你的话会直接变成它的新指令，不是闲聊。它会把手头这一小步做完再照办
              （通常一两秒）。「停下来」是彻底不做了，已经写出来的东西会留着。
            </>
          ) : phase === "waiting" ? (
            <>它停下来在等你拍板 —— 请在上面那张卡片里选一个处理方式，这里发消息没用。</>
          ) : phase === "queued" ? (
            <>还没开始跑 —— 正在拆解或在等前面的步骤做完。等它动起来就能插话了。</>
          ) : pendingSaid !== null ? (
            <>
              已经收到 —— 它会<b>带着已经做出来的东西</b>重新跑。这会儿任务状态
              还没翻过来（要等它重新起跑，可能几秒），不用再发一遍。
            </>
          ) : (
            <>
              这一轮做完了。还有要改的就直接说 —— 它会<b>带着已经做出来的东西</b>
              接着干（不是从头再来），所以会重新花时间和 token。
              不想再动它就别发消息。
            </>
          )}
        </div>
      </div>
    </main>
  );
}

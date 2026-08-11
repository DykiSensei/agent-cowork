import { useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import type { ActionResult } from "../../api";
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

export default function LiteStream({
  taskId,
  detail,
  onIntervene,
  onCancel,
  onRuling,
}: {
  taskId: string;
  detail: TaskDetail;
  onIntervene: (taskId: string, text: string) => Promise<ActionResult>;
  onCancel: (taskId: string, reason: string) => Promise<ActionResult>;
  onRuling: (
    taskId: string,
    action: string,
    rationale: string,
    specChanges?: Record<string, unknown>,
  ) => Promise<ActionResult>;
}) {
  const [bar, setBar] = useState(false);
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

  const submitIntervene = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const input = e.currentTarget.querySelector("input")!;
    const text = input.value.trim();
    if (!text || !target) return;
    setFailed(null);
    void onIntervene(target.id, text).then((r) => {
      if (!r.ok) {
        // **不清空输入框**：那句话还没送到，人可能想改改再发一次
        setFailed(r.error ?? "没能送出去");
        return;
      }
      input.value = "";
      setBar(true);
      setTimeout(() => setBar(false), 4000);
    });
  };

  return (
    <main className="l-stream">
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
        </div>
      </div>

      <div className="l-composer">
        <div className={`l-sysbar${bar ? " show" : ""}`}>
          已告诉它，等它做完手头这一小步就照你说的办。
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
          <form className="row" style={{ display: "contents" }} onSubmit={submitIntervene}>
            <input
              placeholder={
                running
                  ? "插一句话，改变它接下来的做法…"
                  : phase === "queued"
                    ? "还没开始跑，等它动起来"
                    : phase === "waiting"
                      ? "在上面那张卡片里答复"
                      : "这条任务已经结束了"
              }
              autoComplete="off"
              disabled={!running}
            />
            <button type="submit" disabled={!running}>
              发送
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
          ) : (
            <>这条任务已经结束了，发消息不会有人收。想接着做请发布一个新任务。</>
          )}
        </div>
      </div>
    </main>
  );
}

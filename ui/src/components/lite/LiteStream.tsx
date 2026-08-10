import { useMemo, useState } from "react";
import type { FormEvent, ReactNode } from "react";
import {
  LITE_ACTION,
  LITE_ACTION_SUGGEST,
  liteRationale,
  liteSignalBody,
  liteSignalTitle,
  liteWaitText,
} from "../../copy";
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

function WaitCard({
  pending,
  onSubmit,
}: {
  pending: PendingRuling;
  onSubmit: (action: string) => Promise<boolean>;
}) {
  const [done, setDone] = useState<string | null>(null);
  const suggestion = pending.suggestion;

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
      <div className="l-wait-t">这件事需要你定一下</div>
      <p>{liteWaitText(pending.reason)}</p>
      {suggestion && (
        <div className="l-suggest">
          系统的建议：<b>{LITE_ACTION_SUGGEST[suggestion.action]}</b>
          {suggestion.rationale ? `，${suggestion.rationale}` : ""}
        </div>
      )}
      <div className="l-choices">
        {actions.map((a) => (
          <button
            key={a}
            className={suggestion?.action === a ? "primary" : undefined}
            onClick={() => void onSubmit(a).then(() => setDone(LITE_ACTION[a]))}
          >
            {LITE_ACTION[a]}
          </button>
        ))}
      </div>
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
  onRuling,
}: {
  taskId: string;
  detail: TaskDetail;
  onIntervene: (taskId: string, text: string) => Promise<boolean>;
  onRuling: (taskId: string, action: string, rationale: string) => Promise<boolean>;
}) {
  const [bar, setBar] = useState(false);
  const events = useMemo(() => translate(detail), [detail]);

  const submitIntervene = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const input = e.currentTarget.querySelector("input")!;
    const text = input.value.trim();
    if (!text) return;
    void onIntervene(taskId, text).then(() => {
      input.value = "";
      setBar(true);
      setTimeout(() => setBar(false), 4000);
    });
  };

  return (
    <main className="l-stream">
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
                    onSubmit={(action) => onRuling(taskId, action, "")}
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
        <div className="row">
          <form className="row" style={{ display: "contents" }} onSubmit={submitIntervene}>
            <input placeholder="插一句话，改变它接下来的做法…" autoComplete="off" />
            <button type="submit">发送</button>
          </form>
        </div>
        <div className="l-hint">你的话会直接变成它的新指令，不是闲聊。</div>
      </div>
    </main>
  );
}

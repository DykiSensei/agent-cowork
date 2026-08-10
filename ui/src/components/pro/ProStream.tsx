import { useMemo, useState } from "react";
import type { FormEvent } from "react";
import { fmtTime, proSignalBody } from "../../copy";
import { translate } from "../../translate";
import type {
  DecisionRecord,
  PendingRuling,
  PlanData,
  Signal,
  StreamEvent,
  TaskDetail,
  TaskState,
  TaskStatus,
} from "../../types";

/** 刻度级日志行的时间线节点样式，按日志前缀归类。 */
function tickNode(text: string): string {
  const t = text.trimStart();
  if (t.includes("[STOP")) return "node stop";
  if (t.includes("[DONE")) return "node done";
  if (t.includes("[RUN") || t.includes("[LAYER")) return "node run";
  return "node";
}

const ACTION_BADGE: Record<DecisionRecord["action"], string> = {
  CONTINUE: "badge b-continue",
  MODIFY_TASK: "badge b-modify",
  ABANDON: "badge b-abandon",
  REASSIGN: "badge b-reassign",
};

function HumanItem({ ev }: { ev: Extract<StreamEvent, { kind: "human" }> }) {
  return (
    <div className="item">
      <span className="node hum">你</span>
      <div className="human-card">
        <div className="who">
          <b>你</b>
          <span className="t">
            {ev.ts ? `${fmtTime(ev.ts)} · ` : ""}发布任务
          </span>
        </div>
        <div className="what">{ev.text}</div>
      </div>
    </div>
  );
}

function LogTick({ ev }: { ev: Extract<StreamEvent, { kind: "log" }> }) {
  return (
    <div className="item">
      <span className={tickNode(ev.text)} />
      <div className="tick">
        <span className={ev.soft ? "soft" : undefined}>{ev.text}</span>
        {ev.ts != null && <span className="t">{fmtTime(ev.ts)}</span>}
      </div>
    </div>
  );
}

function SoftSignalTick({ sig }: { sig: Signal }) {
  return (
    <div className="item">
      <span className="node" />
      <div className="tick">
        <span className="soft">
          Subagent 上报软信号 <span className="mono">{sig.type}</span>：「
          {String(sig.payload?.detail ?? "")}」 · L1 · 入队分诊，不抢占
        </span>
        <span className="t">{fmtTime(sig.created_at)}</span>
      </div>
    </div>
  );
}

function SignalCard({ sig }: { sig: Signal }) {
  return (
    <div className="item">
      <span className="node sig" />
      <div className="card sig-card">
        <div className="card-hd">
          <span className="badge b-l0">L0</span>
          <span className="badge b-sig">{sig.type}</span>
          <span className="t">
            source {sig.source} · {sig.id}
          </span>
          <span className="t">{fmtTime(sig.created_at)}</span>
        </div>
        <div className="card-bd">
          <div className="why">{proSignalBody(sig)}</div>
          {sig.raw_evidence && (
            <details className="ev">
              <summary>raw_evidence（第三方文本，纯文本渲染）</summary>
              <pre className="evidence">{sig.raw_evidence}</pre>
            </details>
          )}
        </div>
        <div className="card-ft">
          <span>disposition: {sig.disposition}</span>
          {sig.type === "TEST_FAILED" && (
            <span>
              payload.criterion_id: {String(sig.payload?.criterion_id ?? "")}
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

/** spec diff：直接读 spec_changes（已生效的改动），不再比对两版 spec。 */
function SpecDiff({ d }: { d: DecisionRecord }) {
  const sc = d.spec_changes ?? {};
  const added = sc.added_criteria ?? [];
  if (!sc.goal && added.length === 0) return null;
  return (
    <div className="diff">
      {d.new_spec && (
        <div className="row rev">
          spec rev {d.new_spec.revision - 1} → {d.new_spec.revision}
        </div>
      )}
      {sc.goal ? (
        <div className="row add">~ goal：{sc.goal}</div>
      ) : (
        <div className="row same">　goal：不变</div>
      )}
      {added.map((c) => (
        <div className="row add" key={c.id}>
          + acceptance.{c.id}：{c.description}
        </div>
      ))}
    </div>
  );
}

function DecisionCard({ d }: { d: DecisionRecord }) {
  return (
    <div className="item">
      <span className="node dec" />
      <div className="card">
        {d.escalation_reason && (
          <div className="esc">
            <span className="flag">⚑</span>
            <span>
              {d.decider === "HUMAN" ? "曾升级给人：" : "升级给人："}
              {d.escalation_reason}
            </span>
          </div>
        )}
        <div className="card-hd">
          {d.decider === "LLM" ? (
            <span className="badge b-llm">架构师 · LLM</span>
          ) : (
            <span className="badge b-human">人</span>
          )}
          <span className={ACTION_BADGE[d.action]}>{d.action}</span>
          {d.resume_mode && <span className="badge b-mode">{d.resume_mode}</span>}
          <span className="t">{d.id}</span>
          <span className="t">{fmtTime(d.created_at)}</span>
        </div>
        <div className="card-bd">
          {d.rationale}
          {d.action === "MODIFY_TASK" && <SpecDiff d={d} />}
        </div>
        <div className="card-ft">
          {d.complexity_score != null && (
            <span>complexity {d.complexity_score.toFixed(2)}</span>
          )}
          {d.trigger.length > 0 && <span>trigger: {d.trigger.join(", ")}</span>}
          {d.suggestion && d.decider === "HUMAN" && (
            <span title={d.suggestion.rationale}>
              LLM 建议：{d.suggestion.action}（已被人裁决覆盖/采纳）
            </span>
          )}
        </div>
      </div>
    </div>
  );
}

function AwaitCard({
  pending,
  ts,
  onSubmit,
}: {
  pending: PendingRuling;
  ts: number | null;
  onSubmit: (action: string, rationale: string) => Promise<boolean>;
}) {
  const sg = pending.suggestion;
  const [action, setAction] = useState<DecisionRecord["action"]>(
    sg?.action ?? "CONTINUE",
  );
  const [rationale, setRationale] = useState("");
  const [done, setDone] = useState<string | null>(null);

  if (done) {
    return (
      <div className="item">
        <span className="node wait" />
        <div className="await-card">
          <div className="await-hd">⚑ AWAITING_HUMAN · 裁决已提交</div>
          <div className="card-bd" style={{ padding: 12 }}>
            已记录你的裁决：
            <span className={ACTION_BADGE[done as DecisionRecord["action"]]}>{done}</span>{" "}
            · 将写入 DecisionRecord（decider=HUMAN）。
            <br />
            <span style={{ color: "var(--faint)", fontSize: "11.5px" }}>
              真实链路：服务层拿答复构造 HumanRuling，`Orchestrator.restore()` 从
              checkpoint 重建现场继续跑；spec 的改动仍经 `Architect.apply_human_ruling()`
              落地，不绕过架构师。
            </span>
          </div>
        </div>
      </div>
    );
  }

  return (
    <div className="item">
      <span className="node wait" />
      <div className="await-card">
        <div className="await-hd">
          ⚑ AWAITING_HUMAN · 轮到你了
          {ts != null && <span className="t">{fmtTime(ts)}</span>}
        </div>
        <div className="esc" style={{ borderTop: "none" }}>
          <span className="flag">升级原因</span>
          <span>{pending.reason}</span>
        </div>
        {sg && (
          <div className="suggest">
            <div className="s-hd">
              <span className="badge b-llm">架构师 · LLM 建议</span>
              <span className={ACTION_BADGE[sg.action]}>{sg.action}</span>
              {sg.complexity_score != null && (
                <span className="t">complexity {sg.complexity_score.toFixed(2)}</span>
              )}
            </div>
            <div className="s-bd">
              {sg.rationale}
              {(sg.spec_changes?.added_criteria?.length ?? 0) > 0 && (
                <div className="diff" style={{ marginTop: 8 }}>
                  {sg.spec_changes.added_criteria!.map((c) => (
                    <div className="row add" key={c.id}>
                      + acceptance.{c.id}：{c.description}
                    </div>
                  ))}
                </div>
              )}
            </div>
          </div>
        )}
        <form
          className="ruling-form"
          onSubmit={(e: FormEvent) => {
            e.preventDefault();
            void onSubmit(action, rationale).then(() => setDone(action));
          }}
        >
          <div className="f-label">
            你的裁决（将记录为 DecisionRecord · decider=HUMAN，全群可见）
          </div>
          <div className="radios">
            {(["CONTINUE", "MODIFY_TASK", "ABANDON", "REASSIGN"] as const).map((a) => (
              <label key={a}>
                <input
                  type="radio"
                  name="action"
                  value={a}
                  checked={action === a}
                  onChange={() => setAction(a)}
                />
                {a}
              </label>
            ))}
          </div>
          <textarea
            placeholder="理由（必填）—— 会原样写进 DecisionRecord.rationale"
            value={rationale}
            onChange={(e) => setRationale(e.target.value)}
          />
          <div className="f-actions">
            <button className="btn" type="submit">
              提交裁决
            </button>
            <span className="f-note">选中 MODIFY_TASK 时应展开 spec 编辑区（未实现）</span>
          </div>
        </form>
        <div className="await-ft">
          <span>任务已挂起 · 挂起期间不消耗 token</span>
          {pending.checkpoint_id && (
            <span>答复后从 checkpoint {pending.checkpoint_id} 恢复</span>
          )}
        </div>
      </div>
    </div>
  );
}

function PlanCard({
  plan,
  tasks,
  pendingChildren,
}: {
  plan: PlanData;
  tasks: Record<string, TaskState>;
  pendingChildren: string[];
}) {
  return (
    <div className="item">
      <span className="node done" />
      <div className="card plan-card">
        <div className="card-hd">
          <span className="badge b-plan">PLAN</span>
          <span>分层执行计划</span>
          <span className="t">
            max_parallel {plan.max_parallel} · decomposable{" "}
            {plan.decomposable ? "✓" : "✗"} · issues {plan.issues.length}
          </span>
        </div>
        <div className="card-bd" style={{ paddingTop: 6, paddingBottom: 6 }}>
          {plan.layers.map((layer, li) => (
            <div className="layer" key={li}>
              <span className="l-no">
                L{li + 1} · 并行 {layer.length}
              </span>
              <div className="l-tasks">
                {layer.map((tid) => {
                  const t = tasks[tid];
                  const waiting = pendingChildren.includes(tid);
                  return (
                    <div className="ltask" key={tid}>
                      <span
                        className={`dot ${waiting ? "AWAITING_HUMAN" : (t?.status ?? "PENDING")}`}
                      />
                      <span className="mono">{tid}</span>
                      {waiting && <span className="t-tag hot">等你处理</span>}
                      <span className="goal">{t?.spec.goal}</span>
                      {t?.spec.scope.length ? (
                        <span className="prod">→ {t.spec.scope.join(", ")}</span>
                      ) : null}
                    </div>
                  );
                })}
              </div>
            </div>
          ))}
        </div>
        <div className="card-ft">
          <span>子任务各有一个 thread（跳转未实现）</span>
        </div>
      </div>
    </div>
  );
}

const TERM_COLOR: Partial<Record<TaskStatus, string>> = {
  COMPLETED: "var(--green)",
  FAILED: "var(--red)",
  ABANDONED: "var(--rose)",
};

function TermCard({ ev }: { ev: Extract<StreamEvent, { kind: "terminal" }> }) {
  const color = TERM_COLOR[ev.status] ?? "var(--gray)";
  const mark =
    ev.status === "COMPLETED" ? "✓" : ev.status === "FAILED" ? "✗" : "■";
  return (
    <div className="item">
      <span className={`node ${ev.status === "COMPLETED" ? "done" : "stop"}`} />
      <div className="card term-card" style={{ borderLeftColor: color }}>
        <div className="card-bd">
          <div className="big" style={{ color }}>
            {mark} {ev.title ?? ev.status}
          </div>
          <div className="chips" style={{ marginTop: 8 }}>
            {ev.chips.map((c) => (
              <span className="chip" key={c}>
                {c}
              </span>
            ))}
          </div>
        </div>
      </div>
    </div>
  );
}

export default function ProStream({
  taskId,
  detail,
  onIntervene,
  onCancel,
  onRuling,
}: {
  taskId: string;
  detail: TaskDetail;
  onIntervene: (taskId: string, text: string) => Promise<boolean>;
  onCancel: (taskId: string, reason: string) => Promise<boolean>;
  onRuling: (taskId: string, action: string, rationale: string) => Promise<boolean>;
}) {
  const [bar, setBar] = useState(false);
  const [stopping, setStopping] = useState(false);
  const events = useMemo(() => translate(detail), [detail]);
  const isAwaiting =
    detail.kind === "single" && detail.state?.status === "AWAITING_HUMAN";
  // 取消只对运行中的任务成立：AWAITING_HUMAN 走 ruling(ABANDON)，终局无可取消
  const isRunning =
    detail.kind === "single" &&
    (detail.state?.status === "RUNNING" || detail.state?.status === "INTERRUPTED");

  const submitCancel = () => {
    if (stopping) return;
    setStopping(true);
    void onCancel(taskId, "在界面上取消").finally(() => setStopping(false));
  };

  const submitIntervene = (e: FormEvent<HTMLFormElement>) => {
    e.preventDefault();
    const input = e.currentTarget.querySelector("input")!;
    const text = input.value.trim();
    if (!text) return;
    void onIntervene(taskId, text).then(() => {
      input.value = "";
      setBar(true);
      setTimeout(() => setBar(false), 5000);
    });
  };

  return (
    <main className="stream">
      <div className="msgs">
        {events.map((ev, i) => {
          switch (ev.kind) {
            case "human":
              return <HumanItem key={i} ev={ev} />;
            case "log":
              return <LogTick key={i} ev={ev} />;
            case "signal":
              return ev.signal.level === "L1" ? (
                <SoftSignalTick key={i} sig={ev.signal} />
              ) : (
                <SignalCard key={i} sig={ev.signal} />
              );
            case "decision":
              return <DecisionCard key={i} d={ev.decision} />;
            case "awaiting":
              return (
                <AwaitCard
                  key={i}
                  pending={ev.pending}
                  ts={ev.ts}
                  onSubmit={(action, rationale) => onRuling(taskId, action, rationale)}
                />
              );
            case "plan":
              return (
                <PlanCard
                  key={i}
                  plan={ev.plan}
                  tasks={ev.tasks}
                  pendingChildren={ev.pendingChildren}
                />
              );
            case "terminal":
              return <TermCard key={i} ev={ev} />;
          }
        })}
      </div>

      <div className={`sysbar${bar ? " show" : ""}`}>
        <span className="dot RUNNING" />
        已提交，等待当前 step 结束（实测 step 中位 1.65s · p99 5.9s）。中断后抢占队列清空，重复提交只有一条生效。
      </div>
      <div className="composer">
        <form className="row" onSubmit={submitIntervene}>
          <input
            placeholder="介入指令 —— 会成为新的 goal，是命令不是聊天（HUMAN_INTERVENTION 硬信号）"
            autoComplete="off"
          />
          <button className="btn" type="submit">
            介入
          </button>
          {isRunning && (
            <button
              className="btn danger"
              type="button"
              onClick={submitCancel}
              disabled={stopping}
              title="ABANDON，不经架构师裁决"
            >
              {stopping ? "取消中…" : "取消任务"}
            </button>
          )}
        </form>
        <div className="hint">
          {detail.kind === "composite" ? (
            <span>
              <b>复合任务的介入</b>应路由到对应子任务的 Orchestrator（路由选择未实现）。
            </span>
          ) : isAwaiting ? (
            <span>
              <b>下一个 step 边界生效</b>，不是立即。任务挂起（AWAITING_HUMAN）时请改用上方裁决表单
              —— 介入信号只在运行中的循环里被消费。
            </span>
          ) : (
            <span>
              <b>下一个 step 边界生效</b>，不是立即。中断后抢占队列清空，连点只有一条生效。
              {isRunning && (
                <>
                  {" "}
                  <b>取消任务</b>同样停在 step 边界，但<b>不问架构师</b> —— 直接
                  ABANDONED，已落盘的产出保留。
                </>
              )}
            </span>
          )}
        </div>
      </div>
    </main>
  );
}

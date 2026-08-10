import type {
  CompositeDetail,
  SingleDetail,
  TaskDetail,
  TaskState,
  TaskStatus,
} from "../../types";

// 与 policy.py 的默认值对齐。真实服务层应通过端点下发，而不是前端各抄一份。
const MAX_INTERRUPTS = 3;
const MAX_REBASE = 2;

function SingleMeta({ detail }: { detail: SingleDetail }) {
  const state = detail.state;
  const spec = state.spec;
  // rebase 次数从裁决历史重算（和将来 restore 路径的算法一致）
  const rebaseCount = Object.values(detail.decisions).filter(
    (d) => d.resume_mode === "REBASE",
  ).length;
  const stepPct = spec.max_steps
    ? Math.min(100, Math.round((state.current_step / spec.max_steps) * 100))
    : 0;
  const tokPct = spec.token_budget
    ? Math.min(100, Math.round((state.tokens_used / spec.token_budget) * 100))
    : 0;

  return (
    <>
      <h3>任务状态</h3>
      <span className={`status-chip ${state.status}`}>
        <span className={`dot ${state.status}`} />
        {state.status}
      </span>
      {state.status === "AWAITING_HUMAN" && (
        <div className="note">等待你的答复 · 挂起期间不消耗 token</div>
      )}

      <h3>进度与成本</h3>
      <div className="kv-row">
        <span className="k">step</span>
        <span className="v">
          {state.current_step} / {spec.max_steps}
        </span>
      </div>
      <div className={`bar${stepPct > 80 ? " warn" : ""}`}>
        <i style={{ width: `${stepPct}%` }} />
      </div>
      <div className="kv-row">
        <span className="k">tokens</span>
        <span className="v">
          {state.tokens_used.toLocaleString()} / {(spec.token_budget ?? 0).toLocaleString()}
        </span>
      </div>
      <div className={`bar${tokPct > 60 ? " warn" : ""}`}>
        <i style={{ width: `${tokPct}%` }} />
      </div>
      <div className="kv-row">
        <span className="k">interrupt_count</span>
        <span
          className="v"
          style={
            state.interrupt_count >= MAX_INTERRUPTS
              ? { color: "var(--amber-hi)" }
              : undefined
          }
        >
          {state.interrupt_count}（升级阈值 {MAX_INTERRUPTS}）
        </span>
      </div>
      <div className="kv-row">
        <span className="k">rebase</span>
        <span className="v">
          {rebaseCount} / 上限 {MAX_REBASE}
        </span>
      </div>

      <h3>标识</h3>
      <div className="kv-row">
        <span className="k">task_id</span>
        <span className="v">{state.task_id}</span>
      </div>
      <div className="kv-row">
        <span className="k">revision</span>
        <span className="v">{state.revision}</span>
      </div>
      <div className="kv-row">
        <span className="k">agent_id</span>
        <span className="v">{state.agent_id ?? "-"}</span>
      </div>
      <div className="kv-row">
        <span className="k">checkpoint</span>
        <span className="v">{state.checkpoint_id ?? "-"}</span>
      </div>
      <div className="kv-row">
        <span className="k">model</span>
        <span className="v">{spec.model}</span>
      </div>

      <h3>Spec（rev {state.revision}）</h3>
      {spec.acceptance.map((c) => (
        <div className="crit" key={c.id}>
          <span className="cid">{c.id}</span>
          <div>
            <div className="cdesc">{c.description}</div>
            <div className="ccmd">
              {c.command ? c.command.join(" ") : "（非机器可检 · 架构师验收）"}
            </div>
          </div>
        </div>
      ))}
      <div className="kv-row" style={{ marginTop: 6 }}>
        <span className="k">sandbox</span>
        <span className="v">
          {spec.sandbox
            ? `${spec.sandbox.use_docker ? "Docker" : "本地白名单"} · network ${spec.sandbox.network}`
            : "-"}
        </span>
      </div>
      <div className="kv-row">
        <span className="k">tools</span>
        <span className="v">{spec.tools.join(" · ")}</span>
      </div>

      <h3>硬信号覆盖面（{spec.hard_signals.length}）</h3>
      <div className="chips">
        {spec.hard_signals.map((s) => (
          <span className="chip" key={s}>
            {s}
          </span>
        ))}
      </div>

      {state.artifacts.length > 0 && (
        <>
          <h3>Artifacts</h3>
          {state.artifacts.map((a) => (
            <div className="kv-row" key={a}>
              <span className="k">{a.slice(0, 12)}</span>
              <span className="v">{a}</span>
            </div>
          ))}
        </>
      )}
    </>
  );
}

/** 合成父行的状态规则（与 views._synthetic_parent 同一条：按最坏情况取）。 */
function worstStatus(tasks: TaskState[]): TaskStatus {
  const ss = new Set(tasks.map((t) => t.status));
  if (ss.has("AWAITING_HUMAN")) return "AWAITING_HUMAN";
  if (ss.has("FAILED") || ss.has("ABANDONED")) return "FAILED";
  if ([...ss].every((s) => s === "COMPLETED")) return "COMPLETED";
  if (ss.has("RUNNING") || ss.has("INTERRUPTED")) return "RUNNING";
  return "PENDING";
}

function CompositeMeta({ detail }: { detail: CompositeDetail }) {
  const plan = detail.plan;
  const review = detail.review;
  const tasks = Object.values(detail.tasks);
  const doneCount = tasks.filter((t) => t.status === "COMPLETED").length;
  const status = worstStatus(tasks);
  const conflicts = Object.values(detail.signals).filter(
    (s) => s.type === "CONFLICT_DETECTED",
  );
  const conflictIds = new Set(conflicts.map((s) => s.id));
  const arbitrations = Object.values(detail.decisions).filter((d) =>
    d.trigger.some((t) => conflictIds.has(t)),
  ).length;

  return (
    <>
      <h3>复合任务状态</h3>
      <span className={`status-chip ${status}`}>
        <span className={`dot ${status}`} />
        {status}
      </span>
      <div className="note">
        {doneCount}/{tasks.length} 子任务完成
      </div>
      {detail.pending_children.length > 0 && (
        <div className="note amber">
          在等你处理：{detail.pending_children.join("、")}
        </div>
      )}

      {plan && (
        <>
          <h3>计划</h3>
          <div className="kv-row">
            <span className="k">layers</span>
            <span className="v">{plan.layers.length}</span>
          </div>
          <div className="kv-row">
            <span className="k">max_parallel</span>
            <span className="v">{plan.max_parallel}</span>
          </div>
          <div className="kv-row">
            <span className="k">decomposable</span>
            <span className="v">{plan.decomposable ? "✓" : "✗"}</span>
          </div>
          <div className="kv-row">
            <span className="k">issues</span>
            <span className="v">
              {plan.issues.length
                ? plan.issues.map((i) => i.detail).join("；")
                : "无"}
            </span>
          </div>
          <div className="kv-row">
            <span className="k">conflicts</span>
            <span className="v">
              {conflicts.length} · arbitrations {arbitrations}
            </span>
          </div>
        </>
      )}

      {review && (
        <>
          <h3>复核</h3>
          <div className="kv-row">
            <span className="k">sufficient</span>
            <span className="v">{review.sufficient ? "✓ 覆盖完整" : "✗ 有缺口"}</span>
          </div>
          <div className="kv-row">
            <span className="k">missing</span>
            <span className="v">{review.missing.length ? review.missing.join("；") : "无"}</span>
          </div>
          <div className="kv-row">
            <span className="k">reviewer</span>
            <span className="v">{review.reviewer}</span>
          </div>
          {!review.independent && (
            <div className="note amber">
              independent: false —— 复核者与拆解者是同一个后端（真实配置下应换一家，§11.11）
            </div>
          )}
        </>
      )}

      <h3>子任务</h3>
      {Object.entries(detail.tasks).map(([tid, t]) => (
        <div className="kv-row" key={tid}>
          <span className="k" style={{ width: 90 }}>
            {tid}
          </span>
          <span className="v">
            <span
              className={`dot ${detail.pending_children.includes(tid) ? "AWAITING_HUMAN" : t.status}`}
            />{" "}
            {t.tokens_used} tok
          </span>
        </div>
      ))}
    </>
  );
}

export default function ProMetaPanel({ detail }: { detail: TaskDetail }) {
  return (
    <aside className="meta">
      {detail.kind === "composite" ? (
        <CompositeMeta detail={detail} />
      ) : (
        <SingleMeta detail={detail} />
      )}
    </aside>
  );
}

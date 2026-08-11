import { useCallback, useEffect, useRef, useState } from "react";
import type { FormEvent } from "react";
import { createTask, dispatchPlan, fetchPlan, rulePlan } from "../api";
import type { PlanView } from "../types";

/**
 * 发布任务：目标 → 拆解 → 人裁决 → 派发（M6 §6 的 `POST /tasks` + `/plans/*`）。
 *
 * 这条路服务端一直是齐的，界面这边却一个调用都没有 —— 于是 `cowork serve` 起来的
 * 界面只能**看**和**答**，任务必须从 CLI 发。第一屏还写着「填个 key 就能开始」。
 *
 * 两条边界照搬后端的分工，不在这里另做判断：
 *
 * - **拆解的三种终局不是异常**（ACCEPTED / AWAITING_HUMAN / REJECTED），
 *   所以 AWAITING_HUMAN 不是错误提示，是一张要人拍板的卡片。
 * - **写权不在这里**。界面只发 accept / reject，重拆永远由架构师做（§2.3）。
 *   所以没有「编辑这份拆解」的入口 —— 那需要人自己交一份完整的 specs，
 *   属于 `PlanRuling.specs`，不是随手改两个字的事。
 *
 * 模型分配（`dispatch` 的 assignments）暂不在界面上做：只有一家可用时它没有意义，
 * 多家时需要先把 profiles 摆出来给人选，那是独立的一屏。不传 = 全用默认那家，
 * 与 `AutoApproveGate` 的处置一致。
 */

const POLL_MS = 1500;

function StatusLine({ plan }: { plan: PlanView }) {
  if (plan.status === "RUNNING") {
    return <div className="nt-line">正在拆解这个目标…（一次真实调用，约 10–35k token）</div>;
  }
  if (plan.status === "ERROR") {
    return <div className="nt-line bad">拆解失败：{plan.error}</div>;
  }
  if (plan.status === "REJECTED") {
    return <div className="nt-line">你否决了这份拆解。{plan.rationale}</div>;
  }
  if (plan.status === "AWAITING_HUMAN") {
    return (
      <div className="nt-line warn">
        拆解没能自己收敛，要你拍板：{plan.escalation_reason || plan.rationale}
      </div>
    );
  }
  return (
    <div className="nt-line good">
      拆解通过复核（第 {plan.attempts ?? 1} 轮，{(plan.tokens ?? 0).toLocaleString()} token）
    </div>
  );
}

function SpecList({ plan }: { plan: PlanView }) {
  if (!plan.specs?.length) return null;
  return (
    <div className="nt-specs">
      {plan.specs.map((s) => (
        <div className="nt-spec" key={s.id}>
          <div className="nt-spec-hd">
            <span className="mono">{s.id}</span>
            {s.depends_on.length > 0 && (
              <span className="nt-dep">← {s.depends_on.join(", ")}</span>
            )}
            {s.scope.length > 0 && <span className="nt-scope">{s.scope.join(", ")}</span>}
          </div>
          <div className="nt-spec-goal">{s.goal}</div>
          <ul className="nt-crit">
            {s.acceptance.map((c) => (
              <li key={c.id}>
                {c.description}
                {c.command ? <span className="nt-cmd"> · 机器可检</span> : null}
              </li>
            ))}
          </ul>
        </div>
      ))}
    </div>
  );
}

function ReviewNotes({ plan }: { plan: PlanView }) {
  const review = plan.review;
  if (!review) return null;
  const items = [
    ...review.structural.map((i) => `${i.kind}：${i.detail}`),
    ...review.missing,
  ];
  if (!items.length) return null;
  return (
    <div className="nt-review">
      <div className="nt-review-hd">
        复核意见（复核者 {review.reviewer}
        {review.independent ? "，独立" : "，与拆解者同一个后端"}）
      </div>
      <ul>
        {items.map((t, i) => (
          <li key={i}>{t}</li>
        ))}
      </ul>
    </div>
  );
}

export default function NewTask({
  onDispatched,
  onClose,
}: {
  onDispatched: (rootId: string) => void;
  onClose: () => void;
}) {
  const [goal, setGoal] = useState("");
  const [planId, setPlanId] = useState<string | null>(null);
  const [plan, setPlan] = useState<PlanView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 拆解在服务端是一条后台线程，没有「拆完了」的推送保证 —— SSE 的 plan 事件到了
  // 会刷新，但轮询是那条保底：拿到终局就停，所以它不会一直转下去。
  useEffect(() => {
    if (!planId) return;
    let alive = true;
    const tick = () => {
      fetchPlan(planId)
        .then((p) => {
          if (!alive) return;
          setPlan(p);
          if (p.status === "RUNNING") {
            timer.current = setTimeout(tick, POLL_MS);
          }
        })
        .catch((e: unknown) => {
          if (alive) setError(String(e));
        });
    };
    tick();
    return () => {
      alive = false;
      if (timer.current) clearTimeout(timer.current);
    };
  }, [planId]);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const text = goal.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    void createTask(text)
      .then((r) => {
        if (!r.ok || !r.plan_id) {
          setError(r.error ?? "服务端没有返回 plan_id");
          return;
        }
        setPlanId(r.plan_id);
      })
      .finally(() => setBusy(false));
  };

  const dispatch = useCallback(() => {
    if (!planId || busy) return;
    setBusy(true);
    setError(null);
    void dispatchPlan(planId)
      .then((r) => {
        if (!r.ok || !r.root_id) {
          setError(r.error ?? "派发没有返回 root_id");
          return;
        }
        onDispatched(r.root_id);
        onClose();
      })
      .finally(() => setBusy(false));
  }, [planId, busy, onDispatched, onClose]);

  const rule = useCallback(
    (accept: boolean) => {
      if (!planId || busy) return;
      setBusy(true);
      setError(null);
      void rulePlan(planId, accept, accept ? "人确认按当前拆解执行" : "人否决了这份拆解")
        .then((r) => {
          if (!r.ok) {
            setError(r.error ?? "裁决没有被接受");
            return;
          }
          return fetchPlan(planId).then(setPlan);
        })
        .finally(() => setBusy(false));
    },
    [planId, busy],
  );

  if (!planId) {
    return (
      <form className="nt" onSubmit={submit}>
        <div className="nt-hd">发布一个任务</div>
        <textarea
          className="nt-input"
          placeholder="一句话说清你要什么。系统会先拆解、复核，拆解通过后才开始执行。"
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          rows={3}
          autoFocus
        />
        {error && <div className="nt-line bad">{error}</div>}
        <div className="nt-actions">
          <button type="button" className="nt-ghost" onClick={onClose}>
            取消
          </button>
          <button type="submit" className="nt-primary" disabled={busy || !goal.trim()}>
            {busy ? "提交中…" : "开始拆解"}
          </button>
        </div>
      </form>
    );
  }

  const canDispatch = Boolean(plan?.dispatchable) && !plan?.dispatched_root;
  const needsRuling = plan?.status === "AWAITING_HUMAN" && !plan?.dispatchable;

  return (
    <div className="nt">
      <div className="nt-hd">发布一个任务</div>
      <div className="nt-goal">{goal}</div>
      {plan ? <StatusLine plan={plan} /> : <div className="nt-line">正在拆解…</div>}
      {plan && <ReviewNotes plan={plan} />}
      {plan && <SpecList plan={plan} />}
      {error && <div className="nt-line bad">{error}</div>}
      <div className="nt-actions">
        <button type="button" className="nt-ghost" onClick={onClose}>
          关闭
        </button>
        {needsRuling && (
          <>
            <button
              type="button"
              className="nt-ghost"
              disabled={busy}
              onClick={() => rule(false)}
            >
              否决这份拆解
            </button>
            <button
              type="button"
              className="nt-primary"
              disabled={busy}
              onClick={() => rule(true)}
            >
              就按这份拆解跑
            </button>
          </>
        )}
        {canDispatch && (
          <button
            type="button"
            className="nt-primary"
            disabled={busy}
            onClick={dispatch}
          >
            {busy ? "派发中…" : "开始执行"}
          </button>
        )}
      </div>
      {canDispatch && (
        <div className="nt-note">
          派发之后就开始真的花钱了。子任务全用默认那家模型 ——
          按任务挑供应商要先看架构师给的任务画像，那是独立的一屏（未实现）。
        </div>
      )}
    </div>
  );
}

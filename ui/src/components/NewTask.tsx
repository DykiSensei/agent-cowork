import { useCallback, useEffect, useRef, useState } from "react";

import type { FormEvent } from "react";
import {
  createTask,
  dispatchPlan,
  fetchPlan,
  fetchSettings,
  fetchSkills,
  rulePlan,
} from "../api";
import type { PlanView, SkillList, TaskSpec } from "../types";
import FolderPicker from "./FolderPicker";

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
 * - **重拆永远由架构师做**（§2.3 唯一写入决策点）。人可以改这份拆解，但那走的是
 *   `PlanRuling.specs` —— 人**交上一份完整的 specs**，而不是让架构师照着改。
 *   界面能改的只有模型有权决定的那四样（目标 / 验收标准 / 可写路径 / 依赖），
 *   sandbox、工具白名单、各类上限原样回传：那是隔离边界，不在这里改。
 *
 * 模型分配（`dispatch` 的 assignments）暂不在界面上做：只有一家可用时它没有意义，
 * 多家时需要先把 profiles 摆出来给人选，那是独立的一屏。不传 = 全用默认那家，
 * 与 `AutoApproveGate` 的处置一致。
 */

const POLL_MS = 1500;

/**
 * 各轮次的耗时基线，出自 `plan_ab.jsonl` 的 37 次真实拆解。
 *
 * **按轮数分开，不能用一个总的中位数**：1 轮中位 110 秒、2 轮 286 秒、3 轮 381 秒
 * —— 混在一起算出来的 243 秒对任何一轮都不准。
 * 样本很小（n=20/10/6），所以措辞是「以往大概」，不是承诺。
 */
const PLAN_BASELINE: Record<number, { median: number; max: number }> = {
  1: { median: 110, max: 349 },
  2: { median: 286, max: 574 },
  3: { median: 381, max: 748 },
};

const PHASE_TEXT: Record<string, string> = {
  generating: "架构师在拆这个目标",
  reviewing: "复核者在挑毛病",
};

/**
 * 拆解的实时进度。
 *
 * 原来这里只有一行「正在拆解这个目标…」，而这个循环实测要 110~381 秒 ——
 * 中间几分钟里「慢」和「卡死」在界面上长得一模一样，人只能猜。
 *
 * 摆四样**真实的量**，一个合成的百分比都没有：跑到第几轮（分母是
 * `max_regenerate`，真的有上限）、这一轮在干嘛、烧了多少 token（在动 = 活着）、
 * 已经多久（对着历史分位，让「久」有刻度）。
 */
export function PlanProgressView({ plan }: { plan: PlanView }) {
  const [now, setNow] = useState(() => Date.now() / 1000);
  useEffect(() => {
    const t = setInterval(() => setNow(Date.now() / 1000), 1000);
    return () => clearInterval(t);
  }, []);

  const p = plan.progress;
  const elapsed = plan.started_at ? Math.max(0, Math.round(now - plan.started_at)) : null;
  const inPhase = p?.phase_started_at
    ? Math.max(0, Math.round(now - p.phase_started_at))
    : null;
  const base = p ? PLAN_BASELINE[Math.min(p.attempt, 3)] : undefined;
  const slow = Boolean(elapsed && base && elapsed > base.max);

  const steps: { key: string; label: string }[] = [
    { key: "generating", label: "生成拆解" },
    { key: "reviewing", label: "复核" },
  ];

  return (
    <div className="nt-prog">
      <div className="nt-prog-hd">
        {p ? PHASE_TEXT[p.phase] ?? "正在拆解" : "正在准备"}
        {p && p.max_attempts > 1 && (
          <span className="nt-prog-round">
            第 {p.attempt} / 最多 {p.max_attempts} 轮
          </span>
        )}
      </div>

      <div className="nt-prog-steps">
        {steps.map((s) => {
          const state = !p
            ? "todo"
            : s.key === p.phase
              ? "now"
              : steps.findIndex((x) => x.key === p.phase) >
                  steps.findIndex((x) => x.key === s.key)
                ? "done"
                : "todo";
          return (
            <span className={`nt-prog-step ${state}`} key={s.key}>
              {s.label}
            </span>
          );
        })}
      </div>

      <div className="nt-prog-meta">
        {p ? `${p.tokens.toLocaleString()} token` : "还没开始调模型"}
        {elapsed !== null && ` · 已用 ${fmtSec(elapsed)}`}
        {/* 这一阶段待了多久。**token 在这段时间里是不动的**（链上没有流式调用，
            它只在一次调用返回后才跳），所以「还活着吗」得靠这个计时器来答。 */}
        {inPhase !== null && ` · 这一步 ${fmtSec(inPhase)}`}
        {base && ` · 这一轮以往大概 ${fmtSec(base.median)}`}
      </div>

      {slow && (
        <div className="nt-prog-slow">
          比以往同轮次的最慢一次（{fmtSec(base!.max)}）还久了。
          多半只是这个目标更难拆，但也可能是模型那边卡住了 ——
          关掉这一屏不影响它继续跑，任务不会因此丢。
        </div>
      )}
    </div>
  );
}

/**
 * 说明书（skill）勾选。
 *
 * **一份都没有时也要出现**，而且要说清目录在哪 —— 「我该把 skill 放哪」
 * 这个问题必须有答案，否则人只会以为这个功能坏了（同工作区那条）。
 *
 * 只勾选、不编辑：增删改在文件系统里做。在界面上做一个文件管理器没有意义，
 * 人已经有一个了。
 */
export function SkillPicker({
  picked,
  onToggle,
  list: given,
}: {
  picked: string[];
  onToggle: (name: string) => void;
  /** 直接给一份清单（不自己拉）。行为检查用的就是这条 —— 静态渲染跑不了 effect。 */
  list?: SkillList;
}) {
  const [fetched, setFetched] = useState<SkillList | null>(null);
  useEffect(() => {
    if (given) return;
    void fetchSkills()
      .then(setFetched)
      .catch(() => {});
  }, [given]);

  const list = given ?? fetched;
  if (!list) return null;
  return (
    <div className="nt-skills">
      <div className="nt-skills-hd">
        说明书
        <span className="nt-skills-note">
          {list.skills.length
            ? "勾上的会加进 Subagent 的提示词 —— 只有勾上的，所以别一次全选"
            : "还没有说明书。在下面这个目录里放 <名字>.md 就会出现在这里"}
        </span>
      </div>
      {list.skills.length > 0 && (
        <div className="nt-skills-list">
          {list.skills.map((s) => (
            <label
              key={s.name}
              className={`nt-skill${picked.includes(s.name) ? " on" : ""}`}
              title={s.path}
            >
              <input
                type="checkbox"
                checked={picked.includes(s.name)}
                onChange={() => onToggle(s.name)}
              />
              <span className="nt-skill-n">{s.name}</span>
              <span className="nt-skill-d">{s.description}</span>
            </label>
          ))}
        </div>
      )}
      <div className="nt-skills-root">
        放在 <code>{list.root}</code>
        {!list.exists && "（还不存在，建一个就行）"}
      </div>
    </div>
  );
}

function fmtSec(s: number): string {
  if (s < 60) return `${s} 秒`;
  const m = Math.floor(s / 60);
  return `${m} 分 ${String(s % 60).padStart(2, "0")} 秒`;
}

function StatusLine({ plan }: { plan: PlanView }) {
  if (plan.status === "RUNNING") {
    return <PlanProgressView plan={plan} />;
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

/**
 * 拆解清单。**可以改** —— 人是仲裁者，不该只有「同意」和「否决」两个按钮。
 *
 * 能改的只有模型有权决定的那四样：目标 / 验收标准 / 可写路径 / 依赖。
 * sandbox、工具白名单、各类上限**原样带回去**（整份 spec 回传，只替换这四个
 * 字段）—— 那是隔离边界，归模板和人在设置页管，不该在这里被顺手改掉，
 * 更不能因为界面没带这些字段而丢失。
 */
export function SpecList({
  plan,
  edited,
  onEdit,
}: {
  plan: PlanView;
  edited: TaskSpec[] | null;
  onEdit: ((specs: TaskSpec[] | null) => void) | null;
}) {
  const specs = edited ?? plan.specs;
  if (!specs?.length) return null;
  const editable = onEdit !== null;

  const patch = (i: number, next: Partial<TaskSpec>) => {
    if (!onEdit) return;
    onEdit(specs.map((s, k) => (k === i ? { ...s, ...next } : s)));
  };
  const csv = (v: string) =>
    v.split(/[,，]/).map((x) => x.trim()).filter(Boolean);

  return (
    <div className="nt-specs">
      {specs.map((s, i) => (
        <div className="nt-spec" key={s.id}>
          <div className="nt-spec-hd">
            <span className="mono">{s.id}</span>
            {s.depends_on.length > 0 && (
              <span className="nt-dep">← {s.depends_on.join(", ")}</span>
            )}
            {editable && specs.length > 1 && (
              <button
                className="nt-del"
                title="删掉这个子任务"
                onClick={() => onEdit!(specs.filter((_, k) => k !== i))}
              >
                删掉
              </button>
            )}
          </div>

          {editable ? (
            <textarea
              className="nt-edit nt-edit-goal"
              value={s.goal}
              rows={2}
              onChange={(e) => patch(i, { goal: e.target.value })}
            />
          ) : (
            <div className="nt-spec-goal">{s.goal}</div>
          )}

          <ul className="nt-crit">
            {s.acceptance.map((c, ci) => (
              <li key={c.id}>
                {editable ? (
                  <div className="nt-crit-row">
                    <input
                      className="nt-edit"
                      value={c.description}
                      onChange={(e) =>
                        patch(i, {
                          acceptance: s.acceptance.map((x, k) =>
                            k === ci ? { ...x, description: e.target.value } : x,
                          ),
                        })
                      }
                    />
                    {s.acceptance.length > 1 && (
                      <button
                        className="nt-del"
                        title="删掉这条验收标准"
                        onClick={() =>
                          patch(i, {
                            acceptance: s.acceptance.filter((_, k) => k !== ci),
                          })
                        }
                      >
                        ×
                      </button>
                    )}
                  </div>
                ) : (
                  <>
                    {c.description}
                    {c.command ? <span className="nt-cmd"> · 机器可检</span> : null}
                  </>
                )}
              </li>
            ))}
          </ul>

          {editable && (
            <div className="nt-spec-more">
              <button
                className="nt-add"
                onClick={() =>
                  patch(i, {
                    acceptance: [
                      ...s.acceptance,
                      { id: `h${s.acceptance.length + 1}`, description: "", command: null },
                    ],
                  })
                }
              >
                + 加一条验收标准
              </button>
              <label className="nt-field">
                <span>可写路径</span>
                <input
                  className="nt-edit mono"
                  value={s.scope.join(", ")}
                  onChange={(e) => patch(i, { scope: csv(e.target.value) })}
                  placeholder="逗号分隔"
                />
              </label>
              <label className="nt-field">
                <span>依赖</span>
                <input
                  className="nt-edit mono"
                  value={s.depends_on.join(", ")}
                  onChange={(e) => patch(i, { depends_on: csv(e.target.value) })}
                  placeholder="其它子任务的 id，逗号分隔"
                />
              </label>
            </div>
          )}

          {!editable && s.scope.length > 0 && (
            <div className="nt-scope">{s.scope.join(", ")}</div>
          )}
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
  initialPlanId = null,
  onDispatched,
  onClose,
}: {
  /** 从一个没派发的 plan 恢复（详情页「去裁决 / 去派发」跳转过来）。 */
  initialPlanId?: string | null;
  onDispatched: (rootId: string) => void;
  onClose: () => void;
}) {
  const [goal, setGoal] = useState("");
  const [mode, setMode] = useState<"new" | "takeover">("new");
  const [ws, setWs] = useState("");
  const [wsDefault, setWsDefault] = useState("");
  const [picking, setPicking] = useState(false);
  const [planId, setPlanId] = useState<string | null>(null);
  // 派发成功了。**不自动关框**：关掉会让人以为「只能发一个」，而服务端本来
  // 就并行 —— 发完一个接着发下一个是最自然的事（M12 待办 #2）。
  const [dispatched, setDispatched] = useState(false);
  const [plan, setPlan] = useState<PlanView | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  // 人改过的拆解。null = 没动过，显示架构师那份。
  const [edited, setEdited] = useState<TaskSpec[] | null>(null);
  // 否决时写给架构师的意见 —— 带意见重新拆，不带就是纯否决（M12 之后）。
  const [instruction, setInstruction] = useState("");
  // 轮询重启的扳机：带意见重新拆会让 plan 回到 RUNNING，而轮询只在
  // status===RUNNING 时自续 —— 从「等拍板」翻回「拆解中」时它已经停了，
  // 得靠这个 nonce 把它重新拉起来（planId 没变，useEffect 不会自己重跑）。
  const [pollNonce, setPollNonce] = useState(0);
  // 这次带哪几份说明书。**只有勾了的会进提示词**：代价是前缀缓存按组合分叉，
  // 换来的是 skill 多起来之后不必每个任务都驮着全部正文（M12）。
  const [picked, setPicked] = useState<string[]>([]);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 默认工作区从设置页来 —— 「我的产物会落在哪」要在**发布之前**就看得见，
  // 而不是跑完了再去找
  useEffect(() => {
    void fetchSettings()
      .then((s) => {
        setWsDefault(s.workspace || s.workspace_default);
        if (!s.workspace) return;
        setWs(s.workspace);
      })
      .catch(() => {});
  }, []);

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
  }, [planId, pollNonce]);

  // 从详情页「去裁决 / 去派发」跳转过来时，直接进入那条 plan，不再问一次目标。
  useEffect(() => {
    if (initialPlanId) {
      setPlanId(initialPlanId);
      setDispatched(false);
    }
  }, [initialPlanId]);

  const toggleSkill = useCallback((name: string) => {
    setPicked((prev) =>
      prev.includes(name) ? prev.filter((n) => n !== name) : [...prev, name],
    );
  }, []);

  const submit = (e: FormEvent) => {
    e.preventDefault();
    const text = goal.trim();
    if (!text || busy) return;
    setBusy(true);
    setError(null);
    void createTask(text, {
      workspace: ws.trim() || undefined,
      mode,
      skills: picked,
    })
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
        setDispatched(true);
        onDispatched(r.root_id);
      })
      .finally(() => setBusy(false));
  }, [planId, busy, onDispatched]);

  const rule = useCallback(
    (accept: boolean, specs?: TaskSpec[], instruction?: string) => {
      if (!planId || busy) return;
      setBusy(true);
      setError(null);
      const note = specs
        ? "人改过这份拆解"
        : accept
          ? "人确认按当前拆解执行"
          : instruction
            ? "人否决并提了意见，重新拆"
            : "人否决了这份拆解";
      void rulePlan(planId, accept, note, specs, instruction)
        .then((r) => {
          if (!r.ok) {
            setError(r.error ?? "裁决没有被接受");
            return;
          }
          setEdited(null); // 服务端已经收下，之后以它返回的为准
          if (instruction) {
            // 带意见重拆：plan 回到 RUNNING，重启轮询盯着它拆完。
            // 直接 fetchPlan 一次不够 —— 轮询已经停在「等拍板」那一步了。
            setPollNonce((n) => n + 1);
          } else {
            return fetchPlan(planId).then(setPlan);
          }
        })
        .finally(() => setBusy(false));
    },
    [planId, busy],
  );

  const reset = useCallback(() => {
    setGoal("");
    setPlanId(null);
    setPlan(null);
    setDispatched(false);
    setEdited(null);
    setPicked([]);
  }, []);

  if (dispatched) {
    return (
      <div className="nt">
        <div className="nt-hd">已派发</div>
        <div className="nt-line good">
          任务已经开跑，正在后台并行执行 —— 接着发布下一个，系统会同时跑。
        </div>
        <div className="nt-actions">
          <button type="button" className="nt-ghost" onClick={reset}>
            再发一个
          </button>
          <button type="button" className="nt-primary" onClick={onClose}>
            回到工作台
          </button>
        </div>
      </div>
    );
  }

  if (!planId) {
    return (
      <form className="nt" onSubmit={submit}>
        <div className="nt-hd">发布一个任务</div>
        <p className="nt-lead">
          说清你要什么就行 —— 系统会先把它拆成几步、自己复核一遍，
          拆解通过之后才开始花钱执行。
        </p>

        {/* 从零开始 / 接手已有项目：**这不是一个选项，是两件事**。
            接手时产物直接写进你选的目录（否则改不到已有文件），
            而且架构师会先拿到那儿的文件清单再拆 —— 不给的话它会把一个
            有内容的目录当空目录，从零重建一遍。 */}
        <div className="nt-modes">
          {(
            [
              ["new", "从零开始", "在一个空目录里做一件新的事"],
              ["takeover", "接手已有项目", "目录里已经有代码/文档，在它基础上继续"],
            ] as const
          ).map(([m, label, hint]) => (
            <button
              type="button"
              key={m}
              className={`nt-mode${mode === m ? " on" : ""}`}
              onClick={() => setMode(m)}
            >
              <b>{label}</b>
              <span>{hint}</span>
            </button>
          ))}
        </div>

        <label className="nt-ws">
          <span className="nt-ws-k">
            {mode === "takeover" ? "接手哪个文件夹" : "产物放在哪"}
          </span>
          <input
            className="nt-ws-input"
            placeholder={wsDefault || "例：D:\\work\\my-project"}
            value={ws}
            onChange={(e) => setWs(e.target.value)}
            spellCheck={false}
          />
          <button type="button" className="nt-browse" onClick={() => setPicking(true)}>
            浏览…
          </button>
        </label>
        {picking && (
          <FolderPicker
            value={ws || wsDefault}
            onPick={setWs}
            onClose={() => setPicking(false)}
          />
        )}
        <div className="nt-ws-note">
          {mode === "takeover" ? (
            <>
              产物**直接写进这个目录**，已有文件会被就地修改。
              架构师会先看一眼里面有什么，再决定怎么拆。
            </>
          ) : (
            <>
              每个任务一个子目录：<code>{(ws || wsDefault) || "…"}\&lt;任务id&gt;\</code>
              。留空就用设置页里的默认工作区。
            </>
          )}
        </div>

        <SkillPicker picked={picked} onToggle={toggleSkill} />

        <textarea
          className="nt-input"
          placeholder={
            "例：把 data/ 下的 CSV 转成一份 Markdown 报告，" +
            "包含每列的缺失率和一张分布表。\n\n" +
            "写得越具体，拆出来的验收标准越准。Ctrl/⌘+Enter 提交。"
          }
          value={goal}
          onChange={(e) => setGoal(e.target.value)}
          onKeyDown={(e) => {
            if ((e.metaKey || e.ctrlKey) && e.key === "Enter") submit(e);
          }}
          rows={8}
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
      <div className="nt-goal">{goal || plan?.goal || ""}</div>
      {plan?.workspace && (
        <div className="nt-ws-note">
          {plan.takeover ? "接手" : "产物落在"}：<code>{plan.workspace}</code>
        </div>
      )}
      {plan ? <StatusLine plan={plan} /> : <div className="nt-line">正在拆解…</div>}
      {plan && <ReviewNotes plan={plan} />}
      {plan?.draft && plan.specs?.length ? (
        <div className="nt-line nt-draftnote">
          下面是刚生成出来的**草稿**，复核者还在看 —— 它可能还会变。
          先摆出来是因为读它比看转圈有用。
        </div>
      ) : null}
      {plan && (
        <SpecList
          plan={plan}
          edited={edited}
          // 草稿还会被复核改写，这时候改它没有意义；派发之后同样不能改
          onEdit={plan.draft || plan.dispatched_root ? null : setEdited}
        />
      )}
      {edited && (
        <div className="nt-line nt-dirty">
          你改过这份拆解（还没保存）。保存时会**整份交上去**，
          架构师不会再改一遍 —— 沙箱、工具白名单、各类上限原样保留。
        </div>
      )}
      {error && <div className="nt-line bad">{error}</div>}
      <div className="nt-actions">
        <button type="button" className="nt-ghost" onClick={onClose}>
          关闭
        </button>
        {edited && (
          <button
            type="button"
            className="nt-ghost"
            disabled={busy}
            onClick={() => setEdited(null)}
          >
            撤销我的修改
          </button>
        )}
        {needsRuling && !edited && (
          <textarea
            className="nt-instruction"
            placeholder="哪里拆得不对？写一句，架构师会带着它重新拆（选填）…"
            value={instruction}
            onChange={(e) => setInstruction(e.target.value)}
            rows={2}
          />
        )}
        {needsRuling && !edited && (
          <button
            type="button"
            className="nt-ghost"
            disabled={busy}
            onClick={() => rule(false)}
          >
            否决这份拆解
          </button>
        )}
        {needsRuling && !edited && instruction.trim() && (
          <button
            type="button"
            className="nt-primary"
            disabled={busy}
            onClick={() => rule(false, undefined, instruction.trim())}
          >
            带着意见重新拆
          </button>
        )}
        {((needsRuling && Boolean(plan?.specs?.length)) || edited) && (
          <button
            type="button"
            className="nt-primary"
            disabled={busy}
            onClick={() => rule(true, edited ?? undefined)}
          >
            {edited ? "按我改的这份跑" : "就按这份拆解跑"}
          </button>
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

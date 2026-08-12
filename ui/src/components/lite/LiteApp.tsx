import { useState } from "react";
import type { AppProps } from "../../App";
import { LITE_STATUS } from "../../copy";
import NewTask from "../NewTask";
import LiteStream from "./LiteStream";

/** lite 模式的线程状态点色。 */
const LITE_DOT: Record<string, string> = {
  PENDING: "gray",
  RUNNING: "blue",
  INTERRUPTED: "gray",
  COMPLETED: "green",
  AWAITING_HUMAN: "amber",
  FAILED: "red",
  ABANDONED: "gray",
};

export default function LiteApp(props: AppProps) {
  const [composing, setComposing] = useState(false);
  // 从详情页「去裁决 / 去派发」跳转回来要恢复的那条 plan（M12 待办 #1）。
  const [resumePlanId, setResumePlanId] = useState<string | null>(null);
  const [confirm, setConfirm] = useState<string | null>(null);
  const [delErr, setDelErr] = useState<string | null>(null);
  // INTERRUPTED 是过渡态，不值得在 lite 里占一行
  const threads = props.threads.filter((t) => t.status !== "INTERRUPTED");
  const awaiting = threads.filter((t) => t.status === "AWAITING_HUMAN").length;
  // 「此刻在跑几条」—— 服务端本来就并行，侧栏得让人一眼看到（M12 待办 #2）
  const active = threads.filter((t) => !t.terminal).length;

  const openCompose = (planId?: string) => {
    setResumePlanId(planId ?? null);
    setComposing(true);
  };
  const closeCompose = () => {
    setComposing(false);
    setResumePlanId(null);
  };

  return (
    <div id="lite">
      <header className="l-top">
        <div className="l-brand">
          agent-cowork<span>群聊工作台</span>
        </div>
        <div className="spacer" />
        <button className="l-setbtn" onClick={props.onOpenSettings}>
          设置
        </button>
      </header>

      <div className="l-layout">
        <aside className="l-threads">
          <div className="l-threads-hd">
            任务
            {active > 0 && <span className="l-run-tag">{active} 条进行中</span>}
            {awaiting > 0 && <span className="l-await-tag">{awaiting} 件等你处理</span>}
          </div>
          <button className="nt-open" onClick={() => openCompose()}>
            ＋ 发布新任务
          </button>
          {threads.map((t) => (
            <div
              key={t.task_id}
              className={`l-thread${t.task_id === props.selected ? " sel" : ""}`}
              onClick={() => props.onSelect(t.task_id)}
            >
              <div className="l-t-title">{t.title}</div>
              <div className="l-t-meta">
                <span className={`l-dot ${LITE_DOT[t.status]}`} />
                {LITE_STATUS[t.status]}
                <button
                  className="l-del"
                  title="删掉这条记录（不会动工作区里的文件）"
                  onClick={(e) => {
                    e.stopPropagation();
                    setConfirm(t.task_id);
                  }}
                >
                  删除
                </button>
              </div>
            </div>
          ))}
          {confirm && (
            <div className="l-confirm-mask" onClick={() => setConfirm(null)}>
              <div className="l-confirm" onClick={(e) => e.stopPropagation()}>
                <b>删掉这条任务的记录？</b>
                {/* 说清楚**不会**发生什么 —— 用户最怕的是「我的代码没了」 */}
                <p>
                  只删对话、信号和裁决记录。
                  <b>工作区里已经产出的文件不会被动。</b>
                  正在跑的任务要先停下来才能删。
                </p>
                {delErr && <div className="l-fail">{delErr}</div>}
                <div className="l-confirm-row">
                  <button onClick={() => setConfirm(null)}>算了</button>
                  <button
                    className="danger"
                    onClick={() => {
                      const id = confirm;
                      setDelErr(null);
                      void props.onDelete(id).then((r) => {
                        if (r.ok) setConfirm(null);
                        else setDelErr(r.error ?? "没能删掉");
                      });
                    }}
                  >
                    删除
                  </button>
                </div>
              </div>
            </div>
          )}
        </aside>

        {composing ? (
          <main className="l-stream nt-host">
            <NewTask
              initialPlanId={resumePlanId}
              onDispatched={props.onDispatched}
              onClose={closeCompose}
            />
          </main>
        ) : props.detail && props.selected ? (
          <LiteStream
            key={props.selected}
            taskId={props.selected}
            detail={props.detail}
            onIntervene={props.onIntervene}
            onFollowUp={props.onFollowUp}
            onCancel={props.onCancel}
            onRuling={props.onRuling}
            onResumePlan={openCompose}
            onDispatched={props.onDispatched}
          />
        ) : (
          // 选中了但详情还没到（刚派发的线程有一小段真空期）。
          // 给一句话而不是一片空白 —— 空白会被当成「坏了」。
          <main className="l-stream l-waiting">
            {props.selected ? "正在准备这条任务…" : "左边选一条任务，或者发布一个新的。"}
          </main>
        )}
      </div>
    </div>
  );
}

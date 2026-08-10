import type { AppProps } from "../../App";
import { LITE_STATUS } from "../../copy";
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
  // INTERRUPTED 是过渡态，不值得在 lite 里占一行
  const threads = props.threads.filter((t) => t.status !== "INTERRUPTED");
  const awaiting = threads.filter((t) => t.status === "AWAITING_HUMAN").length;

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
        <div className="seg">
          <button className="on">简洁</button>
          <button onClick={() => props.onSwitchMode("pro")}>专业</button>
        </div>
      </header>

      <div className="l-layout">
        <aside className="l-threads">
          <div className="l-threads-hd">
            任务
            {awaiting > 0 && <span className="l-await-tag">{awaiting} 件等你处理</span>}
          </div>
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
              </div>
            </div>
          ))}
        </aside>

        {props.detail && props.selected ? (
          <LiteStream
            key={props.selected}
            taskId={props.selected}
            detail={props.detail}
            onIntervene={props.onIntervene}
            onRuling={props.onRuling}
          />
        ) : (
          <main className="l-stream" />
        )}
      </div>
    </div>
  );
}

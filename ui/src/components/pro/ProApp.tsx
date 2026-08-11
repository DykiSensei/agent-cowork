import { useState } from "react";
import type { AppProps } from "../../App";
import { STATUSES, fmtTok } from "../../copy";
import NewTask from "../NewTask";
import ProStream from "./ProStream";
import ProMetaPanel from "./ProMetaPanel";

export default function ProApp(props: AppProps) {
  const [composing, setComposing] = useState(false);
  const awaiting = props.threads.filter(
    (t) => t.status === "AWAITING_HUMAN",
  ).length;

  return (
    <div id="pro">
      <header className="topbar">
        <div className="brand">
          agent-cowork <span className="sub">/ ChatSurface</span>
        </div>
        <div className="spacer" />
        <span className="env">backend: scripted</span>
        <button className="env mode-btn" onClick={props.onOpenSettings}>
          ⚙ 设置
        </button>
        <button className="env mode-btn" onClick={() => props.onSwitchMode("lite")}>
          → 简洁版
        </button>
      </header>

      <div className="layout">
        <aside className="threads">
          <div className="threads-hd">
            <h2>Threads</h2>
            {awaiting > 0 && <span className="await-pill">{awaiting} 待你答复</span>}
            <button className="nt-open" onClick={() => setComposing(true)}>
              ＋ 新任务
            </button>
          </div>
          <ul className="thread-list">
            {props.threads.map((t) => (
              <li
                key={t.task_id}
                className={`thread${t.task_id === props.selected ? " sel" : ""}`}
                onClick={() => props.onSelect(t.task_id)}
              >
                <div className="t-title">{t.title}</div>
                <div className="t-meta">
                  <span className={`dot ${t.status}`} />
                  {t.status}
                  <span>
                    {t.revision > 1 ? `rev ${t.revision} · ` : ""}
                    {t.current_step > 0 ? `step ${t.current_step} · ` : ""}
                    {fmtTok(t.tokens_used)} tok
                  </span>
                  {t.status === "AWAITING_HUMAN" && (
                    <span className="t-tag hot">待答复</span>
                  )}
                  {t.composite && <span className="t-tag">复合</span>}
                </div>
              </li>
            ))}
          </ul>
          <div className="legend">
            {STATUSES.map((s) => (
              <span className="lg" key={s}>
                <span className={`dot ${s}`} />
                {s}
              </span>
            ))}
          </div>
        </aside>

        {composing ? (
          <main className="stream nt-host">
            <NewTask
              onDispatched={props.onDispatched}
              onClose={() => setComposing(false)}
            />
          </main>
        ) : props.detail && props.selected ? (
          <ProStream
            key={props.selected}
            taskId={props.selected}
            detail={props.detail}
            onIntervene={props.onIntervene}
            onCancel={props.onCancel}
            onRuling={props.onRuling}
          />
        ) : (
          <main className="stream waiting">
            {props.selected ? "正在准备这条线程…" : "选一条线程，或者发布一个新任务。"}
          </main>
        )}

        {props.detail && <ProMetaPanel detail={props.detail} />}
      </div>
    </div>
  );
}

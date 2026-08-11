import { useState } from "react";
import { fmtTok } from "../copy";
import type { TaskDetail } from "../types";

/**
 * 「技术细节」抽屉 —— 专业版弃用之后，它独有的那些信息搬到这里，默认收起。
 *
 * 弃用专业版的理由是它不好看；但那一版上有几样东西**只有它有**：spec 全文、
 * 验收标准、硬信号覆盖面、预算水位、checkpoint id。直接删掉等于把可观测性
 * 一起删了 —— 而这套系统的卖点恰好是「每一步都看得见」。
 *
 * 所以做成折叠：默认不打扰，想看的时候一次给全。
 */
export default function Details({ detail }: { detail: TaskDetail }) {
  const [open, setOpen] = useState(false);

  const tasks =
    detail.kind === "composite"
      ? Object.values(detail.tasks)
      : [detail.state];

  return (
    <details className="dt" open={open} onToggle={(e) => setOpen(e.currentTarget.open)}>
      <summary>技术细节</summary>
      {open && (
        <div className="dt-body">
          {detail.kind === "composite" && detail.review && (
            <div className="dt-block">
              <div className="dt-h">拆解复核</div>
              <div className="dt-kv">
                <span>覆盖完整</span>
                <b>{detail.review.sufficient ? "是" : "有缺口"}</b>
              </div>
              <div className="dt-kv">
                <span>复核者</span>
                <b>
                  {detail.review.reviewer}
                  {detail.review.independent ? "（独立）" : "（与拆解者同一个后端）"}
                </b>
              </div>
              {detail.review.missing.map((m, i) => (
                <div className="dt-note" key={i}>
                  · {m}
                </div>
              ))}
            </div>
          )}

          {tasks.map((t) => (
            <div className="dt-block" key={t.task_id}>
              <div className="dt-h">
                {t.goal}
                <span className="dt-id">{t.task_id}</span>
              </div>
              <div className="dt-kv">
                <span>状态 / 版本</span>
                <b>
                  {t.status} · rev {t.revision}
                </b>
              </div>
              <div className="dt-kv">
                <span>进度</span>
                <b>
                  {t.current_step} / {t.spec.max_steps} 步 · 中断 {t.interrupt_count} 次
                </b>
              </div>
              <div className="dt-kv">
                <span>成本</span>
                <b>
                  {fmtTok(t.tokens_used)} tok
                  {/* token_budget = 0 是「不限」（M11 起的默认）。
                      写成 "/ 0 tok" 会让人以为额度是 0、马上要炸 */}
                  {t.spec.token_budget ? ` / ${fmtTok(t.spec.token_budget)}` : "（不限）"}
                </b>
              </div>
              <div className="dt-kv">
                <span>可写路径</span>
                <b className="mono">{t.spec.scope.join("、") || "—"}</b>
              </div>
              <div className="dt-kv">
                <span>工具</span>
                <b className="mono">{t.spec.tools.join(" ")}</b>
              </div>
              <div className="dt-kv">
                <span>沙箱</span>
                <b>
                  {t.spec.sandbox
                    ? `${t.spec.sandbox.use_docker ? "Docker" : "本地白名单"} · ` +
                      `${t.spec.sandbox.allowed_binaries.join("/")} · network ${t.spec.sandbox.network}`
                    : "—"}
                </b>
              </div>
              <div className="dt-h2">验收标准</div>
              {t.spec.acceptance.map((c) => (
                <div className="dt-note" key={c.id}>
                  · {c.description}
                  {c.command ? (
                    <span className="dt-cmd"> 〔{c.command.join(" ")}〕</span>
                  ) : (
                    <span className="dt-cmd"> 〔架构师判断〕</span>
                  )}
                </div>
              ))}
              <div className="dt-h2">
                能产生的硬信号（{t.spec.hard_signals.length}）
              </div>
              <div className="dt-note mono">{t.spec.hard_signals.join(" ")}</div>
              {t.checkpoint_id && (
                <div className="dt-kv">
                  <span>checkpoint</span>
                  <b className="mono">{t.checkpoint_id}</b>
                </div>
              )}
            </div>
          ))}
        </div>
      )}
    </details>
  );
}

import { useCallback, useEffect, useRef, useState } from "react";
import type { ActionResult } from "./api";
import {
  fetchDetail,
  fetchProviders,
  fetchThreads,
  postCancel,
  postIntervene,
  postRuling,
  subscribeStream,
} from "./api";
import type { TaskDetail, ThreadSummary } from "./types";
import ProApp from "./components/pro/ProApp";
import LiteApp from "./components/lite/LiteApp";
import SettingsPage from "./components/Settings";

export type Mode = "lite" | "pro";

export interface AppProps {
  threads: ThreadSummary[];
  selected: string | null;
  onSelect: (id: string) => void;
  detail: TaskDetail | null;
  onSwitchMode: (m: Mode) => void;
  onOpenSettings: () => void;
  /** 派发完成后把新线程选中并刷新列表。 */
  onDispatched: (rootId: string) => void;
  onIntervene: (taskId: string, instruction: string) => Promise<ActionResult>;
  onCancel: (taskId: string, reason: string) => Promise<ActionResult>;
  onRuling: (taskId: string, action: string, rationale: string) => Promise<ActionResult>;
}

/** 全新用户的第一屏：一家 key 都没配、也还没有任何线程。 */
function FirstRun({ onOpenSettings }: { onOpenSettings: () => void }) {
  return (
    <div className="first-run">
      <div className="first-run-card">
        <h1>先填一个 API Key</h1>
        <p>
          还没有配置任何供应商，所以现在发任务会失败。去设置页填一个 key
          就能开始 —— 填完可以当场点「测试连接」确认它真的能用。
        </p>
        <button onClick={onOpenSettings}>去设置页</button>
        <p className="first-run-alt">
          只想先看看这套东西怎么跑？在终端里执行 <code>cowork demo</code> ——
          它用脚本后端，不需要任何 key，完整链路照样走一遍。
        </p>
      </div>
    </div>
  );
}

function initialMode(): Mode {
  // 深链：#pro 切专业模式，#task_xxx 选线程，可叠加成 #pro,task_comp
  if (location.hash.includes("pro")) return "pro";
  return localStorage.getItem("cowork-mode") === "pro" ? "pro" : "lite";
}

/** 服务端按 updated_at 排序（views.thread_list），这里把「等你处理」的顶到最前。 */
function sortThreads(ts: ThreadSummary[]): ThreadSummary[] {
  return [...ts].sort(
    (a, b) =>
      Number(b.status === "AWAITING_HUMAN") - Number(a.status === "AWAITING_HUMAN") ||
      (b.updated_at ?? 0) - (a.updated_at ?? 0),
  );
}

export default function App() {
  const [mode, setMode] = useState<Mode>(initialMode);
  const [page, setPage] = useState<"chat" | "settings">(() =>
    location.hash.includes("settings") ? "settings" : "chat",
  );
  const [threads, setThreads] = useState<ThreadSummary[] | null>(null);
  const [selected, setSelected] = useState<string | null>(null);
  const [detail, setDetail] = useState<TaskDetail | null>(null);
  const [error, setError] = useState<string | null>(null);
  // null = 还没问出来；true = 一家 key 都没配
  const [needsSetup, setNeedsSetup] = useState(false);

  useEffect(() => {
    void fetchProviders()
      .then((ps) => setNeedsSetup(ps.every((p) => !p.configured)))
      .catch(() => setNeedsSetup(false)); // 问不到就别挡路
  }, [page]);

  useEffect(() => {
    // 支持 #task_xxx 深链（可与 #pro 叠加：#pro,task_comp）
    const hashTask = /task_\w+/.exec(location.hash)?.[0] ?? "";
    fetchThreads()
      .then((ts) => {
        setThreads(sortThreads(ts));
        setSelected(
          (s) =>
            s ??
            (ts.some((t) => t.task_id === hashTask)
              ? hashTask
              : (ts[0]?.task_id ?? null)),
        );
      })
      .catch((e: unknown) => setError(String(e)));
  }, []);

  useEffect(() => {
    if (!selected) return;
    let alive = true;
    setDetail(null);
    fetchDetail(selected)
      .then((d) => {
        if (alive) setDetail(d);
      })
      // **详情拿不到不是致命错误，不要换掉整个页面。**
      // 刚派发的线程会有一小段真空期（服务端已修，但网络抖动、旧库同样会走到
      // 这里）。实测撞到的形态：拆解完成、界面切到新线程，整页变成
      // 「连不上服务」，刷新一下又好了 —— 而任务其实一直在正常跑。
      .catch(() => {
        if (alive) setDetail(null);
      });
    return () => {
      alive = false;
    };
  }, [selected]);

  const refresh = useCallback((taskId?: string | null) => {
    fetchThreads()
      .then((ts) => setThreads(sortThreads(ts)))
      .catch(() => {});
    if (taskId) {
      fetchDetail(taskId)
        .then((d) => setDetail(d))
        .catch(() => {});
    }
  }, []);

  // SSE：事件只是「该重拉了」的通知，正文永远以 task_detail 为准（接口文档 §10.4）
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  useEffect(() => {
    if (page !== "chat") return; // 设置页不需要事件流
    let timer: ReturnType<typeof setTimeout> | null = null;
    const stop = subscribeStream(() => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => refresh(selectedRef.current), 400);
    });
    return () => {
      if (timer) clearTimeout(timer);
      stop();
    };
  }, [page, refresh]);

  const switchMode = useCallback((m: Mode) => {
    setMode(m);
    localStorage.setItem("cowork-mode", m);
  }, []);

  const onIntervene = useCallback(
    (taskId: string, instruction: string) => postIntervene(taskId, instruction),
    [],
  );

  const onCancel = useCallback(
    (taskId: string, reason: string) => postCancel(taskId, reason),
    [],
  );

  const onRuling = useCallback(
    (taskId: string, action: string, rationale: string) =>
      postRuling(taskId, action, rationale),
    [],
  );

  const onDispatched = useCallback(
    (rootId: string) => {
      setSelected(rootId);
      refresh(rootId);
    },
    [refresh],
  );

  if (error) {
    return (
      <div style={{ padding: 40, fontFamily: "monospace" }}>
        连不上服务：{error}（真实服务用 cowork serve，界面开发用 npm run dev）
      </div>
    );
  }

  if (page === "settings") {
    return (
      <SettingsPage
        onBack={() => {
          setPage("chat");
          history.replaceState(null, "", location.pathname);
        }}
      />
    );
  }
  if (!threads) return null;

  // 一家 key 都没配 + 一条线程都没有 = 全新用户第一次打开。
  // 别让他先提交一个必然失败的任务再去找设置页 —— 直接把路指出来。
  // （**不在服务端拦**：设置页正是填 key 的地方，拒绝启动会死锁。）
  if (needsSetup && threads.length === 0) {
    return (
      <FirstRun
        onOpenSettings={() => {
          setPage("settings");
          history.replaceState(null, "", "#settings");
        }}
      />
    );
  }

  const props: AppProps = {
    threads,
    selected,
    onSelect: setSelected,
    detail,
    onSwitchMode: switchMode,
    onOpenSettings: () => {
      setPage("settings");
      history.replaceState(null, "", "#settings");
    },
    onDispatched,
    onIntervene,
    onCancel,
    onRuling,
  };
  return mode === "lite" ? <LiteApp {...props} /> : <ProApp {...props} />;
}

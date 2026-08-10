import { useCallback, useEffect, useRef, useState } from "react";
import {
  fetchDetail,
  fetchThreads,
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
  onIntervene: (taskId: string, instruction: string) => Promise<boolean>;
  onRuling: (taskId: string, action: string, rationale: string) => Promise<boolean>;
}

function initialMode(): Mode {
  // 深链：#pro 切专业模式，#task_xxx 选线程，可叠加成 #pro,task_comp
  if (location.hash.includes("pro")) return "pro";
  return localStorage.getItem("cowork-mode") === "pro" ? "pro" : "lite";
}

/** 服务端按 updated_at 排序（views.thread_list），这里把「等你处理」的顶到最前。 */
function sortThreads(ts: ThreadSummary[]): ThreadSummary[] {
  return ts.sort(
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
      .catch((e: unknown) => setError(String(e)));
    return () => {
      alive = false;
    };
  }, [selected]);

  // SSE：事件只是「该重拉了」的通知，正文永远以 task_detail 为准（接口文档 §10.4）
  const selectedRef = useRef(selected);
  selectedRef.current = selected;
  useEffect(() => {
    if (page !== "chat") return; // 设置页不需要事件流
    let timer: ReturnType<typeof setTimeout> | null = null;
    const stop = subscribeStream(() => {
      if (timer) clearTimeout(timer);
      timer = setTimeout(() => {
        fetchThreads()
          .then((ts) => setThreads(sortThreads(ts)))
          .catch(() => {});
        const cur = selectedRef.current;
        if (cur) {
          fetchDetail(cur)
            .then((d) => setDetail(d))
            .catch(() => {});
        }
      }, 400);
    });
    return () => {
      if (timer) clearTimeout(timer);
      stop();
    };
  }, [page]);

  const switchMode = useCallback((m: Mode) => {
    setMode(m);
    localStorage.setItem("cowork-mode", m);
  }, []);

  const onIntervene = useCallback(async (taskId: string, instruction: string) => {
    return (await postIntervene(taskId, instruction)).ok;
  }, []);

  const onRuling = useCallback(
    async (taskId: string, action: string, rationale: string) => {
      return (await postRuling(taskId, action, rationale)).ok;
    },
    [],
  );

  if (error) {
    return (
      <div style={{ padding: 40, fontFamily: "monospace" }}>
        mock API 连不上：{error}（用 npm run dev / preview 启动）
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
    onIntervene,
    onRuling,
  };
  return mode === "lite" ? <LiteApp {...props} /> : <ProApp {...props} />;
}

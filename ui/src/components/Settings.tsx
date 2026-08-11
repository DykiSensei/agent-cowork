import { useEffect, useState } from "react";
import {
  fetchProviders,
  fetchSettings,
  putProviderKey,
  putSettings,
  testProvider,
} from "../api";
import type { ProbeResult, ProviderInfo, Settings } from "../types";

/**
 * 设置页：各家供应商的 API Key + 全局模型 / 推理挡位。
 *
 * 密钥纪律（与 AGENTS.md 一致）：key **只写不读** —— 服务端永远不回显，
 * 最多给最后 4 位的识别串。mock 存在 ui/mock/settings.local.json；
 * 真实语义（写 .env 还是配置库、要不要落盘加密）归服务层决定。
 */

const EFFORT_LEVELS = ["off", "low", "medium", "high", "max"] as const;

const EFFORT_ROLES: { key: keyof Settings["effort"]; label: string; hint: string }[] = [
  { key: "architect", label: "架构师", hint: "决定整条链走向的那几次调用，默认 high" },
  { key: "subagent", label: "Subagent", hint: "干活的，默认 medium" },
  { key: "cheap", label: "廉价角色", hint: "分诊 / 探查 / 摘要只判方向，默认 off" },
];

const MODEL_ROLES: { key: keyof Settings["models"]; label: string; env: string }[] = [
  { key: "architect", label: "架构师模型", env: "COWORK_ARCHITECT_MODEL" },
  { key: "subagent", label: "Subagent 模型", env: "COWORK_SUBAGENT_MODEL" },
  { key: "triage", label: "分诊模型", env: "COWORK_TRIAGE_MODEL" },
];

/** 三个角色分别用哪一家。**和上面的模型 id 覆盖是两件事** —— 这是「谁来干」。 */
const PROVIDER_ROLES: {
  key: keyof Settings["providers"];
  label: string;
  hint: string;
}[] = [
  {
    key: "architect",
    label: "生成者 / 架构师",
    hint: "拆解、中断决策、验收、分诊都走它 —— 整条链最有杠杆的那几次调用",
  },
  {
    key: "reviewer",
    label: "复核者",
    hint: "只看不改：复核拆解和写入。换一家才叫独立复核（§11.11）",
  },
  {
    key: "subagent",
    label: "Subagent",
    hint: "真正干活的。留空 = 跟架构师同一家",
  },
];

/** 探测结果 → 一句人话。四种状态的结论不同，不能都说成「失败」。 */
const PROBE_TEXT: Record<ProbeResult["status"], string> = {
  ok: "可用",
  mismatch: "预设的模型 id 服务端没有",
  unreachable: "问不到（网络或这家没有这个接口）",
  skipped: "没测到",
};

function ProviderCard({ p }: { p: ProviderInfo }) {
  const [input, setInput] = useState("");
  const [configured, setConfigured] = useState(p.configured);
  const [hint, setHint] = useState(p.key_hint);
  const [saving, setSaving] = useState(false);
  const [probe, setProbe] = useState<ProbeResult | null>(null);
  const [testing, setTesting] = useState(false);
  // 保存**可能失败**：值里有换行会被 400 拦（那是 .env 注入防线），.env 写不进去
  // 是 500。原来这里不看返回码，一律显示「已填 ····abcd」——
  // 于是唯一会拒绝的那条路径，恰好在界面上长得像成功。
  const [failed, setFailed] = useState<string | null>(null);

  const save = async (key: string) => {
    setSaving(true);
    setFailed(null);
    try {
      const r = await putProviderKey(p.name, key);
      if (!r.ok) {
        setFailed(r.error ?? "没能保存");
        return;
      }
      setConfigured(key.length > 0);
      setHint(key ? `····${key.slice(-4)}` : null);
      setInput("");
      setProbe(null); // 换了 key，上一次的探测结果作废
    } finally {
      setSaving(false);
    }
  };

  const test = async () => {
    setTesting(true);
    try {
      setProbe(await testProvider(p.name));
    } catch {
      setProbe({ name: p.name, status: "unreachable", detail: "服务端没应答" });
    } finally {
      setTesting(false);
    }
  };

  // 预设已验证（我们的事）和 key 能不能用（你的事）是两件事，分开显示 ——
  // 合成一个标签的话，用户填完 key 看到「未验证」会以为是自己填错了
  const presetVerified = p.preset_verified ?? p.verified;

  return (
    <div className="set-card">
      <div className="set-card-hd">
        <b>{p.name}</b>
        <span
          className={`set-tag${presetVerified ? " ok" : ""}`}
          title={
            presetVerified
              ? "我们在本机用真 key 打通过这一行的 model id"
              : "这一行的 model id 没被我们验证过 —— 没验证不等于错"
          }
        >
          预设{presetVerified ? "已验证" : "未验证"}
        </span>
        {configured ? (
          <span className="set-key-ok">已填 {hint}</span>
        ) : (
          <span className="set-key-no">未填</span>
        )}
      </div>
      <div className="set-row">
        <span className="set-k">env</span>
        <span className="mono">{p.key_env}</span>
      </div>
      <div className="set-row">
        <span className="set-k">base</span>
        <span className="mono">{p.base ?? "（官方 SDK，不走 base）"}</span>
      </div>
      <div className="set-row">
        <span className="set-k">models</span>
        <span className="mono">
          {p.models.subagent ?? "?"}
          {p.models.architect !== p.models.subagent ? ` / ${p.models.architect}` : ""}
          {p.models.triage !== p.models.subagent ? ` / ${p.models.triage}` : ""}
        </span>
      </div>
      <div className="set-keyrow">
        <input
          type="password"
          placeholder={configured ? "更换 Key…" : "粘贴 API Key…"}
          value={input}
          onChange={(e) => setInput(e.target.value)}
          autoComplete="off"
        />
        <button disabled={saving || !input} onClick={() => void save(input)}>
          保存
        </button>
        {configured && (
          <button className="set-clear" disabled={saving} onClick={() => void save("")}>
            清除
          </button>
        )}
      </div>
      {failed && <div className="set-fail">没能保存：{failed}</div>}
      <div className="set-testrow">
        <button className="set-test" disabled={testing || !configured} onClick={() => void test()}>
          {testing ? "测试中…" : "测试连接"}
        </button>
        {probe ? (
          <span className={`set-probe ${probe.status}`} title={probe.detail}>
            {PROBE_TEXT[probe.status]}
          </span>
        ) : (
          <span className="set-probe none">
            {configured ? "「已填」只说明填了，没说明能用 —— 测一下" : "填了 key 才能测"}
          </span>
        )}
      </div>
    </div>
  );
}

export default function SettingsPage({ onBack }: { onBack: () => void }) {
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [savedMsg, setSavedMsg] = useState("");
  const [failed, setFailed] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);

  useEffect(() => {
    // 拿不到就说出来。原来是裸 .then()，服务不在时这一页永远停在「加载中…」
    void fetchProviders()
      .then(setProviders)
      .catch((e: unknown) => setLoadError(String(e)));
    void fetchSettings()
      .then(setSettings)
      .catch((e: unknown) => setLoadError(String(e)));
  }, []);

  const saveGlobals = async () => {
    if (!settings) return;
    setFailed(null);
    const r = await putSettings(settings);
    if (!r.ok) {
      setFailed(r.error ?? "没能保存");
      return;
    }
    setSavedMsg("已保存");
    setTimeout(() => setSavedMsg(""), 2500);
  };

  return (
    <div className="set-page">
      <div className="set-col">
        <div className="set-top">
          <button className="set-back" onClick={onBack}>
            ← 返回
          </button>
          <h1>设置</h1>
        </div>

        <h2>供应商 API</h2>
        <p className="set-note">
          Key 只写不读：保存后这里只显示最后 4 位。真实服务写 .env（配置立即
          生效于新起跑的任务）；mock 存在 ui/mock/settings.local.json。
        </p>
        {loadError && <div className="set-fail">读不到设置：{loadError}</div>}
        <div className="set-grid">
          {providers === null
            ? loadError
              ? null
              : "加载中…"
            : providers.map((p) => <ProviderCard key={p.name} p={p} />)}
        </div>

        {settings && (
          <>
            <h2>谁来干哪一段</h2>
            <p className="set-note">
              只能从**已经填了 key** 的供应商里选（上面显示「已填」的那些）。
              留空 = 自动：架构师用启动时那家，复核者自动挑一家**不同的**，
              Subagent 跟架构师同一家。
            </p>
            <div className="set-card">
              {PROVIDER_ROLES.map((r) => {
                const ready = (providers ?? []).filter((p) => p.configured);
                const value = settings.providers[r.key];
                const sameAsArchitect =
                  r.key === "reviewer" &&
                  value &&
                  value !== "none" &&
                  value === settings.providers.architect;
                return (
                  <div className="set-row" key={r.key}>
                    <span className="set-k">
                      {r.label} <span className="set-env">{r.hint}</span>
                    </span>
                    <select
                      className="set-text"
                      value={value}
                      onChange={(e) =>
                        setSettings({
                          ...settings,
                          providers: { ...settings.providers, [r.key]: e.target.value },
                        })
                      }
                    >
                      <option value="">自动</option>
                      {r.key === "reviewer" && (
                        <option value="none">关掉独立复核（退回同模型）</option>
                      )}
                      {ready.map((p) => (
                        <option key={p.name} value={p.name}>
                          {p.name}
                        </option>
                      ))}
                    </select>
                    {sameAsArchitect && (
                      <span className="set-warn" title="§11.11">
                        和架构师同一家 —— 那不是独立复核
                      </span>
                    )}
                  </div>
                );
              })}
              {(providers ?? []).filter((p) => p.configured).length < 2 && (
                <p className="set-note" style={{ margin: "2px 0 0" }}>
                  只填了一家 key，所以现在选谁都是同一家。独立复核要至少两家。
                </p>
              )}
            </div>

            <h2>工作区</h2>
            <p className="set-note">
              任务的产物落在这里。从零开始的任务进 <code>&lt;工作区&gt;\&lt;任务id&gt;\</code>，
              接手已有项目时直接写进你在发布页指定的那个目录。
            </p>
            <div className="set-card">
              <div className="set-row">
                <span className="set-k">
                  默认工作区 <span className="mono set-env">COWORK_WORKSPACE</span>
                </span>
                <input
                  className="set-text"
                  placeholder={settings.workspace_default}
                  value={settings.workspace}
                  onChange={(e) =>
                    setSettings({ ...settings, workspace: e.target.value })
                  }
                  spellCheck={false}
                />
              </div>
              <p className="set-note" style={{ margin: "2px 0 0" }}>
                留空就用 <code>{settings.workspace_default}</code>。
                要绝对路径 —— 相对路径会落在服务进程的当前目录下，那多半不是你想放东西的地方。
              </p>
            </div>

            <h2>全局模型与推理挡位</h2>
            <p className="set-note">
              留空 = 用供应商预设。挡位词表统一 off / low / medium / high / max，
              各家向最近的真实档位取整（有的家关不掉思考，会如实回落到最低档）。
            </p>
            <div className="set-card">
              <div className="set-row">
                <span className="set-k">COWORK_LLM_BASE_URL</span>
                <input
                  className="set-text"
                  placeholder="自定义 base URL（留空 = 各家默认）"
                  value={settings.base_url_override}
                  onChange={(e) =>
                    setSettings({ ...settings, base_url_override: e.target.value })
                  }
                />
              </div>
              {MODEL_ROLES.map((r) => (
                <div className="set-row" key={r.key}>
                  <span className="set-k">
                    {r.label} <span className="mono set-env">{r.env}</span>
                  </span>
                  <input
                    className="set-text"
                    placeholder="留空 = 供应商预设"
                    value={settings.models[r.key]}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        models: { ...settings.models, [r.key]: e.target.value },
                      })
                    }
                  />
                </div>
              ))}
              {EFFORT_ROLES.map((r) => (
                <div className="set-row" key={r.key}>
                  <span className="set-k">
                    {r.label} <span className="set-env">{r.hint}</span>
                  </span>
                  <select
                    className="set-text"
                    value={settings.effort[r.key]}
                    onChange={(e) =>
                      setSettings({
                        ...settings,
                        effort: { ...settings.effort, [r.key]: e.target.value },
                      })
                    }
                  >
                    {EFFORT_LEVELS.map((l) => (
                      <option key={l} value={l}>
                        {l}
                      </option>
                    ))}
                  </select>
                </div>
              ))}
              <div className="set-row">
                <span className="set-k">
                  写入侧复核{" "}
                  <span className="set-env">
                    改任务规格前先让复核者看一眼；关掉后架构师自己拍板
                  </span>
                </span>
                <select
                  className="set-text"
                  value={settings.review_writes}
                  onChange={(e) =>
                    setSettings({ ...settings, review_writes: e.target.value })
                  }
                >
                  <option value="on">开（默认）</option>
                  <option value="off">关</option>
                </select>
              </div>
              {settings.review_writes === "off" && (
                <p className="set-note" style={{ margin: "2px 0 0" }}>
                  关掉之后，「把失败的输入从目标里摘出去」和「把校验脚本纳入可写范围」
                  这两种改动没有任何东西会拦 —— 它们会让任务<b>看起来成功</b>。
                </p>
              )}
              {failed && <div className="set-fail">没能保存：{failed}</div>}
              <div className="set-row" style={{ justifyContent: "flex-end" }}>
                {savedMsg && <span className="set-key-ok">{savedMsg}</span>}
                <button className="set-save" onClick={() => void saveGlobals()}>
                  保存全局设置
                </button>
              </div>
            </div>
          </>
        )}
      </div>
    </div>
  );
}

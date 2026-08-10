import { useEffect, useState } from "react";
import { fetchProviders, fetchSettings, putProviderKey, putSettings } from "../api";
import type { ProviderInfo, Settings } from "../types";

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

function ProviderCard({ p }: { p: ProviderInfo }) {
  const [input, setInput] = useState("");
  const [configured, setConfigured] = useState(p.configured);
  const [hint, setHint] = useState(p.key_hint);
  const [saving, setSaving] = useState(false);

  const save = async (key: string) => {
    setSaving(true);
    try {
      await putProviderKey(p.name, key);
      setConfigured(key.length > 0);
      setHint(key ? `····${key.slice(-4)}` : null);
      setInput("");
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="set-card">
      <div className="set-card-hd">
        <b>{p.name}</b>
        <span className={`set-tag${p.verified ? " ok" : ""}`}>
          {p.verified ? "已验证" : "未验证"}
        </span>
        {configured ? (
          <span className="set-key-ok">已配置 {hint}</span>
        ) : (
          <span className="set-key-no">未配置</span>
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
    </div>
  );
}

export default function SettingsPage({ onBack }: { onBack: () => void }) {
  const [providers, setProviders] = useState<ProviderInfo[] | null>(null);
  const [settings, setSettings] = useState<Settings | null>(null);
  const [savedMsg, setSavedMsg] = useState("");

  useEffect(() => {
    void fetchProviders().then(setProviders);
    void fetchSettings().then(setSettings);
  }, []);

  const saveGlobals = async () => {
    if (!settings) return;
    await putSettings(settings);
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
        <div className="set-grid">
          {providers === null
            ? "加载中…"
            : providers.map((p) => <ProviderCard key={p.name} p={p} />)}
        </div>

        {settings && (
          <>
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

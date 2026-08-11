import { useEffect, useState } from "react";
import { browseFs } from "../api";
import type { FsListing } from "../types";

/**
 * 文件夹选择器。
 *
 * **为什么不是 `<input type="file" webkitdirectory>`**：那个给的是文件句柄和
 * 相对名，**拿不到绝对路径**（浏览器不给），而服务端要的正是绝对路径。
 * 但这个服务本来就跑在本机 —— 所以「服务端列目录、界面上点」是唯一既能点、
 * 又能拿到真路径的做法。
 *
 * 起点是主目录 / 桌面 / 各盘符，不是文件系统根：从 C:\ 开始点对人没有帮助。
 */
export default function FolderPicker({
  value,
  onPick,
  onClose,
}: {
  value: string;
  onPick: (path: string) => void;
  onClose: () => void;
}) {
  const [listing, setListing] = useState<FsListing | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [at, setAt] = useState(value);

  useEffect(() => {
    let alive = true;
    setError(null);
    browseFs(at)
      .then((l) => alive && setListing(l))
      .catch((e: unknown) => {
        if (!alive) return;
        setError(String(e));
        setListing(null);
      });
    return () => {
      alive = false;
    };
  }, [at]);

  return (
    <div className="fp-mask" onClick={onClose}>
      <div className="fp" onClick={(e) => e.stopPropagation()}>
        <div className="fp-hd">
          选一个文件夹
          <button className="fp-x" onClick={onClose} title="关闭">
            ×
          </button>
        </div>

        <div className="fp-bar">
          <button
            className="fp-up"
            disabled={!listing || listing.roots}
            onClick={() => setAt(listing?.parent ?? "")}
          >
            ↑ 上一层
          </button>
          <input
            className="fp-path"
            value={at}
            onChange={(e) => setAt(e.target.value)}
            placeholder="也可以直接粘贴路径"
            spellCheck={false}
          />
        </div>

        {error && <div className="fp-err">{error}</div>}

        <div className="fp-list">
          {listing?.entries.length === 0 && (
            <div className="fp-empty">这个文件夹里没有子文件夹</div>
          )}
          {listing?.entries.map((e) => (
            <button key={e.path} className="fp-item" onClick={() => setAt(e.path)}>
              <span className="fp-ico">📁</span>
              {e.name}
            </button>
          ))}
        </div>

        <div className="fp-ft">
          {/* 「就用这个」用的是当前所在目录，不是选中的子目录 ——
              进到目标文件夹里再确认，比在父目录里选一行更不容易点错 */}
          <span className="fp-cur" title={listing?.path || ""}>
            {listing?.roots ? "先进到一个文件夹里" : listing?.path}
          </span>
          <button
            className="fp-ok"
            disabled={!listing || listing.roots}
            onClick={() => {
              onPick(listing!.path);
              onClose();
            }}
          >
            就用这个
          </button>
        </div>
      </div>
    </div>
  );
}

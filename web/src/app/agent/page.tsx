'use client';

/**
 * /agent — 智能体指挥台 (手机端)
 *
 * 让店主/运营用手机直接指挥 Hermes/Cindy 等智能体接管系统：
 *   - 📝 命令输入: 自然语言 (如 "上架 丹东草莓 19.9元 30件") → 路由工具执行
 *   - 🎥 视频传货: 手机实拍视频/图片上传 → 自动建商品+库存 (真实文件即商品图)
 *   - 🧰 工具面板: 全部 omodul 工具清单 (Hermes/Cindy 接管的同款协议)
 *
 * 鉴权: 需要管理员 JWT (hemall_auth), 未登录跳转 /login。
 */

import { useEffect, useMemo, useState } from 'react';
import { useRouter } from 'next/navigation';
import { getAuth } from '@/lib/auth-store';
import { api } from '@/lib/api-client';

interface ToolInfo {
  tool: string;
  domain: string;
  name: string;
  path: string;
  require_auth: boolean;
  parameters: Record<string, any>;
}

interface ExecResult {
  status?: string;
  tool?: string;
  args?: Record<string, any>;
  matched_keyword?: string;
  result?: any;
  hint?: string;
  error?: string;
  input?: string;
}

export default function AgentConsolePage() {
  const router = useRouter();
  const [tools, setTools] = useState<ToolInfo[]>([]);
  const [cmd, setCmd] = useState('');
  const [busy, setBusy] = useState(false);
  const [log, setLog] = useState<ExecResult[]>([]);
  const [uploading, setUploading] = useState(false);
  const [mediaUrl, setMediaUrl] = useState('');

  // 鉴权守卫
  useEffect(() => {
    if (!getAuth()?.token) router.replace('/login');
  }, [router]);

  // 拉取工具清单
  useEffect(() => {
    (async () => {
      try {
        const res = await api.agentListTools();
        setTools(res.tools ?? []);
      } catch (e: any) {
        setLog((l) => [...l, { error: `工具清单加载失败: ${e.message}` }]);
      }
    })();
  }, []);

  const toolGroups = useMemo(() => {
    const map = new Map<string, ToolInfo[]>();
    for (const t of tools) {
      if (!map.has(t.domain)) map.set(t.domain, []);
      map.get(t.domain)!.push(t);
    }
    return Array.from(map.entries());
  }, [tools]);

  async function sendCommand() {
    const text = cmd.trim();
    if (!text || busy) return;
    setBusy(true);
    try {
      const res = await api.agentCommand(text);
      setLog((l) => [{ ...res, input: text }, ...l]);
    } catch (e: any) {
      setLog((l) => [{ input: text, error: e.message }, ...l]);
    } finally {
      setBusy(false);
      setCmd('');
    }
  }

  async function uploadVideo(e: React.ChangeEvent<HTMLInputElement>) {
    const file = e.target.files?.[0];
    if (!file || uploading) return;
    setUploading(true);
    try {
      const res = await api.agentIngest(file);
      setLog((l) => [{ ...res, input: `📹 传货: ${file.name}` }, ...l]);
      if (res.media_url) setMediaUrl(res.media_url);
    } catch (err: any) {
      setLog((l) => [{ input: `📹 传货: ${file.name}`, error: err.message }, ...l]);
    } finally {
      setUploading(false);
      e.target.value = '';
    }
  }

  return (
    <main className="mx-auto min-h-screen max-w-lg bg-gray-50 pb-24">
      {/* 头部 */}
      <header className="sticky top-0 z-30 bg-emerald-800 px-4 py-4 text-white shadow-lg">
        <h1 className="text-lg font-black">🤖 智能体指挥台</h1>
        <p className="text-xs text-emerald-200">
          手机指挥 Hermes / Cindy 接管系统 · 发命令 · 传视频 · 上架商品
        </p>
      </header>

      {/* 命令 + 传货 */}
      <div className="sticky top-[70px] z-20 bg-gray-50 px-3 pb-2 pt-3">
        <div className="rounded-3xl bg-white p-3 shadow-md">
          <div className="flex gap-2">
            <input
              value={cmd}
              onChange={(e) => setCmd(e.target.value)}
              onKeyDown={(e) => e.key === 'Enter' && sendCommand()}
              placeholder='发指令: 如 "上架 丹东草莓 19.9元 30件"'
              className="flex-1 rounded-2xl border border-gray-200 px-3 py-3 text-[15px] outline-none focus:border-emerald-400"
            />
            <button
              onClick={sendCommand}
              disabled={busy || !cmd.trim()}
              className="shrink-0 rounded-2xl bg-emerald-600 px-5 py-3 font-black text-white active:scale-95 disabled:opacity-40"
            >
              {busy ? '…' : '执行'}
            </button>
          </div>

          <label className="mt-2 flex cursor-pointer items-center justify-between rounded-2xl border-2 border-dashed border-emerald-300 bg-emerald-50 px-4 py-3 active:scale-[0.99]">
            <span className="flex items-center gap-2 text-[14px] font-bold text-emerald-700">
              {uploading ? '⏳ 正在传货上架...' : '🎥 手机实拍视频/图片 → 一键上架'}
            </span>
            <span className="rounded-full bg-emerald-600 px-4 py-1.5 text-xs font-black text-white">
              选择文件
            </span>
            <input
              type="file"
              accept="video/*,image/*"
              onChange={uploadVideo}
              disabled={uploading}
              className="hidden"
            />
          </label>

          <div className="mt-2 flex flex-wrap gap-1.5">
            {['上架 土豆 2.99元 50件', '补货', '降价', '结算', '退款', '视频号推广'].map((s) => (
              <button
                key={s}
                onClick={() => setCmd(s)}
                className="rounded-full bg-gray-100 px-3 py-1 text-[11px] text-gray-600 hover:bg-emerald-100"
              >
                {s}
              </button>
            ))}
          </div>
        </div>
      </div>

      {/* 执行日志 */}
      <section className="px-3 pt-2">
        <h2 className="mb-2 text-sm font-extrabold text-gray-500">执行日志</h2>
        <div className="space-y-2">
          {log.length === 0 && (
            <p className="py-6 text-center text-sm text-gray-400">
              发一条命令或传一段实拍视频试试
            </p>
          )}
          {log.map((r, i) => (
            <div key={i} className="rounded-2xl bg-white p-3 shadow-sm">
              <div className="mb-1 flex items-center justify-between">
                <span className="text-[13px] font-bold text-gray-700">{r.input}</span>
                {r.status && (
                  <span
                    className={`rounded-full px-2 py-0.5 text-[10px] font-black ${
                      r.status === 'created' || r.status === 'executed'
                        ? 'bg-emerald-100 text-emerald-700'
                        : r.status === 'unrouted'
                          ? 'bg-amber-100 text-amber-700'
                          : 'bg-gray-100 text-gray-600'
                    }`}
                  >
                    {r.status}
                  </span>
                )}
              </div>
              {r.error && <p className="text-[12px] font-bold text-red-500">❌ {r.error}</p>}
              {r.matched_keyword && (
                <p className="text-[11px] text-gray-400">
                  命中「{r.matched_keyword}」→ 工具 {r.tool}
                </p>
              )}
              {(r.result || r.hint) && (
                <pre className="mt-1 max-h-32 overflow-auto rounded-xl bg-gray-50 p-2 text-[11px] text-gray-600">
                  {JSON.stringify(r.result ?? { hint: r.hint }, null, 1)}
                </pre>
              )}
            </div>
          ))}
        </div>
      </section>

      {/* 工具面板 */}
      <section className="px-3 pt-4">
        <h2 className="mb-2 text-sm font-extrabold text-gray-500">
          🧰 可用工具 ({tools.length}) — Hermes/Cindy 接管的同款协议
        </h2>
        <div className="space-y-3">
          {toolGroups.map(([domain, list]) => (
            <div key={domain} className="rounded-2xl bg-white p-3 shadow-sm">
              <h3 className="mb-1.5 text-[13px] font-black text-emerald-700">{domain}</h3>
              <div className="flex flex-wrap gap-1.5">
                {list.map((t) => (
                  <span
                    key={t.tool}
                    className="rounded-lg bg-gray-50 px-2 py-1 font-mono text-[10px] text-gray-600"
                    title={t.require_auth ? '需管理员权限' : '公开'}
                  >
                    {t.name}
                    {t.require_auth ? ' 🔒' : ''}
                  </span>
                ))}
              </div>
            </div>
          ))}
        </div>
      </section>
    </main>
  );
}

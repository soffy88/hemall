'use client';

import { useEffect, useState } from 'react';
import { useRouter } from 'next/navigation';
import { bindTotp, clearPendingSetup, fetchTotpSetup, getPendingSetup, verifyTotp } from '@/lib/auth-store';
import { errMessage } from '@/lib/errors';
import type { TotpBindConfirm, TotpSetupChallenge, TotpSetupInfo } from '@/types/api';

/** 生产环境特权账号首次登录的 TOTP 绑定页（setup_token 只在登录页内存里，
 *  硬刷新即失效 → 回 /login 重新登录再取）。 */
export default function TotpSetupPage() {
  const router = useRouter();
  const [challenge, setChallenge] = useState<TotpSetupChallenge | null>(null);
  const [info, setInfo] = useState<TotpSetupInfo | null>(null);
  const [code, setCode] = useState('');
  const [confirm, setConfirm] = useState<TotpBindConfirm | null>(null);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const [ready, setReady] = useState(false);

  useEffect(() => {
    const pending = getPendingSetup();
    if (!pending || !pending.setup_token) {
      router.replace('/login');
      return;
    }
    fetchTotpSetup(pending.setup_token)
      .then((info) => {
        setChallenge(pending);
        setInfo(info);
      })
      .catch((err) => {
        setChallenge(pending);
        setError(errMessage(err) || '获取绑定信息失败');
      })
      .finally(() => setReady(true));
  }, [router]);

  async function handleBind(e: React.FormEvent) {
    e.preventDefault();
    if (!challenge?.setup_token || !info) return;
    setError('');
    setLoading(true);
    try {
      const result = await bindTotp(challenge.setup_token, info.secret, code.trim());
      setConfirm(result);
    } catch (err) {
      setError(errMessage(err) || '绑定校验失败');
    } finally {
      setLoading(false);
    }
  }

  async function handleFinish() {
    if (!challenge) return;
    setError('');
    setLoading(true);
    try {
      // 绑定用的同一个验证码再走一次登录第二步（无 challenge 令牌 → CSRF 双提交）。
      await verifyTotp(challenge.user_id, { code: code.trim() });
      clearPendingSetup();
      router.push('/admin');
    } catch (err) {
      setError(errMessage(err) || '验证码校验失败');
    } finally {
      setLoading(false);
    }
  }

  if (!ready) {
    return (
      <div className="flex min-h-screen items-center justify-center bg-gray-50 text-sm text-gray-500">
        加载中...
      </div>
    );
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-md space-y-6 p-8 bg-white rounded-xl shadow-sm border border-gray-200">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-gray-900">绑定两步验证</h1>
          <p className="mt-2 text-sm text-gray-500">
            {challenge?.message ?? '请先绑定 TOTP 验证器再进入后台'}
          </p>
        </div>

        {!confirm ? (
          info ? (
            <form onSubmit={handleBind} className="space-y-4">
              <div className="space-y-2 text-sm">
                <p className="text-gray-700">
                  用验证器 App（Google Authenticator / 1Password 等）扫描，或手动输入密钥：
                </p>
                <p className="font-mono break-all bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 select-all">
                  {info.secret}
                </p>
                <p className="break-all text-gray-500">
                  otpauth: <span className="font-mono text-xs">{info.otpauth_uri}</span>
                </p>
              </div>
              <div>
                <label className="block text-sm font-medium text-gray-700 mb-1">
                  输入验证器上的 6 位验证码
                </label>
                <input
                  type="text"
                  inputMode="numeric"
                  value={code}
                  onChange={(e) => setCode(e.target.value)}
                  autoComplete="one-time-code"
                  className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                  required
                />
              </div>
              {error && <p className="text-sm text-red-600">{error}</p>}
              <button
                type="submit"
                disabled={loading}
                className="w-full py-2 px-4 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition font-medium text-sm"
              >
                {loading ? '绑定中...' : '绑定并验证'}
              </button>
            </form>
          ) : (
            <div className="space-y-3 text-sm">
              {error && <p className="text-red-600">{error}</p>}
              <button
                type="button"
                onClick={() => router.push('/login')}
                className="w-full py-2 px-4 border border-gray-300 rounded-lg text-gray-700 hover:bg-gray-50 text-sm"
              >
                返回登录
              </button>
            </div>
          )
        ) : (
          <div className="space-y-4 text-sm">
            <p className="text-green-700 font-medium">{confirm.message}</p>
            <div>
              <p className="text-gray-700 mb-2">
                请立即保存以下恢复码（仅此一次可见，丢失将无法登录）：
              </p>
              <ul className="font-mono bg-gray-50 border border-gray-200 rounded-lg px-3 py-2 space-y-1">
                {confirm.recovery_codes.map((c) => (
                  <li key={c}>{c}</li>
                ))}
              </ul>
            </div>
            {error && <p className="text-red-600">{error}</p>}
            <button
              type="button"
              onClick={handleFinish}
              disabled={loading}
              className="w-full py-2 px-4 bg-blue-600 text-white rounded-lg hover:bg-blue-700 disabled:opacity-50 transition font-medium text-sm"
            >
              {loading ? '进入中...' : '我已保存，进入后台'}
            </button>
          </div>
        )}
      </div>
    </div>
  );
}

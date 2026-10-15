'use client';

import { useState } from 'react';
import { useRouter } from 'next/navigation';
import { login, verifyTotp } from '@/lib/auth-store';
import { errMessage } from '@/lib/errors';
import type { TotpLoginChallenge } from '@/types/api';

export default function LoginPage() {
  const router = useRouter();
  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [challenge, setChallenge] = useState<TotpLoginChallenge | null>(null);
  const [code, setCode] = useState('');
  const [useRecovery, setUseRecovery] = useState(false);
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    setLoading(true);
    try {
      const outcome = await login(email, password);
      if (outcome.kind === 'totp') {
        setChallenge(outcome.challenge);
        return;
      }
      if (outcome.kind === 'setup') {
        router.push('/login/totp-setup');
        return;
      }
      router.push('/admin');
    } catch (err) {
      setError(errMessage(err) || '登录失败');
    } finally {
      setLoading(false);
    }
  }

  async function handleVerify(e: React.FormEvent) {
    e.preventDefault();
    if (!challenge) return;
    setError('');
    setLoading(true);
    try {
      await verifyTotp(
        challenge.user_id,
        useRecovery ? { recovery_code: code.trim() } : { code: code.trim() },
        challenge.totp_token,
      );
      router.push('/admin');
    } catch (err) {
      setError(errMessage(err) || '验证码校验失败');
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="flex min-h-screen items-center justify-center bg-gray-50">
      <div className="w-full max-w-sm space-y-6 p-8 bg-white rounded-xl shadow-sm border border-gray-200">
        <div className="text-center">
          <h1 className="text-2xl font-bold text-gray-900">商家后台登录</h1>
          <p className="mt-2 text-sm text-gray-500">
            {challenge ? '输入动态验证码完成登录' : '使用管理员账号登录后台管理'}
          </p>
        </div>

        {!challenge ? (
          <form onSubmit={handleSubmit} className="space-y-4">
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">邮箱</label>
              <input
                type="email"
                value={email}
                onChange={(e) => setEmail(e.target.value)}
                className="w-full px-3 py-2 border border-gray-300 rounded-lg focus:ring-2 focus:ring-blue-500 focus:border-blue-500 text-sm"
                required
              />
            </div>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">密码</label>
              <input
                type="password"
                value={password}
                onChange={(e) => setPassword(e.target.value)}
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
              {loading ? '登录中...' : '登录'}
            </button>
          </form>
        ) : (
          <form onSubmit={handleVerify} className="space-y-4">
            <p className="text-sm text-gray-600">{challenge.message}</p>
            <div>
              <label className="block text-sm font-medium text-gray-700 mb-1">
                {useRecovery ? '恢复码' : '动态验证码'}
              </label>
              <input
                type="text"
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
              {loading ? '校验中...' : '验证并登录'}
            </button>
            <div className="flex items-center justify-between text-sm">
              <button
                type="button"
                onClick={() => {
                  setUseRecovery((v) => !v);
                  setCode('');
                  setError('');
                }}
                className="text-blue-600 hover:text-blue-700"
              >
                {useRecovery ? '改用动态验证码' : '改用恢复码'}
              </button>
              <button
                type="button"
                onClick={() => {
                  setChallenge(null);
                  setCode('');
                  setError('');
                }}
                className="text-gray-500 hover:text-gray-700"
              >
                返回
              </button>
            </div>
          </form>
        )}
      </div>
    </div>
  );
}

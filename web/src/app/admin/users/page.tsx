'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import type { AppUser } from '@/types/api';

export default function UsersPage() {
  const [users, setUsers] = useState<AppUser[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [editingId, setEditingId] = useState<string | null>(null);

  const [email, setEmail] = useState('');
  const [password, setPassword] = useState('');
  const [name, setName] = useState('');
  const [userStatus, setUserStatus] = useState('active');

  async function load() {
    setLoading(true);
    try {
      const data = await api.adminListUsers();
      setUsers(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  function openCreate() {
    setEditingId(null);
    setEmail(''); setPassword(''); setName(''); setUserStatus('active');
    setShowForm(true);
  }

  function openEdit(u: AppUser) {
    setEditingId(u.id);
    setEmail(u.email); setPassword(''); setName(u.name || ''); setUserStatus(u.status);
    setShowForm(true);
  }

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    try {
      if (editingId) {
        const res = await api.updateUser({
          user_id: editingId,
          email,
          name,
          status: userStatus,
          password: password || undefined,
        });
        if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      } else {
        const res = await api.createUser({ email, password, name });
        if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      }
      setShowForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleResetPassword(u: AppUser) {
    if (!confirm(`向 ${u.email} 发送密码重置通知？`)) return;
    try {
      const res = await api.resetUserPassword({ user_id: u.id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '重置失败');
      alert('重置通知已发出');
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleStatus(u: AppUser) {
    try {
      const newStatus = u.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateUser({ user_id: u.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">管理员账号</h1>
        <button onClick={showForm && !editingId ? () => setShowForm(false) : openCreate} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
          {showForm && !editingId ? '取消' : '新建账号'}
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleSubmit} className="mb-6 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <h2 className="font-semibold">{editingId ? '编辑账号' : '新建账号'}</h2>
          <div className="grid grid-cols-3 gap-3">
            <input placeholder="邮箱" type="email" value={email} onChange={(e) => setEmail(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder={editingId ? '新密码（留空不改）' : '密码'} type="password" value={password} onChange={(e) => setPassword(e.target.value)} required={!editingId} className="px-3 py-2 border rounded-lg text-sm" />
            <input placeholder="姓名" value={name} onChange={(e) => setName(e.target.value)} className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          {editingId && (
            <select value={userStatus} onChange={(e) => setUserStatus(e.target.value)} className="px-3 py-2 border rounded-lg text-sm">
              <option value="active">active</option>
              <option value="inactive">inactive</option>
            </select>
          )}
          <div className="flex gap-2">
            <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">
              {editingId ? '保存' : '创建'}
            </button>
            <button type="button" onClick={() => setShowForm(false)} className="px-4 py-2 bg-gray-100 rounded-lg text-sm hover:bg-gray-200">取消</button>
          </div>
        </form>
      )}

      {error && <div className="mb-4 text-red-600">{error}</div>}

      {loading ? (
        <div className="text-gray-500">加载中...</div>
      ) : (
        <div className="overflow-x-auto">
          <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
            <thead className="bg-gray-50 text-left">
              <tr>
                <th className="px-4 py-3 font-medium text-gray-600">邮箱</th>
                <th className="px-4 py-3 font-medium text-gray-600">姓名</th>
                <th className="px-4 py-3 font-medium text-gray-600">状态</th>
                <th className="px-4 py-3 font-medium text-gray-600">创建时间</th>
                <th className="px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {users.map((u) => (
                <tr key={u.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium">{u.email}</td>
                  <td className="px-4 py-3">{u.name || '—'}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${u.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{u.status}</span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500">{new Date(u.created_at).toLocaleString('zh-CN')}</td>
                  <td className="px-4 py-3">
                    <div className="flex gap-2">
                      <button onClick={() => openEdit(u)} className="text-xs text-blue-600 hover:underline">编辑</button>
                      <button onClick={() => handleResetPassword(u)} className="text-xs text-amber-600 hover:underline">重置密码</button>
                      <button onClick={() => handleToggleStatus(u)} className="text-xs text-blue-600 hover:underline">
                        {u.status === 'active' ? '停用' : '启用'}
                      </button>
                    </div>
                  </td>
                </tr>
              ))}
              {users.length === 0 && (
                <tr><td colSpan={5} className="px-4 py-8 text-center text-gray-400">暂无账号</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

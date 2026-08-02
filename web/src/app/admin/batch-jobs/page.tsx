'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import type { BatchJob } from '@/types/api';

export default function BatchJobsPage() {
  const [jobs, setJobs] = useState<BatchJob[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');
  const [showForm, setShowForm] = useState(false);
  const [jobType, setJobType] = useState('');
  const [payloadText, setPayloadText] = useState('{}');

  async function load() {
    setLoading(true);
    try {
      const data = await api.adminListBatchJobs();
      setJobs(data);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleCreate(e: React.FormEvent) {
    e.preventDefault();
    let payload: Record<string, unknown> | undefined;
    try {
      payload = payloadText.trim() ? JSON.parse(payloadText) : undefined;
    } catch {
      alert('payload 不是合法 JSON');
      return;
    }
    try {
      const res = await api.createBatchJob({ job_type: jobType, payload });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setJobType(''); setPayloadText('{}');
      setShowForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCancel(id: string) {
    if (!confirm('确认取消此任务？')) return;
    try {
      const res = await api.cancelBatchJob({ batch_job_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '取消失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <div className="flex items-center justify-between mb-6">
        <h1 className="text-2xl font-bold">批处理任务</h1>
        <button onClick={() => setShowForm(!showForm)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
          {showForm ? '取消' : '新建任务'}
        </button>
      </div>

      {showForm && (
        <form onSubmit={handleCreate} className="mb-6 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <div>
            <label className="text-xs text-gray-500">任务类型</label>
            <input value={jobType} onChange={(e) => setJobType(e.target.value)} placeholder="export_orders" required className="w-full px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">参数（JSON，可选）</label>
            <textarea value={payloadText} onChange={(e) => setPayloadText(e.target.value)} rows={3} className="w-full px-3 py-2 border rounded-lg text-sm font-mono" />
          </div>
          <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">创建</button>
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
                <th className="px-4 py-3 font-medium text-gray-600">类型</th>
                <th className="px-4 py-3 font-medium text-gray-600">状态</th>
                <th className="px-4 py-3 font-medium text-gray-600">参数</th>
                <th className="px-4 py-3 font-medium text-gray-600">结果</th>
                <th className="px-4 py-3 font-medium text-gray-600">时间</th>
                <th className="px-4 py-3 font-medium text-gray-600">操作</th>
              </tr>
            </thead>
            <tbody className="divide-y divide-gray-100">
              {jobs.map((j) => (
                <tr key={j.id} className="hover:bg-gray-50">
                  <td className="px-4 py-3 font-medium">{j.job_type}</td>
                  <td className="px-4 py-3">
                    <span className={`px-2 py-0.5 rounded text-xs ${
                      j.status === 'completed' ? 'bg-green-100 text-green-700' :
                      j.status === 'failed' ? 'bg-red-100 text-red-700' :
                      j.status === 'canceled' ? 'bg-gray-100 text-gray-600' :
                      'bg-amber-100 text-amber-700'
                    }`}>
                      {j.status}
                    </span>
                  </td>
                  <td className="px-4 py-3 text-xs text-gray-500 font-mono max-w-xs truncate">{j.payload ? JSON.stringify(j.payload) : '—'}</td>
                  <td className="px-4 py-3 text-xs text-gray-500 font-mono max-w-xs truncate">{j.result ? JSON.stringify(j.result) : '—'}</td>
                  <td className="px-4 py-3 text-xs text-gray-500">{new Date(j.created_at).toLocaleString('zh-CN')}</td>
                  <td className="px-4 py-3">
                    {(j.status === 'created' || j.status === 'running') && (
                      <button onClick={() => handleCancel(j.id)} className="text-xs text-red-600 hover:underline">取消</button>
                    )}
                  </td>
                </tr>
              ))}
              {jobs.length === 0 && (
                <tr><td colSpan={6} className="px-4 py-8 text-center text-gray-400">暂无任务</td></tr>
              )}
            </tbody>
          </table>
        </div>
      )}
    </div>
  );
}

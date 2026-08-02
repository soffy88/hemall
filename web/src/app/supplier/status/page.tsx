'use client';

import { useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import type { SupplierStatus } from '@/types/api';

function statusCopy(status: string): { label: string; className: string } {
  if (status === 'sandbox') return { label: '沙盒观察期', className: 'bg-amber-100 text-amber-700' };
  if (status === 'active') return { label: '正常', className: 'bg-emerald-100 text-emerald-700' };
  if (status === 'slashed') return { label: '已冻结（信誉分过低）', className: 'bg-red-100 text-red-700' };
  return { label: status, className: 'bg-gray-100 text-gray-600' };
}

export default function SupplierStatusPage() {
  const [walletAccount, setWalletAccount] = useState('');
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState('');
  const [supplier, setSupplier] = useState<SupplierStatus | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    if (!walletAccount.trim()) return;
    setLoading(true);
    setError('');
    setSupplier(null);
    try {
      const res = await api.supplierLookup(walletAccount.trim());
      setSupplier(res);
    } catch (e: any) {
      setError(e.message === 'supplier not found' ? '未找到该钱包账号对应的供应商，请确认输入是否正确。' : (e.message || '查询失败'));
    } finally {
      setLoading(false);
    }
  }

  return (
    <div className="space-y-5">
      <div className="bg-white rounded-xl border border-gray-200 p-6">
        <h1 className="text-xl font-bold mb-1">查询我的状态</h1>
        <p className="text-sm text-gray-500 mb-4">输入入驻时填写的钱包账号。</p>
        <form onSubmit={handleSubmit} className="flex gap-2">
          <input
            value={walletAccount}
            onChange={(e) => setWalletAccount(e.target.value)}
            placeholder="钱包账号"
            className="flex-1 px-3 py-2 border border-gray-300 rounded-lg text-sm"
          />
          <button
            type="submit"
            disabled={loading}
            className="px-4 py-2 bg-amber-600 text-white rounded-lg hover:bg-amber-700 disabled:opacity-50 text-sm font-medium"
          >
            {loading ? '查询中...' : '查询'}
          </button>
        </form>
        {error && <p className="mt-3 text-sm text-red-600">{error}</p>}
      </div>

      {supplier && (
        <div className="bg-white rounded-xl border border-gray-200 p-6 space-y-3">
          <div className="flex items-center justify-between">
            <div className="font-semibold">{supplier.polygon_name || '（未命名产地）'}</div>
            <span className={`text-xs px-2 py-0.5 rounded ${statusCopy(supplier.status).className}`}>
              {statusCopy(supplier.status).label}
            </span>
          </div>
          <div className="grid grid-cols-2 gap-3 text-sm">
            <div className="bg-gray-50 rounded-lg p-3">
              <div className="text-xs text-gray-400 mb-1">信誉分</div>
              <div className={`text-lg font-bold ${supplier.trust_score < 60 ? 'text-red-600' : 'text-emerald-600'}`}>
                {supplier.trust_score}
              </div>
            </div>
            <div className="bg-gray-50 rounded-lg p-3">
              <div className="text-xs text-gray-400 mb-1">质押余额</div>
              <div className="text-lg font-bold text-gray-800">{formatMoney(supplier.escrow_balance)}</div>
            </div>
          </div>
          <div className="text-xs text-gray-400">
            入驻时间：{new Date(supplier.created_at).toLocaleString('zh-CN')}
          </div>
          {supplier.trust_score < 60 && (
            <div className="text-xs text-red-600 bg-red-50 rounded-lg p-2">
              信誉分低于 60，账号可能已被冻结（例如因批次货品异常被处罚）。
            </div>
          )}
        </div>
      )}
    </div>
  );
}

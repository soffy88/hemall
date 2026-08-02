'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import type { AdminRegion, TaxRate } from '@/types/api';

export default function SettingsPage() {
  const [regions, setRegions] = useState<AdminRegion[]>([]);
  const [taxRates, setTaxRates] = useState<TaxRate[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [showRegionForm, setShowRegionForm] = useState(false);
  const [regionCode, setRegionCode] = useState('');
  const [regionName, setRegionName] = useState('');
  const [regionCurrency, setRegionCurrency] = useState('CNY');
  const [regionProviders, setRegionProviders] = useState('manual');

  const [showTaxForm, setShowTaxForm] = useState(false);
  const [taxRegion, setTaxRegion] = useState('');
  const [taxName, setTaxName] = useState('');
  const [taxRate, setTaxRate] = useState(0);

  async function load() {
    setLoading(true);
    try {
      const [r, t] = await Promise.all([api.adminListRegions(), api.adminListTaxRates()]);
      setRegions(r);
      setTaxRates(t);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleCreateRegion(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createRegion({
        code: regionCode,
        name: regionName,
        currency: regionCurrency,
        payment_provider_names: regionProviders.split(',').map((s) => s.trim()).filter(Boolean),
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setRegionCode(''); setRegionName(''); setShowRegionForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleRegion(r: AdminRegion) {
    try {
      const newStatus = r.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateRegion({ code: r.code, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteRegion(code: string) {
    if (!confirm('确认删除此区域？')) return;
    try {
      const res = await api.deleteRegion({ code });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateTaxRate(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createTaxRate({ region_code: taxRegion, name: taxName, rate_percent: taxRate });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setTaxName(''); setTaxRate(0); setShowTaxForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleTaxRate(t: TaxRate) {
    try {
      const newStatus = t.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateTaxRate({ tax_rate_id: t.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteTaxRate(id: string) {
    if (!confirm('确认删除此税率？')) return;
    try {
      const res = await api.deleteTaxRate({ tax_rate_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">区域与税率设置</h1>
      {error && <div className="mb-4 text-red-600">{error}</div>}
      {loading && <div className="text-gray-500 mb-4">加载中...</div>}

      {/* Regions */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">区域</h2>
        <button onClick={() => setShowRegionForm(!showRegionForm)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
          {showRegionForm ? '取消' : '新建区域'}
        </button>
      </div>
      {showRegionForm && (
        <form onSubmit={handleCreateRegion} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 space-y-3">
          <div className="grid grid-cols-4 gap-3">
            <div>
              <label className="text-xs text-gray-500">代码</label>
              <input value={regionCode} onChange={(e) => setRegionCode(e.target.value)} placeholder="cn-north" required className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">名称</label>
              <input value={regionName} onChange={(e) => setRegionName(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">币种</label>
              <input value={regionCurrency} onChange={(e) => setRegionCurrency(e.target.value)} required className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
            <div>
              <label className="text-xs text-gray-500">支付方式（逗号分隔）</label>
              <input value={regionProviders} onChange={(e) => setRegionProviders(e.target.value)} className="w-full px-3 py-2 border rounded-lg text-sm" />
            </div>
          </div>
          <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">创建</button>
        </form>
      )}
      <div className="overflow-x-auto mb-8">
        <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
          <thead className="bg-gray-50 text-left">
            <tr>
              <th className="px-4 py-2 font-medium text-gray-600">代码</th>
              <th className="px-4 py-2 font-medium text-gray-600">名称</th>
              <th className="px-4 py-2 font-medium text-gray-600">币种</th>
              <th className="px-4 py-2 font-medium text-gray-600">支付方式</th>
              <th className="px-4 py-2 font-medium text-gray-600">状态</th>
              <th className="px-4 py-2 font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {regions.map((r) => (
              <tr key={r.code}>
                <td className="px-4 py-2 font-mono text-xs">{r.code}</td>
                <td className="px-4 py-2 font-medium">{r.name}</td>
                <td className="px-4 py-2">{r.currency}</td>
                <td className="px-4 py-2 text-xs text-gray-500">{r.payment_provider_names.join(', ') || '—'}</td>
                <td className="px-4 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${r.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{r.status}</span>
                </td>
                <td className="px-4 py-2">
                  <div className="flex gap-2">
                    <button onClick={() => handleToggleRegion(r)} className="text-xs text-blue-600 hover:underline">
                      {r.status === 'active' ? '停用' : '启用'}
                    </button>
                    <button onClick={() => handleDeleteRegion(r.code)} className="text-xs text-red-600 hover:underline">删除</button>
                  </div>
                </td>
              </tr>
            ))}
            {regions.length === 0 && <tr><td colSpan={6} className="px-4 py-6 text-center text-gray-400">暂无区域</td></tr>}
          </tbody>
        </table>
      </div>

      {/* Tax Rates */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">税率</h2>
        <button onClick={() => setShowTaxForm(!showTaxForm)} className="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm">
          {showTaxForm ? '取消' : '新建税率'}
        </button>
      </div>
      {showTaxForm && (
        <form onSubmit={handleCreateTaxRate} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 flex gap-3 items-end flex-wrap">
          <div>
            <label className="text-xs text-gray-500">区域</label>
            <select value={taxRegion} onChange={(e) => setTaxRegion(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm">
              <option value="">选择区域...</option>
              {regions.map((r) => <option key={r.code} value={r.code}>{r.name} ({r.code})</option>)}
            </select>
          </div>
          <div>
            <label className="text-xs text-gray-500">名称</label>
            <input value={taxName} onChange={(e) => setTaxName(e.target.value)} placeholder="增值税" required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">税率（%）</label>
            <input type="number" step="0.01" value={taxRate} onChange={(e) => setTaxRate(+e.target.value)} className="px-3 py-2 border rounded-lg text-sm w-24" />
          </div>
          <button type="submit" className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700">创建</button>
        </form>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
          <thead className="bg-gray-50 text-left">
            <tr>
              <th className="px-4 py-2 font-medium text-gray-600">区域</th>
              <th className="px-4 py-2 font-medium text-gray-600">名称</th>
              <th className="px-4 py-2 font-medium text-gray-600">税率</th>
              <th className="px-4 py-2 font-medium text-gray-600">状态</th>
              <th className="px-4 py-2 font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {taxRates.map((t) => (
              <tr key={t.id}>
                <td className="px-4 py-2 font-mono text-xs">{t.region_code}</td>
                <td className="px-4 py-2 font-medium">{t.name}</td>
                <td className="px-4 py-2">{t.rate_percent}%</td>
                <td className="px-4 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${t.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{t.status}</span>
                </td>
                <td className="px-4 py-2">
                  <div className="flex gap-2">
                    <button onClick={() => handleToggleTaxRate(t)} className="text-xs text-blue-600 hover:underline">
                      {t.status === 'active' ? '停用' : '启用'}
                    </button>
                    <button onClick={() => handleDeleteTaxRate(t.id)} className="text-xs text-red-600 hover:underline">删除</button>
                  </div>
                </td>
              </tr>
            ))}
            {taxRates.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-400">暂无税率</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

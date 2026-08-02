'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import { formatMoney } from '@/lib/format';
import { ActionCard, OpsSection } from '@/components/ops/OpsActionCard';
import type { StockLocation, SalesChannel, PriceList } from '@/types/api';

export default function InventorySettingsPage() {
  // hemall 扩展运维（原独立运维页面搬迁过来的裸操作卡片）用的独立 ctx——
  // 跟上面门店/渠道/价格表管理是两套不相干的状态，不复用。
  const [opsCtx, setOpsCtx] = useState<Record<string, string>>({});
  function mergeOpsCtx(patch: Record<string, unknown>) {
    const next: Record<string, string> = {};
    for (const [k, v] of Object.entries(patch)) {
      if (typeof v === 'string' || typeof v === 'number') next[k] = String(v);
    }
    setOpsCtx((c) => ({ ...c, ...next }));
  }

  const [locations, setLocations] = useState<StockLocation[]>([]);
  const [channels, setChannels] = useState<SalesChannel[]>([]);
  const [priceLists, setPriceLists] = useState<PriceList[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [showLocForm, setShowLocForm] = useState(false);
  const [locName, setLocName] = useState('');
  const [locRegion, setLocRegion] = useState('');
  const [locChannelTags, setLocChannelTags] = useState('');

  const [showChannelForm, setShowChannelForm] = useState(false);
  const [channelName, setChannelName] = useState('');

  const [showPlForm, setShowPlForm] = useState(false);
  const [plName, setPlName] = useState('');
  const [plCurrency, setPlCurrency] = useState('CNY');

  async function load() {
    setLoading(true);
    try {
      const [l, c, p] = await Promise.all([
        api.adminListStockLocations(),
        api.adminListSalesChannels(),
        api.adminListPriceLists(),
      ]);
      setLocations(l);
      setChannels(c);
      setPriceLists(p);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  // ── Stock locations ──────────────────────────────────────────

  async function handleCreateLocation(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createStockLocation({
        name: locName,
        region_code: locRegion,
        channel_tags: locChannelTags ? locChannelTags.split(',').map((s) => s.trim()).filter(Boolean) : undefined,
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setLocName(''); setLocRegion(''); setLocChannelTags(''); setShowLocForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleLocation(l: StockLocation) {
    try {
      const newStatus = l.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateStockLocation({ location_id: l.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleEditChannelTags(l: StockLocation) {
    const text = prompt('渠道标签（逗号分隔）：', (l.channel_tags || []).join(', '));
    if (text == null) return;
    try {
      const res = await api.updateStockLocation({
        location_id: l.id,
        channel_tags: text.split(',').map((s) => s.trim()).filter(Boolean),
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteLocation(id: string) {
    if (!confirm('确认删除此仓位？')) return;
    try {
      const res = await api.deleteStockLocation({ location_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  // ── Sales channels ───────────────────────────────────────────

  async function handleCreateChannel(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createSalesChannel({ name: channelName });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setChannelName(''); setShowChannelForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleChannel(c: SalesChannel) {
    try {
      const newStatus = c.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateSalesChannel({ channel_id: c.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteChannel(id: string) {
    if (!confirm('确认删除此销售渠道？')) return;
    try {
      const res = await api.deleteSalesChannel({ channel_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handlePublishProducts(c: SalesChannel) {
    const idsText = prompt('要发布的商品 ID（逗号分隔）：');
    if (!idsText) return;
    const product_ids = idsText.split(',').map((s) => s.trim()).filter(Boolean);
    try {
      const res = await api.publishProductsToChannel({ channel_id: c.id, product_ids });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '发布失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleUnpublishProducts(c: SalesChannel) {
    const idsText = prompt('要下架的商品 ID（逗号分隔）：');
    if (!idsText) return;
    const product_ids = idsText.split(',').map((s) => s.trim()).filter(Boolean);
    try {
      const res = await api.unpublishProductsFromChannel({ channel_id: c.id, product_ids });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '下架失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  // ── Price lists ───────────────────────────────────────────────

  async function handleCreatePriceList(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createPriceList({ name: plName, currency: plCurrency });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setPlName(''); setShowPlForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleTogglePriceList(pl: PriceList) {
    try {
      const newStatus = pl.status === 'active' ? 'inactive' : 'active';
      const res = await api.updatePriceList({ price_list_id: pl.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeletePriceList(id: string) {
    if (!confirm('确认删除此价格表？')) return;
    try {
      const res = await api.deletePriceList({ price_list_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleAddPrice(pl: PriceList) {
    const variantId = prompt('变体 ID：');
    if (!variantId) return;
    const priceStr = prompt('价格（分）：');
    if (!priceStr) return;
    try {
      const res = await api.addPricesToList({
        price_list_id: pl.id,
        items: [{ variant_id: variantId, price_cents: Number(priceStr) }],
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '添加失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleRemovePrice(pl: PriceList, variantId: string) {
    try {
      const res = await api.removePricesFromList({ price_list_id: pl.id, variant_ids: [variantId] });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '移除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">库存与渠道设置</h1>
      {error && <div className="mb-4 text-red-600">{error}</div>}
      {loading && <div className="text-gray-500 mb-4">加载中...</div>}

      {/* Stock Locations */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">仓位 / 门店</h2>
        <button onClick={() => setShowLocForm(!showLocForm)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
          {showLocForm ? '取消' : '新建仓位'}
        </button>
      </div>
      {showLocForm && (
        <form onSubmit={handleCreateLocation} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 flex gap-3 items-end">
          <div>
            <label className="text-xs text-gray-500">名称</label>
            <input value={locName} onChange={(e) => setLocName(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">区域代码</label>
            <input value={locRegion} onChange={(e) => setLocRegion(e.target.value)} placeholder="r-62b90d" required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">渠道标签（逗号分隔，可选）</label>
            <input value={locChannelTags} onChange={(e) => setLocChannelTags(e.target.value)} placeholder="online, offline" className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">创建</button>
        </form>
      )}
      <div className="overflow-x-auto mb-8">
        <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
          <thead className="bg-gray-50 text-left">
            <tr>
              <th className="px-4 py-2 font-medium text-gray-600">名称</th>
              <th className="px-4 py-2 font-medium text-gray-600">区域</th>
              <th className="px-4 py-2 font-medium text-gray-600">渠道标签</th>
              <th className="px-4 py-2 font-medium text-gray-600">状态</th>
              <th className="px-4 py-2 font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {locations.map((l) => (
              <tr key={l.id}>
                <td className="px-4 py-2 font-medium">{l.name}</td>
                <td className="px-4 py-2">{l.region_code}</td>
                <td className="px-4 py-2 text-xs text-gray-500">
                  {(l.channel_tags || []).join(', ') || '—'}
                  <button onClick={() => handleEditChannelTags(l)} className="ml-2 text-blue-600 hover:underline">编辑</button>
                </td>
                <td className="px-4 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${l.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{l.status}</span>
                </td>
                <td className="px-4 py-2">
                  <div className="flex gap-2">
                    <button onClick={() => handleToggleLocation(l)} className="text-xs text-blue-600 hover:underline">
                      {l.status === 'active' ? '停用' : '启用'}
                    </button>
                    <button onClick={() => handleDeleteLocation(l.id)} className="text-xs text-red-600 hover:underline">删除</button>
                  </div>
                </td>
              </tr>
            ))}
            {locations.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-400">暂无仓位</td></tr>}
          </tbody>
        </table>
      </div>

      {/* Sales Channels */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">销售渠道</h2>
        <button onClick={() => setShowChannelForm(!showChannelForm)} className="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm">
          {showChannelForm ? '取消' : '新建渠道'}
        </button>
      </div>
      {showChannelForm && (
        <form onSubmit={handleCreateChannel} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 flex gap-3 items-end">
          <div>
            <label className="text-xs text-gray-500">渠道名称</label>
            <input value={channelName} onChange={(e) => setChannelName(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <button type="submit" className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700">创建</button>
        </form>
      )}
      <div className="overflow-x-auto mb-8">
        <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
          <thead className="bg-gray-50 text-left">
            <tr>
              <th className="px-4 py-2 font-medium text-gray-600">名称</th>
              <th className="px-4 py-2 font-medium text-gray-600">已发布商品数</th>
              <th className="px-4 py-2 font-medium text-gray-600">状态</th>
              <th className="px-4 py-2 font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {channels.map((c) => (
              <tr key={c.id}>
                <td className="px-4 py-2 font-medium">{c.name}</td>
                <td className="px-4 py-2">{c.product_count}</td>
                <td className="px-4 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${c.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{c.status}</span>
                </td>
                <td className="px-4 py-2">
                  <div className="flex gap-2 flex-wrap">
                    <button onClick={() => handlePublishProducts(c)} className="text-xs text-blue-600 hover:underline">发布商品</button>
                    <button onClick={() => handleUnpublishProducts(c)} className="text-xs text-amber-600 hover:underline">下架商品</button>
                    <button onClick={() => handleToggleChannel(c)} className="text-xs text-blue-600 hover:underline">
                      {c.status === 'active' ? '停用' : '启用'}
                    </button>
                    <button onClick={() => handleDeleteChannel(c.id)} className="text-xs text-red-600 hover:underline">删除</button>
                  </div>
                </td>
              </tr>
            ))}
            {channels.length === 0 && <tr><td colSpan={4} className="px-4 py-6 text-center text-gray-400">暂无销售渠道</td></tr>}
          </tbody>
        </table>
      </div>

      {/* Price Lists */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">价格表</h2>
        <button onClick={() => setShowPlForm(!showPlForm)} className="px-4 py-2 bg-emerald-600 text-white rounded-lg hover:bg-emerald-700 text-sm">
          {showPlForm ? '取消' : '新建价格表'}
        </button>
      </div>
      {showPlForm && (
        <form onSubmit={handleCreatePriceList} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 flex gap-3 items-end">
          <div>
            <label className="text-xs text-gray-500">名称</label>
            <input value={plName} onChange={(e) => setPlName(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">币种</label>
            <input value={plCurrency} onChange={(e) => setPlCurrency(e.target.value)} className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <button type="submit" className="px-4 py-2 bg-emerald-600 text-white rounded-lg text-sm hover:bg-emerald-700">创建</button>
        </form>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
          <thead className="bg-gray-50 text-left">
            <tr>
              <th className="px-4 py-2 font-medium text-gray-600">名称</th>
              <th className="px-4 py-2 font-medium text-gray-600">币种</th>
              <th className="px-4 py-2 font-medium text-gray-600">条目</th>
              <th className="px-4 py-2 font-medium text-gray-600">状态</th>
              <th className="px-4 py-2 font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {priceLists.map((pl) => (
              <tr key={pl.id}>
                <td className="px-4 py-2 font-medium">{pl.name}</td>
                <td className="px-4 py-2">{pl.currency}</td>
                <td className="px-4 py-2">
                  <div className="text-xs space-y-0.5">
                    {pl.items.map((it) => (
                      <div key={it.id} className="flex items-center justify-between gap-2 text-gray-500">
                        <span>{it.sku_code || it.variant_id.slice(0, 8)}: {formatMoney(it.price_cents, pl.currency)}</span>
                        <button onClick={() => handleRemovePrice(pl, it.variant_id)} className="text-red-600 hover:underline shrink-0">移除</button>
                      </div>
                    ))}
                    <button onClick={() => handleAddPrice(pl)} className="text-blue-600 hover:underline">+ 添加价格</button>
                  </div>
                </td>
                <td className="px-4 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${pl.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{pl.status}</span>
                </td>
                <td className="px-4 py-2">
                  <div className="flex gap-2">
                    <button onClick={() => handleTogglePriceList(pl)} className="text-xs text-blue-600 hover:underline">
                      {pl.status === 'active' ? '停用' : '启用'}
                    </button>
                    <button onClick={() => handleDeletePriceList(pl.id)} className="text-xs text-red-600 hover:underline">删除</button>
                  </div>
                </td>
              </tr>
            ))}
            {priceLists.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-400">暂无价格表</td></tr>}
          </tbody>
        </table>
      </div>

      <div className="mt-10">
        <h2 className="text-lg font-semibold mb-3">hemall 扩展运维 — 新节点注册 / 自动补货</h2>
        <OpsSection title="门店与补货">
          <ActionCard
            title="新节点注册 commission_new_location"
            badge="admin"
            desc="新车库通电联网，自动注册基础数据并重绘 Voronoi 网格 (≥2 个 active 节点时)。"
            fields={[
              { key: 'host_id', label: 'host_id' },
              { key: 'address', label: 'address' },
              { key: 'lat', label: 'lat', type: 'number' },
              { key: 'lon', label: 'lon', type: 'number' },
            ]}
            defaults={opsCtx}
            onSubmit={(v) => api.hemallCommissionNewLocation(v as any)}
            onResult={(r) => mergeOpsCtx({ location_id: (r as any).location_id })}
          />
          <ActionCard
            title="自动补货 execute_ambient_replenishment"
            badge="public"
            desc="预测家庭存货见底日期，到期直接扣款并推单至最近微仓 (purchase_history 至少 2 条)。"
            fields={[
              { key: 'customer_ref', label: 'customer_ref' },
              { key: 'variant_id', label: 'variant_id' },
              { key: 'qty', label: 'qty', type: 'number' },
              { key: 'family_size', label: 'family_size', type: 'number' },
              {
                key: 'purchase_history',
                label: 'purchase_history (JSON 数组)',
                type: 'json',
                placeholder: '[{"purchased_at":"2026-06-01T00:00:00Z","quantity":1},{"purchased_at":"2026-07-01T00:00:00Z","quantity":1}]',
              },
              { key: 'customer_lat', label: 'customer_lat', type: 'number' },
              { key: 'customer_lon', label: 'customer_lon', type: 'number' },
            ]}
            defaults={opsCtx}
            onSubmit={(v) => api.hemallExecuteAmbientReplenishment(v as any)}
          />
        </OpsSection>
      </div>
    </div>
  );
}

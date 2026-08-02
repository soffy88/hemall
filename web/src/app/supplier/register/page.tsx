'use client';

import { useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';

/** 用中心点 + 半径生成一个近似圆形的 GeoJSON Polygon（8 边形），比让果农手填
 * 原始坐标数组好用得多——真实场景应该是地图上画一个圈/多边形，这里没有接
 * 地图组件，先用"中心点+半径"这个更友好的输入方式代替，服务端只认
 * GeoJSON Polygon，不关心前端是怎么算出来的。 */
function buildPolygon(lat: number, lng: number, radiusKm: number) {
  const points = 8;
  const latDegPerKm = 1 / 111.32;
  const lngDegPerKm = 1 / (111.32 * Math.cos((lat * Math.PI) / 180));
  const coordinates: number[][] = [];
  for (let i = 0; i <= points; i++) {
    const angle = (i / points) * 2 * Math.PI;
    coordinates.push([
      lng + radiusKm * lngDegPerKm * Math.cos(angle),
      lat + radiusKm * latDegPerKm * Math.sin(angle),
    ]);
  }
  return { type: 'Polygon', coordinates: [coordinates] };
}

export default function SupplierRegisterPage() {
  const [walletAccount, setWalletAccount] = useState('');
  const [polygonName, setPolygonName] = useState('');
  const [lat, setLat] = useState('');
  const [lng, setLng] = useState('');
  const [radiusKm, setRadiusKm] = useState('2');
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState('');
  const [result, setResult] = useState<{ supplier_id: string } | null>(null);

  async function handleSubmit(e: React.FormEvent) {
    e.preventDefault();
    setError('');
    const latNum = Number(lat);
    const lngNum = Number(lng);
    const radiusNum = Number(radiusKm);
    if (!walletAccount.trim()) { setError('请填写钱包账号'); return; }
    if (Number.isNaN(latNum) || latNum < -90 || latNum > 90) { setError('纬度需在 -90 到 90 之间'); return; }
    if (Number.isNaN(lngNum) || lngNum < -180 || lngNum > 180) { setError('经度需在 -180 到 180 之间'); return; }
    if (Number.isNaN(radiusNum) || radiusNum <= 0) { setError('半径需大于 0'); return; }

    setSubmitting(true);
    try {
      const res = await api.hemallClaimOrigin({
        wallet_account: walletAccount.trim(),
        spatial_polygon: buildPolygon(latNum, lngNum, radiusNum),
        polygon_name: polygonName.trim() || undefined,
      });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '入驻失败');
      setResult({ supplier_id: (res as any).supplier_id });
    } catch (e: any) {
      setError(e.message);
    } finally {
      setSubmitting(false);
    }
  }

  if (result) {
    return (
      <div className="bg-white rounded-xl border border-gray-200 p-6 text-center space-y-4">
        <div className="text-3xl">✅</div>
        <h1 className="text-xl font-bold">入驻成功</h1>
        <p className="text-sm text-gray-600">
          你的供应商账号已创建，初始信誉分 100，质押余额 ¥0.00，状态 sandbox（沙盒观察期）。
        </p>
        <div className="bg-gray-50 rounded-lg p-3 text-xs font-mono break-all">
          supplier_id: {result.supplier_id}
        </div>
        <p className="text-xs text-gray-400">
          请牢记你填写的钱包账号「{walletAccount}」——后续查询状态需要用它。
        </p>
        <Link href="/supplier/status" className="inline-block text-sm text-amber-700 hover:underline">
          去查询我的状态 →
        </Link>
      </div>
    );
  }

  return (
    <div className="bg-white rounded-xl border border-gray-200 p-6">
      <h1 className="text-xl font-bold mb-1">入驻登记</h1>
      <p className="text-sm text-gray-500 mb-5">
        录入原产地围栏（用中心点 + 半径圈定一片区域），无需人工审核，提交后立即生效。
      </p>
      <form onSubmit={handleSubmit} className="space-y-4">
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">钱包账号</label>
          <input
            value={walletAccount}
            onChange={(e) => setWalletAccount(e.target.value)}
            placeholder="用于结算货款，也是后续查询状态的唯一凭证"
            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
            required
          />
        </div>
        <div>
          <label className="block text-sm font-medium text-gray-700 mb-1">产地名称（可选）</label>
          <input
            value={polygonName}
            onChange={(e) => setPolygonName(e.target.value)}
            placeholder="如：云南哀牢山车厘子基地"
            className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
          />
        </div>
        <div className="grid grid-cols-3 gap-3">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">中心点纬度</label>
            <input
              value={lat}
              onChange={(e) => setLat(e.target.value)}
              placeholder="31.0"
              type="number"
              step="any"
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">中心点经度</label>
            <input
              value={lng}
              onChange={(e) => setLng(e.target.value)}
              placeholder="121.0"
              type="number"
              step="any"
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
              required
            />
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">半径 (公里)</label>
            <input
              value={radiusKm}
              onChange={(e) => setRadiusKm(e.target.value)}
              type="number"
              step="any"
              className="w-full px-3 py-2 border border-gray-300 rounded-lg text-sm"
              required
            />
          </div>
        </div>
        {error && <p className="text-sm text-red-600">{error}</p>}
        <button
          type="submit"
          disabled={submitting}
          className="w-full py-2 px-4 bg-amber-600 text-white rounded-lg hover:bg-amber-700 disabled:opacity-50 transition font-medium text-sm"
        >
          {submitting ? '提交中...' : '提交入驻'}
        </button>
      </form>
    </div>
  );
}

'use client';

import { useEffect, useState } from 'react';
import { api } from '@/lib/api-client';
import type { ProductCategory, ProductCollection } from '@/types/api';

export default function CatalogPage() {
  const [categories, setCategories] = useState<ProductCategory[]>([]);
  const [collections, setCollections] = useState<ProductCollection[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState('');

  const [showCatForm, setShowCatForm] = useState(false);
  const [catName, setCatName] = useState('');
  const [catSlug, setCatSlug] = useState('');
  const [catParent, setCatParent] = useState('');

  const [showColForm, setShowColForm] = useState(false);
  const [colName, setColName] = useState('');
  const [colSlug, setColSlug] = useState('');

  async function load() {
    setLoading(true);
    try {
      const [c, col] = await Promise.all([api.adminListCategories(), api.adminListCollections()]);
      setCategories(c);
      setCollections(col);
    } catch (e: any) {
      setError(e.message);
    } finally {
      setLoading(false);
    }
  }

  useEffect(() => { load(); }, []);

  async function handleCreateCategory(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createProductCategory({ name: catName, slug: catSlug, parent_id: catParent || null });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setCatName(''); setCatSlug(''); setCatParent(''); setShowCatForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleRenameCategory(c: ProductCategory) {
    const name = prompt('新名称：', c.name);
    if (!name || name === c.name) return;
    try {
      const res = await api.updateProductCategory({ category_id: c.id, name });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleCategory(c: ProductCategory) {
    try {
      const newStatus = c.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateProductCategory({ category_id: c.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteCategory(id: string) {
    if (!confirm('确认删除此分类？')) return;
    try {
      const res = await api.deleteProductCategory({ category_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleCreateCollection(e: React.FormEvent) {
    e.preventDefault();
    try {
      const res = await api.createProductCollection({ name: colName, slug: colSlug });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '创建失败');
      setColName(''); setColSlug(''); setShowColForm(false);
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleToggleCollection(c: ProductCollection) {
    try {
      const newStatus = c.status === 'active' ? 'inactive' : 'active';
      const res = await api.updateProductCollection({ collection_id: c.id, status: newStatus });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '更新失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  async function handleDeleteCollection(id: string) {
    if (!confirm('确认删除此合集？')) return;
    try {
      const res = await api.deleteProductCollection({ collection_id: id });
      if (res.status === 'failed') throw new Error((res.error as any)?.message || '删除失败');
      load();
    } catch (e: any) {
      alert(e.message);
    }
  }

  return (
    <div>
      <h1 className="text-2xl font-bold mb-6">分类与合集</h1>
      {error && <div className="mb-4 text-red-600">{error}</div>}
      {loading && <div className="text-gray-500 mb-4">加载中...</div>}

      {/* Categories */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">商品分类</h2>
        <button onClick={() => setShowCatForm(!showCatForm)} className="px-4 py-2 bg-blue-600 text-white rounded-lg hover:bg-blue-700 text-sm">
          {showCatForm ? '取消' : '新建分类'}
        </button>
      </div>
      {showCatForm && (
        <form onSubmit={handleCreateCategory} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 flex gap-3 items-end flex-wrap">
          <div>
            <label className="text-xs text-gray-500">名称</label>
            <input value={catName} onChange={(e) => setCatName(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">slug（唯一）</label>
            <input value={catSlug} onChange={(e) => setCatSlug(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">父分类（可选）</label>
            <select value={catParent} onChange={(e) => setCatParent(e.target.value)} className="px-3 py-2 border rounded-lg text-sm">
              <option value="">无（顶级）</option>
              {categories.map((c) => <option key={c.id} value={c.id}>{c.name}</option>)}
            </select>
          </div>
          <button type="submit" className="px-4 py-2 bg-blue-600 text-white rounded-lg text-sm hover:bg-blue-700">创建</button>
        </form>
      )}
      <div className="overflow-x-auto mb-8">
        <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
          <thead className="bg-gray-50 text-left">
            <tr>
              <th className="px-4 py-2 font-medium text-gray-600">名称</th>
              <th className="px-4 py-2 font-medium text-gray-600">slug</th>
              <th className="px-4 py-2 font-medium text-gray-600">父分类</th>
              <th className="px-4 py-2 font-medium text-gray-600">状态</th>
              <th className="px-4 py-2 font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {categories.map((c) => (
              <tr key={c.id}>
                <td className="px-4 py-2 font-medium">{c.name}</td>
                <td className="px-4 py-2 text-gray-500">{c.slug}</td>
                <td className="px-4 py-2 text-gray-500">{c.parent_name || '—'}</td>
                <td className="px-4 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${c.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{c.status}</span>
                </td>
                <td className="px-4 py-2">
                  <div className="flex gap-2">
                    <button onClick={() => handleRenameCategory(c)} className="text-xs text-blue-600 hover:underline">改名</button>
                    <button onClick={() => handleToggleCategory(c)} className="text-xs text-blue-600 hover:underline">
                      {c.status === 'active' ? '停用' : '启用'}
                    </button>
                    <button onClick={() => handleDeleteCategory(c.id)} className="text-xs text-red-600 hover:underline">删除</button>
                  </div>
                </td>
              </tr>
            ))}
            {categories.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-400">暂无分类</td></tr>}
          </tbody>
        </table>
      </div>

      {/* Collections */}
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">商品合集</h2>
        <button onClick={() => setShowColForm(!showColForm)} className="px-4 py-2 bg-indigo-600 text-white rounded-lg hover:bg-indigo-700 text-sm">
          {showColForm ? '取消' : '新建合集'}
        </button>
      </div>
      {showColForm && (
        <form onSubmit={handleCreateCollection} className="mb-4 p-4 bg-white rounded-xl border border-gray-200 flex gap-3 items-end">
          <div>
            <label className="text-xs text-gray-500">名称</label>
            <input value={colName} onChange={(e) => setColName(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <div>
            <label className="text-xs text-gray-500">slug（唯一）</label>
            <input value={colSlug} onChange={(e) => setColSlug(e.target.value)} required className="px-3 py-2 border rounded-lg text-sm" />
          </div>
          <button type="submit" className="px-4 py-2 bg-indigo-600 text-white rounded-lg text-sm hover:bg-indigo-700">创建</button>
        </form>
      )}
      <div className="overflow-x-auto">
        <table className="w-full text-sm bg-white rounded-xl overflow-hidden border border-gray-200">
          <thead className="bg-gray-50 text-left">
            <tr>
              <th className="px-4 py-2 font-medium text-gray-600">名称</th>
              <th className="px-4 py-2 font-medium text-gray-600">slug</th>
              <th className="px-4 py-2 font-medium text-gray-600">商品数</th>
              <th className="px-4 py-2 font-medium text-gray-600">状态</th>
              <th className="px-4 py-2 font-medium text-gray-600">操作</th>
            </tr>
          </thead>
          <tbody className="divide-y divide-gray-100">
            {collections.map((c) => (
              <tr key={c.id}>
                <td className="px-4 py-2 font-medium">{c.name}</td>
                <td className="px-4 py-2 text-gray-500">{c.slug}</td>
                <td className="px-4 py-2">{c.product_count}</td>
                <td className="px-4 py-2">
                  <span className={`px-2 py-0.5 rounded text-xs ${c.status === 'active' ? 'bg-green-100 text-green-700' : 'bg-gray-100 text-gray-600'}`}>{c.status}</span>
                </td>
                <td className="px-4 py-2">
                  <div className="flex gap-2">
                    <button onClick={() => handleToggleCollection(c)} className="text-xs text-blue-600 hover:underline">
                      {c.status === 'active' ? '停用' : '启用'}
                    </button>
                    <button onClick={() => handleDeleteCollection(c.id)} className="text-xs text-red-600 hover:underline">删除</button>
                  </div>
                </td>
              </tr>
            ))}
            {collections.length === 0 && <tr><td colSpan={5} className="px-4 py-6 text-center text-gray-400">暂无合集</td></tr>}
          </tbody>
        </table>
      </div>
    </div>
  );
}

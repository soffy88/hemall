'use client';

import { useEffect, useState } from 'react';
import Link from 'next/link';
import { api } from '@/lib/api-client';
import { errMessage } from '@/lib/errors';

type DocumentPayload = Awaited<ReturnType<typeof api.storeComplianceDocument>>;

export default function ComplianceDocument({ slug }: { slug: string }) {
  const [doc, setDoc] = useState<DocumentPayload | null>(null);
  const [error, setError] = useState('');

  useEffect(() => {
    api.storeComplianceDocument(slug)
      .then(setDoc)
      .catch((e) => setError(errMessage(e)));
  }, [slug]);

  return (
    <main className="min-h-screen bg-stone-50 px-4 py-10">
      <article className="mx-auto max-w-3xl rounded-2xl border border-stone-200 bg-white p-6 shadow-sm sm:p-10">
        <Link href="/shop" className="text-sm text-emerald-700 hover:underline">
          ← 返回商城
        </Link>
        {error && <p className="mt-6 rounded-lg bg-red-50 p-3 text-sm text-red-700">{error}</p>}
        {!doc && !error && <p className="mt-6 text-sm text-stone-500">加载中...</p>}
        {doc && (
          <>
            <div className="mt-6 flex flex-wrap items-center gap-2">
              <h1 className="text-3xl font-bold tracking-tight text-stone-900">{doc.title}</h1>
              {!doc.published && (
                <span className="rounded-full bg-amber-100 px-3 py-1 text-xs font-semibold text-amber-800">
                  即将公布
                </span>
              )}
            </div>
            {!doc.official_open && (
              <p className="mt-4 rounded-xl border border-amber-200 bg-amber-50 p-4 text-sm leading-6 text-amber-900">
                {doc.site_notice}
              </p>
            )}
            <p className="mt-4 text-xs text-stone-400">文档版本：{doc.version}</p>
            <div className="mt-8 whitespace-pre-wrap text-[15px] leading-8 text-stone-700">
              {doc.content}
            </div>
          </>
        )}
      </article>
    </main>
  );
}

import { NextRequest, NextResponse } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 同源 BFF 反代 catch-all（F-01/F-03）：/api/*（除 app/api/auth/* 静态路由）
 * 之外的请求全部原样转发到后端同路径。
 *
 *  静态段路由（/api/auth/login、/api/auth/refresh 等）优先于本 catch-all，
 * 它们负责剥离响应体里的 access_token；这里只做透明转发。 */

/** 浏览器侧不需要这些请求头，交给后端 hop-by-hop 处理已在 proxy.ts。 */
const NO_BODY_METHODS = new Set(['GET', 'HEAD']);

export async function handler(request: NextRequest) {
  const { pathname, search } = request.nextUrl;
  const backendPath = pathname.replace(/^\/api/, '') + search;

  try {
    // 必须用 arrayBuffer：request.text() 会破坏 multipart/form-data 等二进制体。
    const init: RequestInit = { method: request.method };
    if (!NO_BODY_METHODS.has(request.method)) {
      init.body = await request.arrayBuffer();
    }
    return await proxyToBackend(request, backendPath, init);
  } catch (error) {
    console.error(`BFF proxy error (${request.method} ${pathname}):`, error);
    return NextResponse.json({ detail: '后端服务不可用' }, { status: 502 });
  }
}

export const GET = handler;
export const HEAD = handler;
export const POST = handler;
export const PUT = handler;
export const PATCH = handler;
export const DELETE = handler;
export const OPTIONS = handler;

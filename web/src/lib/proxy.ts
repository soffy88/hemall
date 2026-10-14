/** BFF 透明代理：浏览器 → 同源 /api/* → 本模块 → 后端。
 *
 * F-01/F-03: 浏览器只发同源请求（cookie 自动附带、无 CORS 问题），Next 服务端
 * 把 Cookie / X-CSRF-Token / X-Device-Id 等头转发给后端，并把后端 Set-Cookie
 * 原样回传。访问令牌全程只在 HttpOnly cookie 与后端之间流动，客户端 JS 拿不到。
 *
 * 本文件只允许被 web/src/app/api 下的 route handler 引用（server-only）。
 */

import { NextRequest, NextResponse } from 'next/server';
import { BACKEND_URL } from './backend-url';

/** 不透传的 hop-by-hop / 重编码头（fetch 会自动处理，透传反而出错）。 */
const DROP_REQUEST_HEADERS = new Set([
  'host',
  'connection',
  'content-length',
  'transfer-encoding',
  'keep-alive',
  'upgrade',
]);

const DROP_RESPONSE_HEADERS = new Set([
  'connection',
  'content-length',
  'transfer-encoding',
  'keep-alive',
]);

export async function proxyToBackend(
  request: NextRequest,
  backendPath: string,
  init?: RequestInit,
): Promise<NextResponse> {
  const headers = new Headers();
  request.headers.forEach((value, key) => {
    if (!DROP_REQUEST_HEADERS.has(key)) headers.set(key, value);
  });

  const res = await fetch(`${BACKEND_URL}${backendPath}`, {
    ...init,
    headers,
    redirect: 'manual',
  });

  const body = await res.arrayBuffer();

  const responseHeaders = new Headers();
  res.headers.forEach((value, key) => {
    if (DROP_RESPONSE_HEADERS.has(key)) return;
    if (key === 'set-cookie') return; // 单独 append，避免被覆盖
    responseHeaders.set(key, value);
  });
  for (const setCookie of res.headers.getSetCookie()) {
    responseHeaders.append('set-cookie', setCookie);
  }

  return new NextResponse(body, {
    status: res.status,
    headers: responseHeaders,
  });
}

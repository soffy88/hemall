/** BFF 透明代理：浏览器 → 同源 /api/* → 本模块 → 后端。
 *
 * F-01/F-03: 浏览器只发同源请求（cookie 自动附带、无 CORS 问题），Next 服务端
 * 把 Cookie / X-CSRF-Token / X-Device-Id 等头转发给后端，并把后端 Set-Cookie
 * 原样回传。访问令牌全程只在 HttpOnly cookie 与后端之间流动，客户端 JS 拿不到。
 *
 * 同源闸门 (F-03 兜底): 后端对 /agent/*、/store/* 等前缀整段豁免 CSRF（Bearer /
 * capability token 体系），浏览器又只能从 BFF 进后端，所以"跨站状态变更请求"
 * 必须在 BFF 这一层拦掉：校验 Origin / Sec-Fetch-Site，跨站直接 403 且不打后端。
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

/** 状态变更方法：浏览器发这些请求必然带 Origin，BFF 必须做同源核验。 */
const STATE_CHANGING_METHODS = new Set(['POST', 'PUT', 'PATCH', 'DELETE']);

/** 多域名部署（如 mall.example.com + www）时追加的放行来源，逗号分隔。 */
function extraAllowedOrigins(): string[] {
  return (process.env.HEMALL_BFF_ORIGINS ?? '')
    .split(',')
    .map((value) => value.trim().replace(/\/+$/, ''))
    .filter(Boolean);
}

/** 该请求是否跨站：Origin 不在放行列表，或 Sec-Fetch-Site 明示 cross-site。
 *
 * 判定顺序（任一命中即跨站）：
 *  1. Sec-Fetch-Site: cross-site —— 浏览器禁设头，伪造不了，最可信；
 *  2. Origin 存在且不在"BFF 自身 origin + HEMALL_BFF_ORIGINS"白名单里；
 *     Origin: null（沙箱 iframe / 隐私上下文）一律不信任。
 *
 * 无 Origin 且无 Sec-Fetch-Site 的请求视为服务端客户端（curl / 上游服务）：
 * 浏览器发状态变更请求一定会带 Origin，缺少即非浏览器，不属于 CSRF 向量，
 * 放行（与后端 CSRF 中间件对"旧浏览器"的处理保持一致）。
 */
export function isCrossSiteRequest(request: NextRequest): boolean {
  const secFetchSite = request.headers.get('sec-fetch-site');
  if (secFetchSite && secFetchSite !== 'same-origin' && secFetchSite !== 'same-site' && secFetchSite !== 'none') {
    return true;
  }

  const origin = request.headers.get('origin');
  if (!origin) return false;
  if (origin === 'null') return true;

  const allowed = new Set([request.nextUrl.origin.replace(/\/+$/, ''), ...extraAllowedOrigins()]);
  return !allowed.has(origin.replace(/\/+$/, ''));
}

/** 状态变更闸门：跨站请求返回 403 响应（调用方直接回给浏览器），同源返回 null。 */
function rejectCrossSite(request: NextRequest, init?: RequestInit): NextResponse | null {
  const method = (init?.method ?? request.method).toUpperCase();
  if (!STATE_CHANGING_METHODS.has(method)) return null;
  if (!isCrossSiteRequest(request)) return null;
  return NextResponse.json({ detail: 'CSRF: cross-site request rejected' }, { status: 403 });
}

export async function proxyToBackend(
  request: NextRequest,
  backendPath: string,
  init?: RequestInit,
): Promise<NextResponse> {
  const crossSite = rejectCrossSite(request, init);
  if (crossSite) return crossSite;

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

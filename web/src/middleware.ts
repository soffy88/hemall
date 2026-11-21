import { NextRequest, NextResponse } from 'next/server';
import { BACKEND_URL } from '@/lib/backend-url';

/** /admin 页面的服务端会话守卫（F-01/F-02）。
 *
 *  cookie 分级处理：
 *  - 双 cookie 缺失 → 直接 302 /login（不发后端请求）。
 *  - GET /auth/me 200 → 放行（只按 typ=staff 解码，customer token 天然 401）。
 *  - /auth/me 401 → POST /auth/refresh 续期；续期后重建 cookie 头再查一次。
 *  - 续期失败 / 重查仍非 200 → 跳 /login 并清三 cookie（过期会话）。
 *  - 续期成功但 /auth/me 仍非 200 → 跳 /login **不清 cookie**（权限问题，
 *    不销毁合法的 customer 会话）。
 *  - 后端不可达（网络错误）→ 跳 /login 但不清 cookie，避免误杀会话。
 *
 *  续期成功的 Set-Cookie 必须逐条 append 到放行响应上，否则浏览器拿不到新的
 *  access cookie，下次请求又会走一遍续期。 */

const ACCESS_COOKIE = 'hemall_access_token';
const REFRESH_COOKIE = 'hemall_refresh_token';
const CSRF_COOKIE = 'hemall_csrf_token';
const SESSION_COOKIES = [ACCESS_COOKIE, REFRESH_COOKIE, CSRF_COOKIE];

/** 兼容没有 Headers.getSetCookie() 的运行时（返回值按出现顺序）。 */
function setCookiesOf(headers: Headers): string[] {
  const getSetCookie = (headers as unknown as { getSetCookie?: () => string[] })
    .getSetCookie;
  if (typeof getSetCookie === 'function') return getSetCookie.call(headers);
  const single = headers.get('set-cookie');
  return single ? [single] : [];
}

/** 只取 Set-Cookie 的 name=value 部分（丢弃 Path/HttpOnly/Max-Age 等属性）。 */
function cookiePairs(setCookies: string[]): string[] {
  return setCookies.map((sc) => sc.split(';', 1)[0].trim()).filter(Boolean);
}

/** 把续期签发的 cookie 合并进请求 cookie 头（同名覆盖，其余保留）。 */
function mergeCookieHeader(
  original: string | null,
  setCookies: string[],
): string {
  const jar = new Map<string, string>();
  for (const part of (original ?? '').split(';')) {
    const pair = part.trim();
    if (pair) {
      const eq = pair.indexOf('=');
      if (eq > 0) jar.set(pair.slice(0, eq), pair);
    }
  }
  for (const pair of cookiePairs(setCookies)) {
    const eq = pair.indexOf('=');
    if (eq > 0) jar.set(pair.slice(0, eq), pair);
  }
  return [...jar.values()].join('; ');
}

function redirectToLogin(request: NextRequest): NextResponse {
  const loginUrl = new URL('/login', request.nextUrl.origin);
  const target = request.nextUrl.pathname + request.nextUrl.search;
  if (target && target !== '/admin') loginUrl.searchParams.set('redirect', target);
  return NextResponse.redirect(loginUrl);
}

/** 跳转 /login 并显式清掉三个会话 cookie（后端 401 已清则重复无害）。 */
function redirectToLoginAndClear(request: NextRequest): NextResponse {
  const res = redirectToLogin(request);
  for (const name of SESSION_COOKIES) {
    res.cookies.set(name, '', {
      path: '/',
      maxAge: 0,
      httpOnly: name !== CSRF_COOKIE,
      sameSite: 'lax',
      secure: process.env.NODE_ENV === 'production',
    });
  }
  return res;
}

export async function middleware(request: NextRequest) {
  const hasAccess = request.cookies.has(ACCESS_COOKIE);
  const hasRefresh = request.cookies.has(REFRESH_COOKIE);
  if (!hasAccess || !hasRefresh) return redirectToLogin(request);

  const origin = BACKEND_URL;
  const baseCookie = request.headers.get('cookie') ?? '';

  // 1) 校验现有 access 是否仍然有效（staff 视角）。
  let meRes: Response;
  try {
    meRes = await fetch(`${origin}/auth/me`, {
      headers: { cookie: baseCookie },
      cache: 'no-store',
    });
  } catch {
    return redirectToLogin(request); // 网络错误：不清 cookie
  }
  if (meRes.ok) return NextResponse.next();

  // 2) 401 → 用 refresh cookie 续期（后端会重发 access + csrf Set-Cookie）。
  let refreshRes: Response;
  try {
    refreshRes = await fetch(`${origin}/auth/refresh`, {
      method: 'POST',
      headers: { cookie: baseCookie },
      cache: 'no-store',
    });
  } catch {
    return redirectToLogin(request); // 网络错误：不清 cookie
  }
  const refreshSetCookies = setCookiesOf(refreshRes.headers);
  if (!refreshRes.ok) {
    // 续期失败（refresh 过期）：透传/补清 cookie 后跳登录。
    const res = redirectToLoginAndClear(request);
    for (const sc of refreshSetCookies) res.headers.append('set-cookie', sc);
    return res;
  }

  // 3) 用新 cookie 重查 /auth/me：仍是 401 说明身份不是 staff（如 customer
  //    会话误入 /admin），此时跳登录但不清 cookie。
  const mergedCookie = mergeCookieHeader(baseCookie, refreshSetCookies);
  let recheck: Response;
  try {
    recheck = await fetch(`${origin}/auth/me`, {
      headers: { cookie: mergedCookie },
      cache: 'no-store',
    });
  } catch {
    return redirectToLogin(request);
  }
  if (!recheck.ok) return redirectToLogin(request);

  const res = NextResponse.next();
  for (const sc of refreshSetCookies) res.headers.append('set-cookie', sc);
  return res;
}

export const config = {
  matcher: ['/admin/:path*'],
};

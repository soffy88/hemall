import { NextRequest, NextResponse } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 会话续期：透传到后端 POST /auth/refresh。
 *
 *  后端语义（app/auth.py:258）：用 HttpOnly 的 refresh cookie 重签 access +
 *  csrf 两个 Set-Cookie，**不轮换** refresh cookie；refresh cookie 失效时
 *  返回 401 并把三个 cookie 全部清掉（maxAge=0）——这两类 Set-Cookie 都必须
 *  原样回传给浏览器，否则前端拿不到新 access 或清不掉过期会话。
 *  响应体里的 access_token 是给直连后端的客户端用的；经 BFF 一律剥离。 */
export async function POST(request: NextRequest) {
  try {
    const proxied = await proxyToBackend(request, '/auth/refresh', {
      method: 'POST',
    });

    if (proxied.status !== 200) {
      // 401：透传上游的清 cookie Set-Cookie，让浏览器丢弃过期会话
      return proxied;
    }

    const res = NextResponse.json({ ok: true });
    for (const setCookie of proxied.headers.getSetCookie()) {
      res.headers.append('set-cookie', setCookie);
    }
    return res;
  } catch (error) {
    console.error('Session refresh error:', error);
    return NextResponse.json({ detail: '刷新失败' }, { status: 502 });
  }
}

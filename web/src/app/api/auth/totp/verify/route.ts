import { NextRequest, NextResponse } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** TOTP 登录第二步：透传到后端 POST /auth/totp/verify，Set-Cookie 原样回传。
 *  后端成功时返回 TokenResponse；经 BFF 时剥离 access_token，只回
 *  user_id/email——与 login 路由同一条 F-02 约定，客户端 JS 接触不到令牌。
 *  失败 (401/422 等) 时 detail 原样透传，供登录页展示。 */
export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: 'invalid JSON body' }, { status: 400 });
  }

  const proxied = await proxyToBackend(request, '/auth/totp/verify', {
    method: 'POST',
    body: JSON.stringify(body),
  });

  if (proxied.status !== 200) {
    return proxied;
  }

  const data = await proxied.json();
  const res = NextResponse.json({
    user_id: data.user_id,
    email: data.email,
  });
  // Set-Cookie 逐条搬过来（多值头用逗号 join 会破坏语法）
  for (const setCookie of proxied.headers.getSetCookie()) {
    res.headers.append('set-cookie', setCookie);
  }
  return res;
}

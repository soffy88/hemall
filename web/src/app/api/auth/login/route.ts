import { NextRequest, NextResponse } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 管理员登录：透传到后端 POST /auth/login，Set-Cookie 原样回传。
 *  后端 JSON 里的 access_token 是给直连后端的 API 客户端用的；经 BFF 时
 *  剥离，只回 user_id/email——客户端 JS 永远接触不到令牌。 */
export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: 'invalid JSON body' }, { status: 400 });
  }

  const proxied = await proxyToBackend(request, '/auth/login', {
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
  // proxyToBackend 已经把 Set-Cookie 写在 proxied 上，逐条搬过来
  // （Set-Cookie 不能用 forEach 拼接——多值头用逗号 join 会破坏语法）
  for (const setCookie of proxied.headers.getSetCookie()) {
    res.headers.append('set-cookie', setCookie);
  }
  return res;
}

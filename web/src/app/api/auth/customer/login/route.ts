import { NextRequest, NextResponse } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 顾客登录：透传到后端 POST /auth/customer/login（typ=customer 会话 cookie）。
 *  access_token 只留给直连后端的 API 客户端；经 BFF 时剥离，只回 customer_id/email。 */
export async function POST(request: NextRequest) {
  let body: unknown;
  try {
    body = await request.json();
  } catch {
    return NextResponse.json({ detail: 'invalid JSON body' }, { status: 400 });
  }

  const proxied = await proxyToBackend(request, '/auth/customer/login', {
    method: 'POST',
    body: JSON.stringify(body),
  });

  if (proxied.status !== 200) {
    return proxied;
  }

  const data = await proxied.json();
  const res = NextResponse.json({
    customer_id: data.customer_id,
    email: data.email,
  });
  for (const setCookie of proxied.headers.getSetCookie()) {
    res.headers.append('set-cookie', setCookie);
  }
  return res;
}

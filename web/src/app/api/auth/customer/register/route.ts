import { NextRequest, NextResponse } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 顾客注册：透传到后端 POST /auth/customer/register，Set-Cookie 原样回传。
 *
 *  proxyToBackend 不会自动转发请求体，必须在 init 里显式带上（用 arrayBuffer
 *  而非 text/json，避免把二进制 body 破坏掉）。上游 TokenResponse 里的
 *  access_token 给直连后端的客户端用；经 BFF 剥离，只回 customer_id/email。 */
export async function POST(request: NextRequest) {
  let body: ArrayBuffer;
  try {
    body = await request.arrayBuffer();
  } catch {
    return NextResponse.json({ detail: 'invalid request body' }, { status: 400 });
  }

  try {
    const proxied = await proxyToBackend(request, '/auth/customer/register', {
      method: 'POST',
      body,
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
  } catch (error) {
    console.error('Customer registration error:', error);
    return NextResponse.json({ detail: '注册失败' }, { status: 502 });
  }
}

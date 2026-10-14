/** app/api/auth/* 与 BFF catch-all 路由单测（audit 核验）。
 *
 * 覆盖 BFF 契约（F-01/F-02/F-03/F-05）：
 *  - /api/auth/me、refresh 只打后端专用会话接口，绝不调 /admin/* 业务端点；
 *  - 响应体里的 access_token 一律剥离，客户端 JS 永远拿不到令牌；
 *  - Set-Cookie（HttpOnly 会话 + csrf）逐条原样回传；
 *  - cookie / X-CSRF-Token 头透传给后端，hop-by-hop 头被剥离；
 *  - catch-all 去掉 /api 前缀后透传，GET 不带 body，二进制体不被破坏。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';

import { GET as meGET } from '@/app/api/auth/me/route';
import { POST as refreshPOST } from '@/app/api/auth/refresh/route';
import { GET as csrfGET } from '@/app/api/auth/csrf-token/route';
import { POST as loginPOST } from '@/app/api/auth/login/route';
import { POST as logoutPOST } from '@/app/api/auth/logout/route';
import { GET as customerMeGET } from '@/app/api/auth/customer/me/route';
import { POST as customerLoginPOST } from '@/app/api/auth/customer/login/route';
import { POST as customerLogoutPOST } from '@/app/api/auth/customer/logout/route';
import { POST as catchAllPOST, GET as catchAllGET } from '@/app/api/[...path]/route';

interface Upstream {
  status?: number;
  body?: string;
  setCookies?: string[];
  headers?: Record<string, string>;
  throws?: boolean;
}

const calls: Array<{ method: string; url: string; headers: Record<string, string>; body?: string }> = [];

function jsonUpstream(body: unknown, status = 200, setCookies?: string[]): Upstream {
  return { status, body: JSON.stringify(body), setCookies };
}

function stubUpstream(upstream: Upstream) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers: Record<string, string> = {};
    new Headers((init?.headers ?? {}) as HeadersInit).forEach((v, k) => {
      headers[k.toLowerCase()] = v;
    });
    calls.push({
      method: (init?.method ?? 'GET').toUpperCase(),
      url: String(input),
      headers,
      body: bodyText(init?.body),
    });
    if (upstream.throws) throw new Error('ECONNREFUSED');

    const resHeaders = new Headers(upstream.headers ?? {});
    resHeaders.set('content-type', 'application/json');
    for (const sc of upstream.setCookies ?? []) resHeaders.append('set-cookie', sc);
    return new Response(upstream.body ?? '{}', { status: upstream.status ?? 200, headers: resHeaders });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/** 请求体可能是 string / ArrayBuffer / Uint8Array，统一转成可比对的文本。 */
function bodyText(body: BodyInit | null | undefined): string | undefined {
  if (body == null) return undefined;
  if (typeof body === 'string') return body;
  if (body instanceof ArrayBuffer) return new TextDecoder().decode(new Uint8Array(body));
  if (ArrayBuffer.isView(body)) {
    return new TextDecoder().decode(new Uint8Array(body.buffer, body.byteOffset, body.byteLength));
  }
  return String(body);
}

function req(path: string, init?: RequestInit): NextRequest {
  return new NextRequest(new URL(`https://shop.hemall.test${path}`), {
    method: 'GET',
    ...init,
  });
}

function setCookiesOf(res: Response): string[] {
  const getter = (res.headers as unknown as { getSetCookie?: () => string[] }).getSetCookie;
  return typeof getter === 'function' ? getter.call(res.headers) : [];
}

const SESSION_JAR = 'hemall_access_token=access-1; hemall_refresh_token=refresh-1; hemall_csrf_token=tok-1';

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('专用会话路由只打后端 /auth/*', () => {
  it('/api/auth/me 透传到后端 GET /auth/me，不碰 /admin/users', async () => {
    stubUpstream(jsonUpstream({ user_id: 'u-1', email: 'a@b.c' }));
    const res = await meGET(req('/api/auth/me', { headers: { cookie: SESSION_JAR } }));

    expect(calls).toHaveLength(1);
    expect(calls[0].method).toBe('GET');
    expect(calls[0].url).toBe('http://backend.test/auth/me');
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ user_id: 'u-1', email: 'a@b.c' });
  });

  it('/api/auth/me 转发浏览器 cookie（含 HttpOnly 会话）给后端', async () => {
    stubUpstream(jsonUpstream({ user_id: 'u-1', email: 'a@b.c' }));
    await meGET(req('/api/auth/me', { headers: { cookie: SESSION_JAR } }));
    expect(calls[0].headers.cookie).toBe(SESSION_JAR);
  });

  it('/api/auth/me 401 原样透传给前端（api-client 据此触发续期）', async () => {
    stubUpstream(jsonUpstream({ detail: '未登录' }, 401));
    const res = await meGET(req('/api/auth/me', { headers: { cookie: SESSION_JAR } }));
    expect(res.status).toBe(401);
  });

  it('/api/auth/customer/me 透传到后端 GET /auth/customer/me', async () => {
    stubUpstream(jsonUpstream({ customer_id: 'c-1', email: 'b@b.c' }));
    const res = await customerMeGET(req('/api/auth/customer/me', { headers: { cookie: SESSION_JAR } }));
    expect(calls[0].url).toBe('http://backend.test/auth/customer/me');
    expect(res.status).toBe(200);
  });

  it('/api/auth/csrf-token 透传到后端并把非 HttpOnly csrf cookie 回传', async () => {
    stubUpstream(
      jsonUpstream({ csrf_token: 'tok-9' }, 200, ['hemall_csrf_token=tok-9; Path=/; SameSite=lax']),
    );
    const res = await csrfGET(req('/api/auth/csrf-token'));
    expect(calls[0].url).toBe('http://backend.test/auth/csrf-token');
    expect(setCookiesOf(res)).toEqual(['hemall_csrf_token=tok-9; Path=/; SameSite=lax']);
  });

  it('/api/auth/logout 透传后端清 cookie 的 Set-Cookie', async () => {
    stubUpstream(jsonUpstream({ ok: true }, 200, ['hemall_access_token=; Path=/; Max-Age=0; HttpOnly']));
    const res = await logoutPOST(
      req('/api/auth/logout', { method: 'POST', headers: { cookie: SESSION_JAR } }),
    );
    expect(calls[0].url).toBe('http://backend.test/auth/logout');
    expect(calls[0].method).toBe('POST');
    expect(setCookiesOf(res)).toEqual(['hemall_access_token=; Path=/; Max-Age=0; HttpOnly']);
  });

  it('/api/auth/customer/logout 同样透传', async () => {
    stubUpstream(jsonUpstream({ ok: true }, 200, ['hemall_customer_access=; Path=/; Max-Age=0; HttpOnly']));
    const res = await customerLogoutPOST(
      req('/api/auth/customer/logout', { method: 'POST', headers: { cookie: SESSION_JAR } }),
    );
    expect(calls[0].url).toBe('http://backend.test/auth/customer/logout');
    expect(setCookiesOf(res)).toHaveLength(1);
  });
});

describe('access_token 绝不下发客户端 JS（F-02）', () => {
  it('/api/auth/refresh 只回 { ok:true }，access_token 被剥离但 cookie 保留', async () => {
    stubUpstream(
      jsonUpstream(
        { ok: true, access_token: 'super-secret-jwt' },
        200,
        ['hemall_access_token=access-2; Path=/; HttpOnly', 'hemall_csrf_token=tok-2; Path=/'],
      ),
    );
    const res = await refreshPOST(
      req('/api/auth/refresh', { method: 'POST', headers: { cookie: SESSION_JAR } }),
    );

    expect(calls[0].url).toBe('http://backend.test/auth/refresh');
    expect(calls[0].method).toBe('POST');
    expect(await res.json()).toEqual({ ok: true });
    expect(res.headers.get('set-cookie') ?? '').not.toContain('super-secret-jwt');
    expect(setCookiesOf(res)).toEqual([
      'hemall_access_token=access-2; Path=/; HttpOnly',
      'hemall_csrf_token=tok-2; Path=/',
    ]);
  });

  it('/api/auth/login 只回 user_id/email，access_token 被剥离', async () => {
    stubUpstream(
      jsonUpstream(
        { user_id: 'u-1', email: 'a@b.c', access_token: 'super-secret-jwt', token_type: 'bearer' },
        200,
        ['hemall_access_token=access-1; Path=/; HttpOnly'],
      ),
    );
    const res = await loginPOST(
      req('/api/auth/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', cookie: SESSION_JAR },
        body: JSON.stringify({ email: 'a@b.c', password: 'pw' }),
      }),
    );

    expect(calls[0].url).toBe('http://backend.test/auth/login');
    expect(calls[0].body).toBe(JSON.stringify({ email: 'a@b.c', password: 'pw' }));
    expect(await res.json()).toEqual({ user_id: 'u-1', email: 'a@b.c' });
    expect(setCookiesOf(res)).toEqual(['hemall_access_token=access-1; Path=/; HttpOnly']);
  });

  it('/api/auth/login 401 原样透出后端 detail', async () => {
    stubUpstream(jsonUpstream({ detail: '邮箱或密码错误' }, 401));
    const res = await loginPOST(
      req('/api/auth/login', {
        method: 'POST',
        body: JSON.stringify({ email: 'a@b.c', password: 'x' }),
      }),
    );
    expect(res.status).toBe(401);
    await expect(res.json()).resolves.toEqual({ detail: '邮箱或密码错误' });
  });

  it('/api/auth/login 请求体不是 JSON 时 400，且不打后端', async () => {
    const fetchMock = stubUpstream(jsonUpstream({}));
    const res = await loginPOST(
      req('/api/auth/login', { method: 'POST', body: 'not-json' }),
    );
    expect(res.status).toBe(400);
    await expect(res.json()).resolves.toEqual({ detail: 'invalid JSON body' });
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('/api/auth/customer/login 剥离 customer access_token', async () => {
    stubUpstream(
      jsonUpstream({ customer_id: 'c-1', email: 'b@b.c', access_token: 'cus-secret' }, 200, ['k=v; Path=/']),
    );
    const res = await customerLoginPOST(
      req('/api/auth/customer/login', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', cookie: 'hemall_csrf_token=tok-1' },
        body: JSON.stringify({ email: 'b@b.c', password: 'pw' }),
      }),
    );
    expect(calls[0].url).toBe('http://backend.test/auth/customer/login');
    await expect(res.json()).resolves.toEqual({ customer_id: 'c-1', email: 'b@b.c' });
  });
});

describe('CSRF 与头透传（F-03）', () => {
  it('X-CSRF-Token 与 cookie 一起转发给后端', async () => {
    stubUpstream(jsonUpstream({ customer_id: 'c-1', email: 'b@b.c' }));
    await customerLoginPOST(
      req('/api/auth/customer/login', {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          cookie: 'hemall_csrf_token=tok-1',
          'X-CSRF-Token': 'tok-1',
        },
        body: JSON.stringify({ email: 'b@b.c', password: 'pw' }),
      }),
    );
    expect(calls[0].headers['x-csrf-token']).toBe('tok-1');
    expect(calls[0].headers.cookie).toBe('hemall_csrf_token=tok-1');
  });

  it('hop-by-hop 头（host / content-length）不转发', async () => {
    stubUpstream(jsonUpstream({ user_id: 'u-1' }));
    await meGET(
      req('/api/auth/me', {
        headers: { cookie: SESSION_JAR, host: 'shop.hemall.test', 'content-length': '0' },
      }),
    );
    expect(calls[0].headers.host).toBeUndefined();
    expect(calls[0].headers['content-length']).toBeUndefined();
    expect(calls[0].headers.cookie).toBe(SESSION_JAR);
  });

  it('后端不可达时 /auth/refresh 返回 502 而不是抛出', async () => {
    stubUpstream({ throws: true });
    const res = await refreshPOST(req('/api/auth/refresh', { method: 'POST' }));
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toEqual({ detail: '刷新失败' });
  });
});

describe('BFF catch-all 同源反代', () => {
  it('去掉 /api 前缀后按原路径转发，query 保留', async () => {
    stubUpstream(jsonUpstream([{ id: 'o1' }]));
    const res = await catchAllGET(
      req('/api/admin/orders?status=open&limit=5', { headers: { cookie: SESSION_JAR } }),
    );
    expect(calls[0].url).toBe('http://backend.test/admin/orders?status=open&limit=5');
    expect(calls[0].headers.cookie).toBe(SESSION_JAR);
    await expect(res.json()).resolves.toEqual([{ id: 'o1' }]);
  });

  it('GET 不带请求体', async () => {
    stubUpstream(jsonUpstream({}));
    await catchAllGET(req('/api/health'));
    expect(calls[0].body).toBeUndefined();
  });

  it('POST 请求体原样透传', async () => {
    stubUpstream(jsonUpstream({ ok: true }));
    await catchAllPOST(
      req('/api/catalog/create_product', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ title: '土豆', slug: 'potato' }),
      }),
    );
    expect(calls[0].url).toBe('http://backend.test/catalog/create_product');
    expect(calls[0].method).toBe('POST');
    expect(JSON.parse(calls[0].body as string)).toEqual({ title: '土豆', slug: 'potato' });
  });

  it('二进制/表单体不被破坏（multipart 上传走这里）', async () => {
    stubUpstream(jsonUpstream({ ok: true }));
    const raw = new Uint8Array([0x89, 0x50, 0x4e, 0x47, 0x00, 0xff]);
    await catchAllPOST(
      req('/api/agent/ingest', {
        method: 'POST',
        headers: { 'Content-Type': 'multipart/form-data; boundary=xyz' },
        body: raw,
      }),
    );
    expect(calls[0].url).toBe('http://backend.test/agent/ingest');
    expect(calls[0].headers['content-type']).toBe('multipart/form-data; boundary=xyz');
  });

  it('后端 4xx/5xx 状态原样透传', async () => {
    stubUpstream(jsonUpstream({ detail: '库存不足' }, 409));
    const res = await catchAllPOST(
      req('/api/inventory/create_inventory_batch', { method: 'POST', body: '{}' }),
    );
    expect(res.status).toBe(409);
    await expect(res.json()).resolves.toEqual({ detail: '库存不足' });
  });

  it('后端不可达 → 502 且不抛异常', async () => {
    stubUpstream({ throws: true });
    const res = await catchAllGET(req('/api/health'));
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toEqual({ detail: '后端服务不可用' });
  });

  it('后端 Set-Cookie 多值不会被逗号合并（逐条 append）', async () => {
    stubUpstream(
      jsonUpstream({ ok: true }, 200, ['a=1; Path=/; HttpOnly', 'b=2; Path=/', 'c=3; Path=/']),
    );
    const res = await catchAllGET(req('/api/health'));
    expect(setCookiesOf(res)).toEqual(['a=1; Path=/; HttpOnly', 'b=2; Path=/', 'c=3; Path=/']);
  });
});
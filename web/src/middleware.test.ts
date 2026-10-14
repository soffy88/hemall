/** src/middleware 单测（audit 核验：/admin 服务端会话守卫）。
 *
 * 覆盖 F-01/F-02 的服务端不变量：
 *  - 守卫只走专用会话接口 GET /auth/me + POST /auth/refresh，绝不调业务接口；
 *  - 双 cookie 缺失直接 302，不打后端；
 *  - 续期成功后 Set-Cookie 必须逐条带回放行响应；
 *  - 过期会话清 cookie 跳登录；网络错误/非 staff 会话不清 cookie。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';
import { middleware } from '@/middleware';

interface Route {
  status?: number;
  body?: string;
  setCookies?: string[];
  throws?: boolean;
}

/** 预置的后端应答队列：`seed()` 按调用顺序消费，记录在 calls 里。 */
const script = new Map<string, Route[]>();
const calls: Array<{ method: string; path: string; cookie?: string | null }> = [];

function seed(method: string, path: string, route: Route): void {
  const key = `${method.toUpperCase()} ${path}`;
  script.set(key, [...(script.get(key) ?? []), route]);
}

function setCookieHeaders(list: string[] | undefined): Headers {
  const headers = new Headers();
  for (const sc of list ?? []) headers.append('set-cookie', sc);
  return headers;
}

function stubBackend() {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = new URL(String(input));
    const method = (init?.method ?? 'GET').toUpperCase();
    const headers = (init?.headers ?? {}) as Record<string, string>;
    const key = Object.entries(headers).find(([k]) => k.toLowerCase() === 'cookie')?.[1];
    calls.push({ method, path: url.pathname, cookie: key ?? null });

    const queue = script.get(`${method} ${url.pathname}`) ?? [];
    const route = queue.shift() ?? { status: 404, body: '{}' };
    if (route.throws) throw new Error('ECONNREFUSED');
    return new Response(route.body ?? '{}', {
      status: route.status ?? 200,
      headers: setCookieHeaders(route.setCookies),
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function adminRequest(cookies: Record<string, string> = {}): NextRequest {
  const jar = Object.entries(cookies)
    .map(([k, v]) => `${k}=${v}`)
    .join('; ');
  return new NextRequest(new URL('https://shop.hemall.test/admin/orders'), {
    headers: jar ? { cookie: jar } : {},
  });
}

const VALID = {
  hemall_access_token: 'access-1',
  hemall_refresh_token: 'refresh-1',
};

function setCookiesOf(res: Response): string[] {
  const getter = (res.headers as unknown as { getSetCookie?: () => string[] }).getSetCookie;
  return typeof getter === 'function' ? getter.call(res.headers) : [];
}

beforeEach(() => {
  calls.length = 0;
  script.clear();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('/admin 服务端守卫', () => {
  it('access / refresh cookie 都在且 /auth/me 200 → 直接放行且不打后端两次', async () => {
    seed('GET', '/auth/me', { status: 200 });
    stubBackend();
    const res = await middleware(adminRequest(VALID));
    expect(res.status).toBe(200);
    expect(res.headers.get('x-middleware-next')).toBe('1');
    expect(calls.map((r) => `${r.method} ${r.path}`)).toEqual(['GET /auth/me']);
  });

  it('守卫只调用专用会话接口，绝不触碰 /admin/* 业务端点', async () => {
    seed('GET', '/auth/me', { status: 200 });
    stubBackend();
    await middleware(adminRequest(VALID));
    expect(calls.some((r) => r.path.startsWith('/admin/'))).toBe(false);
    expect(calls.some((r) => r.path === '/admin/users')).toBe(false);
  });

  it('access cookie 缺失 → 302 /login 且不打后端', async () => {
    stubBackend();
    const res = await middleware(adminRequest({ hemall_refresh_token: 'refresh-1' }));
    expect(res.status).toBe(307);
    expect(res.headers.get('location')).toContain('/login');
    expect(calls).toHaveLength(0);
  });

  it('refresh cookie 缺失 → 302 /login 且不打后端', async () => {
    stubBackend();
    const res = await middleware(adminRequest({ hemall_access_token: 'access-1' }));
    expect(res.status).toBe(307);
    expect(calls).toHaveLength(0);
  });

  it('跳转带 redirect 回跳参数（/admin 本身除外）', async () => {
    stubBackend();
    const res = await middleware(
      new NextRequest(new URL('https://shop.hemall.test/admin/orders?status=open'), { headers: { cookie: 'hemall_access_token=a; hemall_refresh_token=r' } }),
    );
    const loc = new URL(res.headers.get('location') as string);
    expect(loc.pathname).toBe('/login');
    expect(loc.searchParams.get('redirect')).toBe('/admin/orders?status=open');
  });

  it('/admin 自身不写 redirect 参数（避免循环）', async () => {
    stubBackend();
    const res = await middleware(
      new NextRequest(new URL('https://shop.hemall.test/admin'), { headers: { cookie: 'hemall_access_token=a; hemall_refresh_token=r' } }),
    );
    const loc = new URL(res.headers.get('location') as string);
    expect(loc.pathname).toBe('/login');
    expect(loc.searchParams.get('redirect')).toBeNull();
  });

  it('/auth/me 401 → 续期成功后放行，并把新 Set-Cookie 带回响应', async () => {
    seed('GET', '/auth/me', { status: 401 });
    seed('POST', '/auth/refresh', {
      status: 200,
      setCookies: ['hemall_access_token=access-2; Path=/; HttpOnly', 'hemall_csrf_token=tok-2; Path=/'],
    });
    seed('GET', '/auth/me', { status: 200 });
    stubBackend();

    const res = await middleware(adminRequest(VALID));
    expect(res.status).toBe(200);
    expect(setCookiesOf(res)).toEqual([
      'hemall_access_token=access-2; Path=/; HttpOnly',
      'hemall_csrf_token=tok-2; Path=/',
    ]);

    // 第三次请求必须带上合并后的新 cookie
    expect(calls[2].cookie).toContain('hemall_access_token=access-2');
    expect(calls[2].cookie).toContain('hemall_refresh_token=refresh-1');
  });

  it('续期失败 → 302 /login 并清掉三个会话 cookie', async () => {
    seed('GET', '/auth/me', { status: 401 });
    seed('POST', '/auth/refresh', {
      status: 401,
      setCookies: ['hemall_access_token=; Path=/; Max-Age=0'],
    });
    stubBackend();

    const res = await middleware(adminRequest(VALID));
    expect(res.status).toBe(307);
    const cleared = setCookiesOf(res).map((sc) => sc.split(';')[0].trim());
    expect(cleared).toContain('hemall_access_token=');
    expect(cleared).toContain('hemall_refresh_token=');
    expect(cleared).toContain('hemall_csrf_token=');
    expect(cleared.every((c) => c.endsWith('='))).toBe(true);
  });

  it('续期成功但 /auth/me 仍 401（customer 会话误入）→ 跳登录但不清 cookie', async () => {
    seed('GET', '/auth/me', { status: 401 });
    seed('POST', '/auth/refresh', { status: 200, setCookies: ['hemall_access_token=cus; Path=/; HttpOnly'] });
    seed('GET', '/auth/me', { status: 401 });
    stubBackend();

    const res = await middleware(adminRequest(VALID));
    expect(res.status).toBe(307);
    const cleared = setCookiesOf(res).filter((sc) => /Max-Age=0/i.test(sc));
    expect(cleared).toHaveLength(0);
  });

  it('后端不可达 → 跳登录但不清 cookie', async () => {
    seed('GET', '/auth/me', { throws: true });
    stubBackend();

    const res = await middleware(adminRequest(VALID));
    expect(res.status).toBe(307);
    expect(setCookiesOf(res)).toHaveLength(0);
  });

  it('续期时后端不可达 → 跳登录但不清 cookie', async () => {
    seed('GET', '/auth/me', { status: 401 });
    seed('POST', '/auth/refresh', { throws: true });
    stubBackend();

    const res = await middleware(adminRequest(VALID));
    expect(res.status).toBe(307);
    expect(setCookiesOf(res)).toHaveLength(0);
  });

  it('续期后重查 /auth/me 时后端不可达 → 跳登录但不清 cookie', async () => {
    seed('GET', '/auth/me', { status: 401 });
    seed('POST', '/auth/refresh', { status: 200, setCookies: ['hemall_access_token=a2; Path=/'] });
    seed('GET', '/auth/me', { throws: true });
    stubBackend();

    const res = await middleware(adminRequest(VALID));
    expect(res.status).toBe(307);
    expect(setCookiesOf(res)).toHaveLength(0);
  });

  it('请求 cookie 原样转发给后端（HttpOnly 会话由服务端保管）', async () => {
    seed('GET', '/auth/me', { status: 200 });
    stubBackend();
    await middleware(adminRequest({ ...VALID, hemall_csrf_token: 'tok-1' }));
    expect(calls[0].cookie).toContain('hemall_access_token=access-1');
    expect(calls[0].cookie).toContain('hemall_refresh_token=refresh-1');
    expect(calls[0].cookie).toContain('hemall_csrf_token=tok-1');
  });
});
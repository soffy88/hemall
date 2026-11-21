/** lib/customer-auth-store 单测（audit 核验）。
 *
 * 顾客侧同构不变量：
 *  - 会话只来自 HttpOnly cookie（/api/auth/customer/me），令牌不落任何本地存储；
 *  - 写操作（登录/注册/登出）带 CSRF 双提交头 —— 后端 customer 路径不在豁免名单里；
 *  - initCustomerAuth 并发去重，失败可重试。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const calls: Array<{ url: string; init: RequestInit }> = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch(handler: (url: string, init: RequestInit) => Response) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    calls.push({ url, init: init ?? {} });
    return handler(url, init ?? {});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function headerOf(call: { init: RequestInit }, name: string): string | undefined {
  const headers = (call.init.headers ?? {}) as Record<string, string>;
  return Object.entries(headers).find(([k]) => k.toLowerCase() === name.toLowerCase())?.[1];
}

function clearCsrfCookie() {
  document.cookie = 'hemall_csrf_token=; Max-Age=0; path=/';
}

async function freshStore() {
  vi.resetModules();
  return import('@/lib/customer-auth-store');
}

beforeEach(() => {
  calls.length = 0;
  window.localStorage.clear();
  window.sessionStorage.clear();
  clearCsrfCookie();
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('customer-auth-store 会话状态', () => {
  it('登录成功从响应取 customer_id/email，不落本地存储', async () => {
    document.cookie = 'hemall_csrf_token=tok; path=/';
    stubFetch(() => jsonResponse({ customer_id: 'c-1', email: 'buyer@hemal.local', access_token: 'leak' }));
    const store = await freshStore();

    await expect(store.customerLogin('buyer@hemal.local', 'pw')).resolves.toEqual({
      customerId: 'c-1',
      email: 'buyer@hemal.local',
    });
    expect(store.isCustomerAuthenticated()).toBe(true);
    expect(store.getCustomerAuth()).toEqual({ customerId: 'c-1', email: 'buyer@hemal.local' });
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(JSON.stringify(calls)).not.toContain('leak');
  });

  it('登录走同源 /api 且带 credentials: include', async () => {
    document.cookie = 'hemall_csrf_token=tok; path=/';
    const fetchMock = stubFetch(() => jsonResponse({ customer_id: 'c-1', email: 'a@b.c' }));
    const store = await freshStore();
    await store.customerLogin('a@b.c', 'pw');

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/auth/customer/login');
    expect(init.credentials).toBe('include');
    expect(JSON.stringify(init.headers)).not.toMatch(/authorization/i);
  });

  it('登录失败抛后端 detail 并保持未登录', async () => {
    document.cookie = 'hemall_csrf_token=tok; path=/';
    stubFetch(() => jsonResponse({ detail: '账号或密码错误' }, 401));
    const store = await freshStore();
    await expect(store.customerLogin('a@b.c', 'bad')).rejects.toThrow('账号或密码错误');
    expect(store.isCustomerAuthenticated()).toBe(false);
  });

  it('CSRF cookie 缺失时先取 token 再带上 X-CSRF-Token', async () => {
    clearCsrfCookie();
    stubFetch((url) =>
      url.endsWith('/csrf-token')
        ? jsonResponse({ csrf_token: 'tok-fresh' })
        : jsonResponse({ customer_id: 'c-2', email: 'a@b.c' }),
    );
    const store = await freshStore();
    await store.customerLogin('a@b.c', 'pw');

    expect(calls[0].url).toBe('/api/auth/csrf-token');
    expect(headerOf(calls[1], 'X-CSRF-Token')).toBe('tok-fresh');
  });

  it('取 CSRF token 失败时不阻塞登录', async () => {
    clearCsrfCookie();
    stubFetch((url) =>
      url.endsWith('/csrf-token')
        ? new Response(null, { status: 500 })
        : jsonResponse({ customer_id: 'c-2', email: 'a@b.c' }),
    );
    const store = await freshStore();
    await expect(store.customerLogin('a@b.c', 'pw')).resolves.toMatchObject({ customerId: 'c-2' });
    expect(headerOf(calls[1], 'X-CSRF-Token')).toBeUndefined();
  });

  it('注册同样带 CSRF 头并写入状态', async () => {
    document.cookie = 'hemall_csrf_token=tok; path=/';
    stubFetch(() => jsonResponse({ customer_id: 'c-3', email: 'new@hemal.local' }));
    const store = await freshStore();

    await expect(
      store.customerRegister({ email: 'new@hemal.local', password: 'pw', phone: '13800000000' }),
    ).resolves.toEqual({ customerId: 'c-3', email: 'new@hemal.local' });

    const call = calls[calls.length - 1];
    expect(call.url).toBe('/api/auth/customer/register');
    expect(headerOf(call, 'X-CSRF-Token')).toBe('tok');
    expect(store.isCustomerAuthenticated()).toBe(true);
  });

  it('注册失败抛 detail 并保持未登录', async () => {
    document.cookie = 'hemall_csrf_token=tok; path=/';
    stubFetch(() => jsonResponse({ detail: '邮箱已被注册' }, 409));
    const store = await freshStore();
    await expect(store.customerRegister({ email: 'a@b.c', password: 'pw' })).rejects.toThrow('邮箱已被注册');
    expect(store.isCustomerAuthenticated()).toBe(false);
  });

  it('initCustomerAuth 并发去重，成功后缓存', async () => {
    stubFetch(() => jsonResponse({ customer_id: 'c-4', email: 'me@hemal.local' }));
    const store = await freshStore();

    await Promise.all([store.initCustomerAuth(), store.initCustomerAuth()]);
    expect(calls.map((c) => c.url)).toEqual(['/api/auth/customer/me']);
    expect(store.getCustomerAuth()).toEqual({ customerId: 'c-4', email: 'me@hemal.local' });
    expect(store.isCustomerAuthLoading()).toBe(false);
  });

  it('initCustomerAuth 401 → 未登录并允许重试', async () => {
    let attempt = 0;
    stubFetch(() => {
      attempt += 1;
      return attempt === 1 ? jsonResponse({}, 401) : jsonResponse({ customer_id: 'c-5', email: 'z@hemal.local' });
    });
    const store = await freshStore();

    await store.initCustomerAuth();
    expect(store.getCustomerAuth()).toBeNull();
    await store.initCustomerAuth();
    expect(store.getCustomerAuth()).toEqual({ customerId: 'c-5', email: 'z@hemal.local' });
  });

  it('initCustomerAuth 网络异常退回未登录', async () => {
    stubFetch(() => {
      throw new Error('offline');
    });
    const store = await freshStore();
    await expect(store.initCustomerAuth()).resolves.toBeUndefined();
    expect(store.isCustomerAuthenticated()).toBe(false);
  });

  it('登出清空状态并通知订阅者', async () => {
    document.cookie = 'hemall_csrf_token=tok; path=/';
    stubFetch((url) =>
      url.endsWith('/customer/logout') ? new Response(null, { status: 200 }) : jsonResponse({ customer_id: 'c-1', email: 'a@b.c' }),
    );
    const store = await freshStore();
    await store.customerLogin('a@b.c', 'pw');

    let notified = 0;
    store.subscribeCustomerAuth(() => {
      notified += 1;
    });
    await store.customerLogout();

    expect(notified).toBe(1);
    expect(store.isCustomerAuthenticated()).toBe(false);
    expect(calls[calls.length - 1].url).toBe('/api/auth/customer/logout');
  });
});
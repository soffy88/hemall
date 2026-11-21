/** lib/auth-store 单测（audit 核验：前端需有 vitest 单测）。
 *
 * 核心不变量（F-01/F-02）：
 *  - 状态只来自 HttpOnly cookie 会话（GET /api/auth/me），不来自 localStorage 令牌；
 *  - 任何路径都不得把令牌写进 localStorage；
 *  - 所有请求走同源 /api + credentials:'include'；
 *  - initAuth 并发去重，失败后可重试。
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

type Listener = () => void;

const meCalls: string[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

function stubFetch(handler: (url: string, init: RequestInit) => Response) {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const url = String(input);
    meCalls.push(url);
    return handler(url, init ?? {});
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

async function freshStore() {
  vi.resetModules();
  return import('@/lib/auth-store');
}

beforeEach(() => {
  meCalls.length = 0;
  window.localStorage.clear();
  window.sessionStorage.clear();
  document.cookie = 'hemall_csrf_token=; Max-Age=0; path=/';
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('auth-store 会话状态', () => {
  it('登录成功从后端响应取 user_id/email，不落任何本地存储', async () => {
    stubFetch((url) =>
      url.endsWith('/auth/login')
        ? jsonResponse({ user_id: 'u-1', email: 'admin@hemal.local', access_token: 'must-not-leak' })
        : jsonResponse({ detail: 'not found' }, 404),
    );
    const store = await freshStore();

    const outcome = await store.login('admin@hemal.local', 'pw');
    expect(outcome).toEqual({
      kind: 'ok',
      user: { id: 'u-1', email: 'admin@hemal.local' },
    });
    expect(store.isAuthenticated()).toBe(true);
    expect(store.getAuth()).toEqual({ id: 'u-1', email: 'admin@hemal.local' });

    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
    expect(JSON.stringify(meCalls)).not.toContain('must-not-leak');
  });

  it('登录请求走同源 /api 且带 credentials: include', async () => {
    const fetchMock = stubFetch(() => jsonResponse({ user_id: 'u-1', email: 'a@b.c' }));
    const store = await freshStore();
    await store.login('a@b.c', 'pw');

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/auth/login');
    expect(init.credentials).toBe('include');
    expect(init.method).toBe('POST');
    expect(String(init.body)).toBe(JSON.stringify({ email: 'a@b.c', password: 'pw' }));
    expect(JSON.stringify(init.headers)).not.toMatch(/authorization/i);
  });

  it('登录失败抛后端 detail，且保持未登录', async () => {
    stubFetch(() => jsonResponse({ detail: '邮箱或密码错误' }, 401));
    const store = await freshStore();
    await expect(store.login('a@b.c', 'bad')).rejects.toThrow('邮箱或密码错误');
    expect(store.isAuthenticated()).toBe(false);
    expect(store.getAuth()).toBeNull();
  });

  it('登录失败且响应体不是 JSON 时退回 statusText', async () => {
    stubFetch(() => new Response('<html>502</html>', { status: 502, statusText: 'Bad Gateway' }));
    const store = await freshStore();
    await expect(store.login('a@b.c', 'pw')).rejects.toThrow('Bad Gateway');
  });

  it('initAuth 只发一次 /auth/me（并发去重），成功后复用', async () => {
    stubFetch(() => jsonResponse({ user_id: 'u-9', email: 'ops@hemal.local' }));
    const store = await freshStore();

    await Promise.all([store.initAuth(), store.initAuth(), store.initAuth()]);
    expect(meCalls).toEqual(['/api/auth/me']);
    expect(store.isAuthenticated()).toBe(true);
    expect(store.isAuthLoading()).toBe(false);

    await store.initAuth();
    expect(meCalls).toEqual(['/api/auth/me']);
  });

  it('initAuth 401 → 未登录、清除去重缓存以便重试', async () => {
    let attempt = 0;
    stubFetch(() => {
      attempt += 1;
      return attempt === 1
        ? jsonResponse({ detail: 'not logged in' }, 401)
        : jsonResponse({ user_id: 'u-2', email: 'later@hemal.local' });
    });
    const store = await freshStore();

    await store.initAuth();
    expect(store.isAuthenticated()).toBe(false);
    expect(store.isAuthLoading()).toBe(false);

    await store.initAuth();
    expect(store.isAuthenticated()).toBe(true);
    expect(meCalls).toEqual(['/api/auth/me', '/api/auth/me']);
  });

  it('initAuth 网络异常也不炸，退回未登录', async () => {
    stubFetch(() => {
      throw new Error('ECONNREFUSED');
    });
    const store = await freshStore();
    await expect(store.initAuth()).resolves.toBeUndefined();
    expect(store.isAuthenticated()).toBe(false);
  });

  it('logout 清空状态并通知订阅者', async () => {
    stubFetch((url) =>
      url.endsWith('/auth/logout') ? new Response(null, { status: 200 }) : jsonResponse({ user_id: 'u-1', email: 'a@b.c' }),
    );
    const store = await freshStore();
    await store.login('a@b.c', 'pw');

    let notified = 0;
    const off: Listener = store.subscribeAuth(() => {
      notified += 1;
    });
    await store.logout();
    expect(notified).toBe(1);

    off();
    await store.logout();
    expect(notified).toBe(1); // 已退订，不再收到通知
    expect(store.isAuthenticated()).toBe(false);
  });

  it('subscribeAuth 返回的退订函数生效', async () => {
    stubFetch(() => jsonResponse({ user_id: 'u-1', email: 'a@b.c' }));
    const store = await freshStore();
    let notified = 0;
    const off = store.subscribeAuth(() => {
      notified += 1;
    });
    store.clearAuth();
    off();
    store.clearAuth();
    expect(notified).toBe(1);
  });

  it('refreshAuth 成功后重查 me 得到用户', async () => {
    let refreshed = false;
    stubFetch((url) => {
      if (url.endsWith('/auth/refresh')) {
        refreshed = true;
        return new Response(null, { status: 200 });
      }
      return jsonResponse({ user_id: 'u-7', email: 'r@hemal.local' });
    });
    const store = await freshStore();

    await expect(store.refreshAuth()).resolves.toBe(true);
    expect(refreshed).toBe(true);
    expect(store.getAuth()).toEqual({ id: 'u-7', email: 'r@hemal.local' });
  });

  it('refreshAuth 401 → false 且保持未登录', async () => {
    stubFetch(() => jsonResponse({ detail: 'refresh expired' }, 401));
    const store = await freshStore();
    await expect(store.refreshAuth()).resolves.toBe(false);
    expect(store.isAuthenticated()).toBe(false);
  });
});
describe('auth-store TOTP 登录第二步 / 首次绑定', () => {
  it('requires_totp → 返回 totp 挑战且不建会话', async () => {
    stubFetch((url) =>
      url.endsWith('/auth/login')
        ? jsonResponse({
            requires_totp: true,
            user_id: 'u-5',
            email: 'admin@hemal.local',
            totp_token: 'challenge-1',
            message: 'TOTP verification required',
          })
        : jsonResponse({ detail: 'not found' }, 404),
    );
    const store = await freshStore();

    const outcome = await store.login('admin@hemal.local', 'pw');
    expect(outcome).toEqual({
      kind: 'totp',
      challenge: {
        requires_totp: true,
        user_id: 'u-5',
        email: 'admin@hemal.local',
        totp_token: 'challenge-1',
        message: 'TOTP verification required',
      },
    });
    expect(store.isAuthenticated()).toBe(false);
    expect(store.getAuth()).toBeNull();
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);
  });

  it('requires_totp_setup → 返回 setup 挑战并暂存（只在内存，不落本地存储）', async () => {
    stubFetch(() =>
      jsonResponse({
        requires_totp_setup: true,
        user_id: 'u-5',
        email: 'admin@hemal.local',
        setup_token: 'setup-1',
        message: 'TOTP enrollment required for privileged accounts in production',
      }),
    );
    const store = await freshStore();

    const outcome = await store.login('admin@hemal.local', 'pw');
    expect(outcome.kind).toBe('setup');
    expect(store.getPendingSetup()).toMatchObject({ setup_token: 'setup-1', user_id: 'u-5' });
    expect(store.isAuthenticated()).toBe(false);
    expect(window.localStorage.length).toBe(0);
    expect(window.sessionStorage.length).toBe(0);

    store.clearPendingSetup();
    expect(store.getPendingSetup()).toBeNull();
  });

  it('verifyTotp 带 challenge 令牌 → Authorization 头（CSRF 豁免），成功建会话', async () => {
    const fetchMock = stubFetch((url) =>
      url.endsWith('/auth/totp/verify')
        ? jsonResponse({ user_id: 'u-5', email: 'admin@hemal.local', access_token: 'must-not-leak' })
        : jsonResponse({ detail: 'not found' }, 404),
    );
    const store = await freshStore();

    const user = await store.verifyTotp('u-5', { code: '123456' }, 'challenge-1');
    expect(user).toEqual({ id: 'u-5', email: 'admin@hemal.local' });
    expect(store.getAuth()).toEqual({ id: 'u-5', email: 'admin@hemal.local' });

    const [url, init] = fetchMock.mock.calls[0] as [string, RequestInit];
    expect(url).toBe('/api/auth/totp/verify');
    expect(init.method).toBe('POST');
    expect(init.credentials).toBe('include');
    expect((init.headers as Record<string, string>).Authorization).toBe('Bearer challenge-1');
    expect(String(init.body)).toBe(JSON.stringify({ user_id: 'u-5', code: '123456' }));
    expect(JSON.stringify(init.headers)).not.toMatch(/x-csrf-token/i);
    expect(JSON.stringify(init.headers)).not.toContain('must-not-leak');
  });

  it('verifyTotp 无 challenge 令牌 → 先取 csrf-token 再带 X-CSRF-Token 头', async () => {
    const fetchMock = stubFetch((url) =>
      url.endsWith('/auth/csrf-token')
        ? jsonResponse({ csrf_token: 'tok-42' })
        : jsonResponse({ user_id: 'u-5', email: 'admin@hemal.local' }),
    );
    const store = await freshStore();

    await store.verifyTotp('u-5', { recovery_code: 'ABC123' });
    expect(store.isAuthenticated()).toBe(true);

    const calls = fetchMock.mock.calls.map(([input]) => String(input));
    expect(calls).toEqual(['/api/auth/csrf-token', '/api/auth/totp/verify']);
    const [, init] = fetchMock.mock.calls[1] as [string, RequestInit];
    const headers = init.headers as Record<string, string>;
    expect(headers['X-CSRF-Token']).toBe('tok-42');
    expect(headers.Authorization).toBeUndefined();
    expect(String(init.body)).toBe(JSON.stringify({ user_id: 'u-5', recovery_code: 'ABC123' }));
  });

  it('verifyTotp 失败且 detail 是 422 数组 → 退回默认提示并保持未登录', async () => {
    stubFetch(() => jsonResponse({ detail: [{ msg: 'invalid code' }] }, 422));
    const store = await freshStore();

    await expect(store.verifyTotp('u-5', { code: '000000' })).rejects.toThrow('验证码校验失败');
    expect(store.isAuthenticated()).toBe(false);
  });

  it('fetchTotpSetup / bindTotp 走同源 /api 并带 setup_token Authorization', async () => {
    document.cookie = 'hemall_csrf_token=tok; path=/';
    const fetchMock = stubFetch((url) => {
      if (url.endsWith('/auth/totp/setup')) {
        return jsonResponse({ otpauth_uri: 'otpauth://totp/x', secret: 'JBSW', recovery_codes: ['a1'] });
      }
      if (url.endsWith('/auth/totp/bind')) {
        return jsonResponse({ message: 'TOTP bound', recovery_codes: ['r1'] });
      }
      return jsonResponse({ detail: 'not found' }, 404);
    });
    const store = await freshStore();

    const info = await store.fetchTotpSetup('setup-1');
    expect(info.secret).toBe('JBSW');
    const confirm = await store.bindTotp('setup-1', 'JBSW', '123456');
    expect(confirm.recovery_codes).toEqual(['r1']);

    const calls = fetchMock.mock.calls.map(([input]) => String(input));
    expect(calls).toEqual(['/api/auth/totp/setup', '/api/auth/totp/bind']);
    const setupInit = fetchMock.mock.calls[0][1] as RequestInit;
    const bindInit = fetchMock.mock.calls[1][1] as RequestInit;
    expect((setupInit.headers as Record<string, string>).Authorization).toBe('Bearer setup-1');
    const bindHeaders = bindInit.headers as Record<string, string>;
    expect(bindHeaders.Authorization).toBe('Bearer setup-1');
    expect(bindHeaders['X-TOTP-Secret']).toBe('JBSW');
    expect(bindInit.method).toBe('POST');
    expect(JSON.stringify(setupInit.headers)).not.toMatch(/x-csrf-token/i);
  });
});

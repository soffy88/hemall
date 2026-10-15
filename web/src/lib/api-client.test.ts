/** lib/api-client 单测（audit 核验：前端需有 vitest 单测）。
 *
 * 覆盖 F-01/F-02/F-03/F-04/F-05 的前端不变量：
 *  - 一律走同源 /api 路径，不出现绝对后端地址（F-05）
 *  - 不读 localStorage 取令牌，改 credentials:'include'（F-02）
 *  - 写请求带 X-CSRF-Token 双提交头（F-03）
 *  - HTTP 200 的业务失败（ok:false / status:'failed' / error）必须抛错（F-04）
 *  - 401 自动续期一次，仍 401 广播 hemall:unauthorized
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { api, deviceId } from '@/lib/api-client';

type FetchCall = { url: string; init: RequestInit };

const calls: FetchCall[] = [];

function jsonResponse(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'Content-Type': 'application/json' },
  });
}

/** 排队式 fetch stub：按顺序返回，未耗尽则回落到 200 {}。 */
function stubFetch(responses: Array<Response | (() => Response | Promise<Response>)>) {
  let i = 0;
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    calls.push({ url: String(input), init: init ?? {} });
    const item = responses[Math.min(i, responses.length - 1)];
    i += 1;
    if (typeof item === 'function') return item();
    return (item ?? jsonResponse({})).clone();
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function headerOf(call: FetchCall, name: string): string | undefined {
  const headers = (call.init.headers ?? {}) as Record<string, string>;
  return Object.entries(headers).find(([k]) => k.toLowerCase() === name.toLowerCase())?.[1];
}

beforeEach(() => {
  calls.length = 0;
  window.localStorage.clear();
  document.cookie = 'hemall_csrf_token=; Max-Age=0; path=/';
});

afterEach(() => {
  vi.unstubAllGlobals();
});

describe('同源与凭据（F-01/F-02/F-05）', () => {
  it('请求地址始终是同源 /api 前缀，绝不出现后端绝对地址', async () => {
    stubFetch([jsonResponse({ ok: true })]);
    await api.health();
    expect(calls[0].url).toBe('/api/health');
  });

  it('每个请求都带 credentials: include（HttpOnly cookie 自动附带）', async () => {
    stubFetch([jsonResponse({ ok: true })]);
    await api.health();
    expect(calls[0].init.credentials).toBe('include');
  });

  it('不再从 localStorage 读取任何令牌', async () => {
    window.localStorage.setItem('hemall_auth', JSON.stringify({ token: 'leaked-jwt' }));
    window.localStorage.setItem('hemall_customer_auth', JSON.stringify({ token: 'leaked-cus' }));
    stubFetch([jsonResponse({ ok: true })]);
    await api.health();
    const serialized = JSON.stringify(calls[0]);
    expect(serialized).not.toContain('leaked-jwt');
    expect(serialized).not.toContain('leaked-cus');
    expect(serialized.toLowerCase()).not.toContain('authorization');
    // localStorage 里不得被回写令牌
    expect(window.localStorage.getItem('hemall_auth')).toBe(JSON.stringify({ token: 'leaked-jwt' }));
  });

  it('device_id 仍走 localStorage（唯一允许的持久化键）', () => {
    vi.stubGlobal('fetch', vi.fn());
    const first = deviceId();
    expect(first).toMatch(/^dev-/);
    expect(window.localStorage.getItem('hemall_device_id')).toBe(first);
    expect(deviceId()).toBe(first); // 稳定复用
  });
});

describe('CSRF 双提交（F-03）', () => {
  it('写请求自动带上 cookie 里的 X-CSRF-Token', async () => {
    document.cookie = 'hemall_csrf_token=tok-123; path=/';
    stubFetch([jsonResponse({ ok: true })]);
    await api.createProduct({ title: '土豆', slug: 'potato' });
    const write = calls[calls.length - 1];
    expect(headerOf(write, 'X-CSRF-Token')).toBe('tok-123');
  });

  it('cookie 缺失时先取 /api/auth/csrf-token 再重放', async () => {
    stubFetch([
      jsonResponse({ csrf_token: 'tok-456' }),
      jsonResponse({ ok: true }),
    ]);
    await api.createProduct({ title: '土豆', slug: 'potato' });
    expect(calls[0].url).toBe('/api/auth/csrf-token');
    expect(calls[0].init.credentials).toBe('include');
    expect(headerOf(calls[1], 'X-CSRF-Token')).toBe('tok-456');
  });

  it('取 token 失败时不阻塞请求，只是不带 CSRF 头', async () => {
    stubFetch([
      () => { throw new Error('network down'); },
      jsonResponse({ ok: true }),
    ]);
    await api.createProduct({ title: '土豆', slug: 'potato' });
    expect(headerOf(calls[1], 'X-CSRF-Token')).toBeUndefined();
  });

  it('GET 请求不触发 CSRF 取 token', async () => {
    stubFetch([jsonResponse([])]);
    await api.adminListProducts({ status: 'active', limit: 10 });
    expect(calls.some((c) => c.url.includes('csrf-token'))).toBe(false);
  });
});

describe('业务失败识别（F-04）', () => {
  it.each([
    ['ok:false + error.message', { ok: false, error: { message: '库存不足' } }, '库存不足'],
    ['ok:false 只有 error.type', { ok: false, error: { type: 'CONFLICT' } }, 'CONFLICT'],
    ['ok:false 全空', { ok: false }, '业务处理失败'],
    ["status:'failed'", { status: 'failed', error: { message: '下单失败' } }, '下单失败'],
    ["status:'cancelled'", { status: 'cancelled' }, '业务处理已取消（cancelled）'],
    ['裸 error 对象', { error: { message: '支付网关超时' } }, '支付网关超时'],
  ])('%s 必须抛错', async (_name, body, expected) => {
    stubFetch([jsonResponse(body)]);
    await expect(api.createProduct({ title: 'x', slug: 'y' })).rejects.toThrow(expected);
  });

  it('数组响应不被误判为业务失败', async () => {
    stubFetch([jsonResponse([{ ok: false, id: 'p1' }])]);
    await expect(api.adminListProducts()).resolves.toEqual([{ ok: false, id: 'p1' }]);
  });

  it('正常成功体原样返回', async () => {
    stubFetch([jsonResponse({ ok: true, id: 'p1' })]);
    await expect(api.createProduct({ title: 'x', slug: 'y' })).resolves.toEqual({ ok: true, id: 'p1' });
  });
});

describe('HTTP 错误消息（F-04）', () => {
  it('detail 字符串直接透出', async () => {
    stubFetch([jsonResponse({ detail: '未授权' }, 403)]);
    await expect(api.adminListProducts()).rejects.toThrow('未授权');
  });

  it('FastAPI 422 的 detail 对象数组被拍平', async () => {
    stubFetch([
      jsonResponse({ detail: [{ msg: 'field required' }, { msg: 'bad type' }] }, 422),
    ]);
    await expect(api.adminListProducts()).rejects.toThrow('field required; bad type');
  });

  it('取不到消息时退回 HTTP 状态码', async () => {
    stubFetch([jsonResponse({}, 500)]);
    await expect(api.adminListProducts()).rejects.toThrow('HTTP 500');
  });
});

describe('401 自动续期', () => {
  it('admin 请求 401 → 续期 → 重放一次成功', async () => {
    const events: string[] = [];
    const listener = () => events.push('unauthorized');
    window.addEventListener('hemall:unauthorized', listener);
    try {
      stubFetch([
        jsonResponse({ detail: 'expired' }, 401),
        jsonResponse({ ok: true }),
        jsonResponse([{ id: 'o1' }]),
      ]);
      await expect(api.adminListOrders()).resolves.toEqual([{ id: 'o1' }]);
      expect(calls.map((c) => c.url)).toEqual([
        '/api/admin/orders?',
        '/api/auth/refresh',
        '/api/admin/orders?',
      ]);
      expect(calls[1].init.method).toBe('POST');
      expect(events).toEqual([]);
    } finally {
      window.removeEventListener('hemall:unauthorized', listener);
    }
  });

  it('续期成功但重放仍 401 → 广播未登录', async () => {
    const seen: unknown[] = [];
    const listener = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener('hemall:unauthorized', listener);
    try {
      stubFetch([
        jsonResponse({ detail: 'expired' }, 401),
        jsonResponse({ ok: true }),
        jsonResponse({ detail: 'expired' }, 401),
      ]);
      await expect(api.adminListOrders()).rejects.toThrow('expired');
      expect(seen).toEqual([{ kind: 'staff' }]);
    } finally {
      window.removeEventListener('hemall:unauthorized', listener);
    }
  });

  it('续期失败 → 直接抛"登录已过期"并广播，不重放', async () => {
    const seen: unknown[] = [];
    const listener = (e: Event) => seen.push((e as CustomEvent).detail);
    window.addEventListener('hemall:unauthorized', listener);
    try {
      stubFetch([jsonResponse({ detail: 'expired' }, 401), jsonResponse({ detail: 'no refresh' }, 401)]);
      await expect(api.adminListOrders()).rejects.toThrow('登录已过期，请重新登录');
      expect(calls).toHaveLength(2);
      expect(seen).toEqual([{ kind: 'staff' }]);
    } finally {
      window.removeEventListener('hemall:unauthorized', listener);
    }
  });

  it('匿名端点 401 不触发续期', async () => {
    stubFetch([jsonResponse({ detail: 'nope' }, 401)]);
    await expect(api.health()).rejects.toThrow('nope');
    expect(calls).toHaveLength(1);
  });
});

describe('退款状态机端点（/aftersales/refunds*）', () => {
  it('顾客申请售后：POST 同源路径 + CSRF 头 + 请求体原样', async () => {
    document.cookie = 'hemall_csrf_token=tok-rf; path=/';
    stubFetch([jsonResponse({ id: 'rf1', status: 'submitted' })]);
    await api.createRefund({
      order_id: 'o1',
      requested_amount: 1999,
      reason: 'product_issue',
      reason_detail: '坏了',
    });
    const call = calls[calls.length - 1];
    expect(call.url).toBe('/api/aftersales/refunds');
    expect(call.init.method).toBe('POST');
    expect(headerOf(call, 'X-CSRF-Token')).toBe('tok-rf');
    expect(JSON.parse(String(call.init.body))).toEqual({
      order_id: 'o1',
      requested_amount: 1999,
      reason: 'product_issue',
      reason_detail: '坏了',
    });
  });

  it('我的售后：GET /my（顾客会话），不做 CSRF 取 token', async () => {
    stubFetch([jsonResponse([])]);
    await expect(api.customerMyRefunds({ limit: 20 })).resolves.toEqual([]);
    expect(calls[0].url).toBe('/api/aftersales/refunds/my?limit=20');
    expect(calls.some((c) => c.url.includes('csrf-token'))).toBe(false);
  });

  it('后台列表：status/order_id 过滤拼进 query', async () => {
    stubFetch([jsonResponse([])]);
    await api.adminListRefunds({ status: 'submitted', order_id: 'o9' });
    expect(calls[0].url).toBe('/api/aftersales/refunds?status=submitted&order_id=o9');
  });

  it('通过 / 驳回 / 重试 / 收货 各自打到正确子路径', async () => {
    document.cookie = 'hemall_csrf_token=tok-rf; path=/';
    stubFetch([jsonResponse({ id: 'rf1' }), jsonResponse({ id: 'rf1' }), jsonResponse({ id: 'rf1' }), jsonResponse({ id: 'rf1' })]);
    await api.adminApproveRefund('rf1', { approved_amount: 500 });
    expect(calls[0].url).toBe('/api/aftersales/refunds/rf1/approve');
    expect(JSON.parse(String(calls[0].init.body))).toEqual({ approved_amount: 500 });

    await api.adminRejectRefund('rf1', '超出可退额度');
    expect(calls[1].url).toBe('/api/aftersales/refunds/rf1/reject');
    expect(JSON.parse(String(calls[1].init.body))).toEqual({ reject_reason: '超出可退额度' });

    await api.adminRetryRefund('rf1');
    expect(calls[2].url).toBe('/api/aftersales/refunds/rf1/retry');

    await api.adminReturnReceivedRefund('rf1', { tracking_no: 'SF1', batch_id: 'b1' });
    expect(calls[3].url).toBe('/api/aftersales/refunds/rf1/return-received');
    expect(JSON.parse(String(calls[3].init.body))).toEqual({ tracking_no: 'SF1', batch_id: 'b1' });
  });

  it('审计日志：GET audit，403 detail 透出（support 可看 / approve 需 admin）', async () => {
    stubFetch([jsonResponse({ detail: '需要 admin 角色' }, 403)]);
    await expect(api.adminRefundAudit('rf1')).rejects.toThrow('需要 admin 角色');
    expect(calls[0].url).toBe('/api/aftersales/refunds/rf1/audit');
  });

  it('refund id 需 URL 编码（避免 / 等字符破坏路径）', async () => {
    document.cookie = 'hemall_csrf_token=tok-rf; path=/';
    stubFetch([jsonResponse({ id: 'rf/x' })]);
    await api.adminRetryRefund('rf/x');
    expect(calls[0].url).toBe('/api/aftersales/refunds/rf%2Fx/retry');
  });
});
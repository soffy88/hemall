/** BFF Origin/Sec-Fetch-Site 同源核验单测（audit：经 Next BFF 的跨站 POST 曾返回 200）。
 *
 * 预览站实测：dev 下经 BFF 发 POST /api/agent/ingest，带 Origin:
 * https://evil.example 返回 200 —— 后端把 /agent/ 整段豁免了 CSRF，浏览器只能
 * 通过 BFF 进来，所以最后一道同源校验必须由 BFF 自己兜住。
 *
 * 覆盖：
 *  - 同源状态变更请求放行（Origin 与 BFF 自身 origin 一致 / Sec-Fetch-Site: same-origin）
 *  - 跨站状态变更请求 403，且不打后端
 *  - 无 Origin、无 Sec-Fetch-Site 的服务端请求（curl / 上游服务）按规则放行
 *  - 只对状态变更方法生效，GET 不受影响
 *  - init.method 覆盖 request.method 时同样按状态变更校验
 *  - HEMALL_BFF_ORIGINS 可追加放行来源（多域名部署）
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { NextRequest } from 'next/server';

import { proxyToBackend } from '@/lib/proxy';

const SITE = 'https://shop.hemall.test';

const calls: Array<{ url: string; method: string; origin: string | undefined }> = [];

function stubUpstream(status = 200, body = '{"ok":true}') {
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const headers = new Headers((init?.headers ?? {}) as HeadersInit);
    calls.push({
      url: String(input),
      method: (init?.method ?? 'GET').toUpperCase(),
      origin: headers.get('origin') ?? undefined,
    });
    return new Response(body, {
      status,
      headers: { 'content-type': 'application/json' },
    });
  });
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

function post(path: string, headers: Record<string, string> = {}, body = '{"a":1}') {
  return new NextRequest(new URL(`${SITE}${path}`), {
    method: 'POST',
    headers: { 'content-type': 'application/json', ...headers },
    body,
  });
}

beforeEach(() => {
  calls.length = 0;
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('BFF 同源核验：跨站状态变更请求 403', () => {
  it('同源 POST 放行并转发到后端', async () => {
    stubUpstream();
    const res = await proxyToBackend(post('/api/agent/ingest', { origin: SITE }), '/agent/ingest', {
      method: 'POST',
      body: '{"a":1}',
    });
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://backend.test/agent/ingest');
    expect(calls[0].origin).toBe(SITE);
  });

  it('跨站 POST (Origin: https://evil.example) 返回 403 且不打后端', async () => {
    const fetchMock = stubUpstream();
    const res = await proxyToBackend(
      post('/api/agent/ingest', { origin: 'https://evil.example' }),
      '/agent/ingest',
      { method: 'POST', body: '{"a":1}' },
    );
    expect(res.status).toBe(403);
    await expect(res.json()).resolves.toMatchObject({ detail: expect.stringContaining('CSRF') });
    expect(fetchMock).not.toHaveBeenCalled();
    expect(calls).toHaveLength(0);
  });

  it('Sec-Fetch-Site: cross-site 同样 403（无 Origin 的跨站 POST）', async () => {
    const fetchMock = stubUpstream();
    const res = await proxyToBackend(
      post('/api/agent/ingest', { 'sec-fetch-site': 'cross-site' }),
      '/agent/ingest',
      { method: 'POST', body: '{"a":1}' },
    );
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('Origin: null（沙箱 iframe / 隐私上下文）视为跨站 403', async () => {
    const fetchMock = stubUpstream();
    const res = await proxyToBackend(post('/api/agent/ingest', { origin: 'null' }), '/agent/ingest', {
      method: 'POST',
      body: '{"a":1}',
    });
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('PUT / PATCH / DELETE 跨站同样 403', async () => {
    for (const method of ['PUT', 'PATCH', 'DELETE']) {
      calls.length = 0;
      const fetchMock = stubUpstream();
      const request = new NextRequest(new URL(`${SITE}/api/agent/ingest?id=1`), {
        method,
        headers: { origin: 'https://evil.example' },
      });
      const res = await proxyToBackend(request, '/agent/ingest?id=1', { method });
      expect(res.status, method).toBe(403);
      expect(fetchMock).not.toHaveBeenCalled();
    }
  });

  it('Sec-Fetch-Site: same-origin 放行', async () => {
    stubUpstream();
    const res = await proxyToBackend(
      post('/api/agent/ingest', { 'sec-fetch-site': 'same-origin' }),
      '/agent/ingest',
      { method: 'POST', body: '{"a":1}' },
    );
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
  });

  it('HEMALL_BFF_ORIGINS 追加的来源放行（多域名部署）', async () => {
    vi.stubEnv('HEMALL_BFF_ORIGINS', 'https://mall.example.com, https://www.mall.example.com/');
    stubUpstream();
    const res = await proxyToBackend(
      post('/api/agent/ingest', { origin: 'https://mall.example.com' }),
      '/agent/ingest',
      { method: 'POST', body: '{"a":1}' },
    );
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
  });
});

describe('BFF 同源核验：合法流量不受影响', () => {
  it('无 Origin 无 Sec-Fetch-Site 的服务端请求放行（curl / 上游服务不属于 CSRF 向量）', async () => {
    stubUpstream();
    const res = await proxyToBackend(post('/api/agent/ingest'), '/agent/ingest', {
      method: 'POST',
      body: '{"a":1}',
    });
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
  });

  it('GET 带跨站 Origin 也放行（不改变状态）', async () => {
    stubUpstream();
    const request = new NextRequest(new URL(`${SITE}/api/health`), {
      method: 'GET',
      headers: { origin: 'https://evil.example' },
    });
    const res = await proxyToBackend(request, '/health');
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
  });

  it('init.method 覆盖成状态变更方法时同样校验（request 是 GET 也拒）', async () => {
    const fetchMock = stubUpstream();
    const request = new NextRequest(new URL(`${SITE}/api/probe`), {
      method: 'GET',
      headers: { origin: 'https://evil.example' },
    });
    const res = await proxyToBackend(request, '/probe', { method: 'POST' });
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('cookie / X-CSRF-Token 依旧透传给后端（同源 POST）', async () => {
    stubUpstream();
    await proxyToBackend(
      post('/api/auth/customer/login', {
        origin: SITE,
        cookie: 'hemall_csrf_token=tok-1',
        'x-csrf-token': 'tok-1',
      }),
      '/auth/customer/login',
      { method: 'POST', body: '{"a":1}' },
    );
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://backend.test/auth/customer/login');
  });
});

/** hemall 前端 — 顾客账号状态管理 (Cookie 会话方案，跟 auth-store.ts 同构)。

F-01/F-02/F-03: 移除 localStorage token 存储，改用后端签发的 HttpOnly Cookie 会话。
- 顾客登录/登出/获取用户信息均通过同源 /api 反代调用后端
- 前端不再直接接触 access/refresh token
*/

interface CustomerAuthState {
  customerId: string | null;
  email: string | null;
  isLoading: boolean;
}

let customerAuthState: CustomerAuthState = {
  customerId: null,
  email: null,
  isLoading: true,
};

const listeners: Set<() => void> = new Set();

/** initCustomerAuth 的在途/已成功 promise（并发去重）；失败时清掉以便重试。 */
let initPromise: Promise<void> | null = null;

function notify() {
  listeners.forEach((fn) => fn());
}

const CSRF_COOKIE_NAME = 'hemall_csrf_token';
const CSRF_HEADER_NAME = 'X-CSRF-Token';

function csrfCookie(): string | null {
  const m = document.cookie.match(new RegExp(`(?:^|;\\s*)${CSRF_COOKIE_NAME}=([^;]+)`));
  return m ? decodeURIComponent(m[1]) : null;
}

/** 生产环境后端要求 CSRF 双重提交，而 /auth/customer/* 不在豁免路径里：
 *  先取（或换发）非 HttpOnly 的 csrf cookie，再把同值塞进请求头。 */
async function csrfHeaders(): Promise<Record<string, string>> {
  let token = csrfCookie();
  if (!token) {
    try {
      const res = await fetch('/api/auth/csrf-token', { credentials: 'include' });
      if (res.ok) {
        const data = await res.json().catch(() => null);
        token = data?.csrf_token ?? csrfCookie();
      }
    } catch {
      token = null;
    }
  }
  return token ? { [CSRF_HEADER_NAME]: token } : {};
}

async function fetchCustomerMe(): Promise<{ customer_id: string; email: string } | null> {
  try {
    const res = await fetch('/api/auth/customer/me', {
      credentials: 'include',
    });
    if (!res.ok) return null;
    return await res.json();
  } catch {
    return null;
  }
}

export async function initCustomerAuth(): Promise<void> {
  // 并发去重：多个页面/组件同时挂载时只发一次 /auth/customer/me；
  // 成功后缓存结果，失败/未登录则清掉，下次挂载重试。
  if (!initPromise) {
    initPromise = (async () => {
      const data = await fetchCustomerMe();
      customerAuthState = {
        // 后端 CustomerMeResponse 是 snake_case
        customerId: data?.customer_id ?? null,
        email: data?.email ?? null,
        isLoading: false,
      };
      if (!data) initPromise = null;
      notify();
    })();
  }
  // 先取本地引用：失败分支会把 initPromise 置 null，届时 await 模块变量会立刻返回。
  const pending = initPromise;
  await pending;
}

export function getCustomerAuth(): { customerId: string; email: string } | null {
  if (!customerAuthState.customerId) return null;
  return { customerId: customerAuthState.customerId, email: customerAuthState.email! };
}

export function isCustomerAuthenticated(): boolean {
  return customerAuthState.customerId != null;
}

export function isCustomerAuthLoading(): boolean {
  return customerAuthState.isLoading;
}

export function subscribeCustomerAuth(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export async function customerLogin(email: string, password: string): Promise<{ customerId: string; email: string }> {
  const res = await fetch('/api/auth/customer/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(await csrfHeaders()) },
    credentials: 'include',
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || '登录失败');
  }
  const data = await res.json();
  customerAuthState = { customerId: data.customer_id, email: data.email, isLoading: false };
  notify();
  return { customerId: data.customer_id, email: data.email };
}

export async function customerRegister(data: { email: string; password: string; phone?: string; name?: string }): Promise<{ customerId: string; email: string }> {
  const res = await fetch('/api/auth/customer/register', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', ...(await csrfHeaders()) },
    credentials: 'include',
    body: JSON.stringify(data),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(body.detail || '注册失败');
  }
  const result = await res.json();
  customerAuthState = { customerId: result.customer_id, email: result.email, isLoading: false };
  notify();
  return { customerId: result.customer_id, email: result.email };
}

export async function customerLogout(): Promise<void> {
  await fetch('/api/auth/customer/logout', {
    method: 'POST',
    headers: await csrfHeaders(),
    credentials: 'include',
  });
  customerAuthState = { customerId: null, email: null, isLoading: false };
  notify();
}
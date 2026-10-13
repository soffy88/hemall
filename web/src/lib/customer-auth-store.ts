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

function notify() {
  listeners.forEach((fn) => fn());
}

async function fetchCustomerMe(): Promise<{ customerId: string; email: string } | null> {
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
  const data = await fetchCustomerMe();
  customerAuthState = {
    customerId: data?.customerId ?? null,
    email: data?.email ?? null,
    isLoading: false,
  };
  notify();
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
    headers: { 'Content-Type': 'application/json' },
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
    headers: { 'Content-Type': 'application/json' },
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
    credentials: 'include',
  });
  customerAuthState = { customerId: null, email: null, isLoading: false };
  notify();
}
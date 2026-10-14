/** hemall 前端 — 管理员认证状态管理 (Cookie 会话方案)。

F-01/F-02/F-03: 移除 localStorage token 存储，改用后端签发的 HttpOnly Cookie 会话。
- 登录/登出/刷新/获取用户信息均通过同源 /api 反代调用后端
- 前端不再直接接触 access/refresh token，仅在登录时获取用户信息用于 UI 展示
- 401 时自动尝试刷新或跳回登录页
*/

import type { AuthUser } from '@/types/api';

interface AuthState {
  user: AuthUser | null;
  isLoading: boolean;
}

let authState: AuthState = {
  user: null,
  isLoading: true,
};

const listeners: Set<() => void> = new Set();

/** initAuth 的在途/已成功 promise（并发去重）；失败时清掉以便重试。 */
let initPromise: Promise<void> | null = null;

function notify() {
  listeners.forEach((fn) => fn());
}

async function fetchMe(): Promise<AuthUser | null> {
  try {
    const res = await fetch('/api/auth/me', {
      credentials: 'include',
    });
    if (!res.ok) return null;
    const data = await res.json();
    return { id: data.user_id, email: data.email };
  } catch {
    return null;
  }
}

export async function initAuth(): Promise<void> {
  // 并发去重：多处守卫同时挂载只发一次 /auth/me；未登录则清缓存以便重试。
  if (!initPromise) {
    initPromise = (async () => {
      const user = await fetchMe();
      authState = { user, isLoading: false };
      if (!user) initPromise = null;
      notify();
    })();
  }
  // 先取本地引用：失败分支会把 initPromise 置 null，届时 await 模块变量会立刻返回。
  const pending = initPromise;
  await pending;
}

export function getAuth(): AuthUser | null {
  return authState.user;
}

export function isAuthenticated(): boolean {
  return authState.user != null;
}

export function isAuthLoading(): boolean {
  return authState.isLoading;
}

export function subscribeAuth(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export async function login(email: string, password: string): Promise<AuthUser> {
  const res = await fetch('/api/auth/login', {
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
  const user: AuthUser = { id: data.user_id, email: data.email };
  authState = { user, isLoading: false };
  notify();
  return user;
}

export async function logout(): Promise<void> {
  await fetch('/api/auth/logout', {
    method: 'POST',
    credentials: 'include',
  });
  authState = { user: null, isLoading: false };
  notify();
}

export function clearAuth(): void {
  authState = { user: null, isLoading: false };
  notify();
}

export async function refreshAuth(): Promise<boolean> {
  try {
    const res = await fetch('/api/auth/refresh', {
      method: 'POST',
      credentials: 'include',
    });
    if (!res.ok) return false;
    const user = await fetchMe();
    authState = { user, isLoading: false };
    notify();
    return true;
  } catch {
    return false;
  }
}
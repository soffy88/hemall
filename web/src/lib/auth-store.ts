/** hemall 前端 — 管理员认证状态管理 (Cookie 会话方案)。

F-01/F-02/F-03: 移除 localStorage token 存储，改用后端签发的 HttpOnly Cookie 会话。
- 登录/登出/刷新/获取用户信息均通过同源 /api 反代调用后端
- 前端不再直接接触 access/refresh token，仅在登录时获取用户信息用于 UI 展示
- 401 时自动尝试刷新或跳回登录页
*/

import type {
  AuthUser,
  LoginOutcome,
  TotpBindConfirm,
  TotpLoginChallenge,
  TotpSetupChallenge,
  TotpSetupInfo,
} from '@/types/api';

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

export async function login(email: string, password: string): Promise<LoginOutcome> {
  const res = await fetch('/api/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    credentials: 'include',
    body: JSON.stringify({ email, password }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof body.detail === 'string' ? body.detail : '登录失败');
  }
  const data = await res.json();

  // 密码正确但需 TOTP 第二步：不建会话，把 challenge 交给登录页的验证码表单。
  if (data.requires_totp === true) {
    const challenge: TotpLoginChallenge = {
      requires_totp: true,
      user_id: data.user_id,
      email: data.email,
      totp_token: data.totp_token ?? null,
      message: data.message ?? 'TOTP verification required',
    };
    return { kind: 'totp', challenge };
  }

  // 生产环境特权账号尚未绑定 TOTP：先完成绑定流程再回来登录。
  if (data.requires_totp_setup === true) {
    const challenge: TotpSetupChallenge = {
      requires_totp_setup: true,
      user_id: data.user_id,
      email: data.email,
      setup_token: data.setup_token ?? null,
      message: data.message ?? 'TOTP enrollment required',
    };
    setPendingSetup(challenge);
    return { kind: 'setup', challenge };
  }

  const user: AuthUser = { id: data.user_id, email: data.email };
  authState = { user, isLoading: false };
  notify();
  return { kind: 'ok', user };
}

// ── TOTP 第二步 / 首次绑定 ────────────────────────────────────────────────

/** 绑定流程的 5 分钟一次性 setup_token 挑战：只留模块内存（不进
 *  localStorage/sessionStorage），硬刷新即失效 → 重新登录再取。 */
let pendingSetup: TotpSetupChallenge | null = null;

export function setPendingSetup(challenge: TotpSetupChallenge): void {
  pendingSetup = challenge;
}

export function getPendingSetup(): TotpSetupChallenge | null {
  return pendingSetup;
}

export function clearPendingSetup(): void {
  pendingSetup = null;
}

/** 状态改变请求的 CSRF 双重提交（后端 /auth/csrf-token 豁免，可无会话先取）。 */
async function ensureCsrfToken(): Promise<string | null> {
  if (typeof document !== 'undefined') {
    const match = document.cookie.match(/(?:^|;\s*)hemall_csrf_token=([^;]+)/);
    if (match) return decodeURIComponent(match[1]);
  }
  try {
    const res = await fetch('/api/auth/csrf-token', { credentials: 'include' });
    if (!res.ok) return null;
    const data = await res.json();
    return data.csrf_token ?? null;
  } catch {
    return null;
  }
}

export interface TotpCredentials {
  code?: string;
  recovery_code?: string;
}

/** TOTP 登录第二步：POST /api/auth/totp/verify → 建会话并写入 authState。
 *  带 totp_token (challenge 令牌, scope=totp) 走 Authorization 头 → 后端 CSRF 豁免；
 *  无令牌（如绑定后用同一验证码自动验证）则先取 CSRF token 走双重提交。 */
export async function verifyTotp(
  userId: string,
  credentials: TotpCredentials,
  totpToken?: string | null,
): Promise<AuthUser> {
  const headers: Record<string, string> = { 'Content-Type': 'application/json' };
  if (totpToken) {
    headers.Authorization = `Bearer ${totpToken}`;
  } else {
    const csrf = await ensureCsrfToken();
    if (csrf) headers['X-CSRF-Token'] = csrf;
  }

  const res = await fetch('/api/auth/totp/verify', {
    method: 'POST',
    headers,
    credentials: 'include',
    body: JSON.stringify({ user_id: userId, ...credentials }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof body.detail === 'string' ? body.detail : '验证码校验失败');
  }
  const data = await res.json();
  const user: AuthUser = { id: data.user_id, email: data.email };
  authState = { user, isLoading: false };
  notify();
  return user;
}

/** 拉取一次性绑定信息 (secret + otpauth URI)；需 setup_token Authorization 头。 */
export async function fetchTotpSetup(setupToken: string): Promise<TotpSetupInfo> {
  const res = await fetch('/api/auth/totp/setup', {
    headers: { Authorization: `Bearer ${setupToken}` },
    credentials: 'include',
  });
  if (!res.ok) {
    throw new Error(res.status === 401 ? '绑定已过期，请重新登录' : '获取绑定信息失败');
  }
  return res.json();
}

/** 提交绑定验证码；恢复码只在本次响应返回一次。 */
export async function bindTotp(
  setupToken: string,
  secret: string,
  code: string,
): Promise<TotpBindConfirm> {
  const res = await fetch('/api/auth/totp/bind', {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      Authorization: `Bearer ${setupToken}`,
      'X-TOTP-Secret': secret,
    },
    credentials: 'include',
    body: JSON.stringify({ code }),
  });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(typeof body.detail === 'string' ? body.detail : '绑定校验失败');
  }
  return res.json();
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
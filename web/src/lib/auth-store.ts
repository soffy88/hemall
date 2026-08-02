/** hemall 前端 — 认证状态管理（简单 localStorage 方案）。 */

import type { AuthUser } from '@/types/api';

const KEY = 'hemall_auth';

interface AuthState {
  token: string;
  user: AuthUser;
}

export function getAuth(): AuthState | null {
  if (typeof window === 'undefined') return null;
  try {
    return JSON.parse(localStorage.getItem(KEY) || 'null');
  } catch {
    return null;
  }
}

export function setAuth(token: string, user: AuthUser): void {
  localStorage.setItem(KEY, JSON.stringify({ token, user }));
}

export function clearAuth(): void {
  localStorage.removeItem(KEY);
}

export function isAuthenticated(): boolean {
  return getAuth()?.token != null;
}

/** hemall 前端 — 顾客账号状态管理（简单 localStorage 方案，跟 auth-store.ts 同构）。 */

const KEY = 'hemall_customer_auth';

interface CustomerAuthState {
  token: string;
  customerId: string;
  email: string;
}

export function getCustomerAuth(): CustomerAuthState | null {
  if (typeof window === 'undefined') return null;
  try {
    return JSON.parse(localStorage.getItem(KEY) || 'null');
  } catch {
    return null;
  }
}

export function setCustomerAuth(token: string, customerId: string, email: string): void {
  localStorage.setItem(KEY, JSON.stringify({ token, customerId, email }));
}

export function clearCustomerAuth(): void {
  localStorage.removeItem(KEY);
}

export function isCustomerAuthenticated(): boolean {
  return getCustomerAuth()?.token != null;
}

'use client';

/**
 * AuthProvider — no-op in the rebuilt frontend.
 * Auth is handled per-layout (admin/layout.tsx checks auth on mount).
 */
export function AuthProvider({ children }: { children: React.ReactNode }) {
  return <>{children}</>;
}

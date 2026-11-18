'use client';

import { createContext, useCallback, useContext, useState } from 'react';

type ToastKind = 'info' | 'error' | 'success' | 'warning';
interface ToastMessage {
  id: number;
  text: string;
  kind: ToastKind;
}

const ToastContext = createContext<((text: string, kind?: ToastKind) => void) | null>(null);

export function ToastProvider({ children }: { children: React.ReactNode }) {
  const [toasts, setToasts] = useState<ToastMessage[]>([]);

  const show = useCallback((text: string, kind: ToastKind = 'info') => {
    const id = Date.now() + Math.random();
    setToasts((prev) => [...prev, { id, text, kind }]);
    setTimeout(() => setToasts((prev) => prev.filter((m) => m.id !== id)), 3200);
  }, []);

  const getBgColor = (kind: ToastKind) => {
    switch (kind) {
      case 'error': return 'bg-red-600';
      case 'success': return 'bg-emerald-600';
      case 'warning': return 'bg-amber-600';
      default: return 'bg-gray-900';
    }
  };

  return (
    <ToastContext.Provider value={show}>
      {children}
      <div className="fixed top-4 left-1/2 -translate-x-1/2 z-[100] flex flex-col items-center gap-2 px-4">
        {toasts.map((t) => (
          <div
            key={t.id}
            className={`px-4 py-2 rounded-lg text-sm font-medium shadow-lg text-white ${getBgColor(t.kind)}`}
          >
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}

/** 显示一条提示：show(text) 或 show(text, 'error'/'success'/'warning')。 */
export function useToast() {
  const show = useContext(ToastContext);
  if (!show) throw new Error('useToast must be used within ToastProvider');
  return show;
}

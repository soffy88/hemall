'use client';

import { useEffect, useRef, useState } from 'react';

type StripePaymentElement = {
  mount: (element: HTMLElement) => void;
  unmount: () => void;
};

type StripeElements = {
  create: (type: 'payment') => StripePaymentElement;
  submit?: () => Promise<{ error?: { message?: string } }>;
};

type StripeClient = {
  elements: (options: { clientSecret: string }) => StripeElements;
  confirmPayment: (options: {
    elements: StripeElements;
    redirect: 'if_required';
  }) => Promise<{
    error?: { message?: string };
    paymentIntent?: { status?: string };
  }>;
};

declare global {
  interface Window {
    Stripe?: (publishableKey: string) => StripeClient;
    __hemallStripePromise?: Promise<StripeClient | null>;
  }
}

function loadStripe(): Promise<StripeClient | null> {
  const publishableKey = process.env.NEXT_PUBLIC_STRIPE_PUBLISHABLE_KEY;
  if (!publishableKey || typeof window === 'undefined') return Promise.resolve(null);
  if (window.Stripe) return Promise.resolve(window.Stripe(publishableKey));
  if (window.__hemallStripePromise) return window.__hemallStripePromise;

  window.__hemallStripePromise = new Promise((resolve, reject) => {
    const existing = document.querySelector<HTMLScriptElement>('script[data-hemall-stripe]');
    const script = existing || document.createElement('script');
    const finish = () => {
      if (window.Stripe) resolve(window.Stripe(publishableKey));
      else reject(new Error('Stripe.js 加载失败'));
    };
    script.addEventListener('load', finish, { once: true });
    script.addEventListener('error', () => reject(new Error('Stripe.js 加载失败')), { once: true });
    if (!existing) {
      script.src = 'https://js.stripe.com/v3/';
      script.async = true;
      script.dataset.hemallStripe = 'true';
      document.head.appendChild(script);
    }
  });
  return window.__hemallStripePromise;
}

interface StripePaymentElementProps {
  clientSecret: string;
  onConfirmed: () => void | Promise<void>;
}

export function StripePaymentElement({ clientSecret, onConfirmed }: StripePaymentElementProps) {
  const mountRef = useRef<HTMLDivElement>(null);
  const [elements, setElements] = useState<StripeElements | null>(null);
  const [stripe, setStripe] = useState<StripeClient | null>(null);
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let cancelled = false;
    let paymentElement: StripePaymentElement | undefined;
    setLoading(true);
    setError('');
    setElements(null);
    setStripe(null);

    loadStripe()
      .then((client) => {
        if (cancelled) return;
        if (!client) {
          setError('Stripe 支付未配置前端公钥，请联系管理员');
          return;
        }
        const stripeElements = client.elements({ clientSecret });
        if (mountRef.current) {
          paymentElement = stripeElements.create('payment');
          paymentElement.mount(mountRef.current);
        }
        setStripe(client);
        setElements(stripeElements);
      })
      .catch((reason: Error) => {
        if (!cancelled) setError(reason.message || 'Stripe 支付组件加载失败');
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });

    return () => {
      cancelled = true;
      paymentElement?.unmount();
    };
  }, [clientSecret]);

  async function submit() {
    if (!stripe || !elements || busy) return;
    setBusy(true);
    setError('');
    try {
      const validation = await elements.submit?.();
      if (validation?.error) throw new Error(validation.error.message || '请检查支付信息');
      const result = await stripe.confirmPayment({ elements, redirect: 'if_required' });
      if (result.error) throw new Error(result.error.message || 'Stripe 支付未完成');
      await onConfirmed();
    } catch (reason: any) {
      setError(reason?.message || 'Stripe 支付未完成');
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="mt-3 rounded-2xl border border-indigo-200 bg-indigo-50 p-4 text-left">
      <div className="mb-2 text-sm font-semibold text-indigo-900">Stripe 安全支付</div>
      <div ref={mountRef} className="rounded-xl bg-white p-3" />
      {loading && <div className="mt-2 text-xs text-indigo-700">正在加载支付组件...</div>}
      {error && <div className="mt-2 text-xs font-semibold text-red-600">{error}</div>}
      <button
        type="button"
        disabled={loading || busy || !stripe || !elements}
        onClick={submit}
        className="mt-3 w-full rounded-xl bg-indigo-600 py-3 text-sm font-bold text-white disabled:opacity-50"
      >
        {busy ? '正在验证支付...' : '确认 Stripe 支付'}
      </button>
      <p className="mt-2 text-center text-xs text-indigo-700">支付结果仍需等待服务端 webhook 验证</p>
    </div>
  );
}

import type { Metadata } from 'next';
import AuthInit from '@/components/auth-init';
import './globals.css';

export const metadata: Metadata = {
  title: '小宋生鲜 · 社区生鲜',
  description: 'xs fresh mall 小宋生鲜 — 社区生鲜电商平台',
  icons: {
    icon: '/favicon.ico',
    shortcut: '/favicon-16.png',
    apple: '/apple-touch-icon-180.png',
    other: [
      { rel: 'icon', type: 'image/png', sizes: '32x32', url: '/favicon-32.png' },
      { rel: 'icon', type: 'image/png', sizes: '16x16', url: '/favicon-16.png' },
      { rel: 'manifest', url: '/manifest.json' },
    ],
  },
  openGraph: {
    title: '小宋生鲜 · 社区生鲜',
    description: 'xs fresh mall 小宋生鲜 — 社区生鲜电商平台',
    type: 'website',
    locale: 'zh_CN',
    siteName: '小宋生鲜',
  },
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-gray-50 text-gray-900 antialiased">
        <AuthInit />
        {children}
      </body>
    </html>
  );
}

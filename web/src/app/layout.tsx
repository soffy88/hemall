import type { Metadata } from 'next';
import './globals.css';

export const metadata: Metadata = {
  title: 'Hemall Commerce',
  description: 'Hemall headless commerce — admin & storefront',
};

export default function RootLayout({ children }: { children: React.ReactNode }) {
  return (
    <html lang="zh-CN">
      <body className="min-h-screen bg-gray-50 text-gray-900 antialiased">
        {children}
      </body>
    </html>
  );
}

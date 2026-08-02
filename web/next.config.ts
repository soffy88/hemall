import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  transpilePackages: ['@helios/blocks', '@helios/oui'],
  // 允许从局域网 IP / 主机名访问 dev 资源 (HMR / 字体); 仅 dev 生效。
  allowedDevOrigins: ['192.168.0.170', '100.86.95.70', 'ubuntu26'],
};

export default nextConfig;

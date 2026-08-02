import type { NextConfig } from 'next';

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // repo 根目录下还有一份 package-lock.json (Python 后端仓库)，不设置的话 Next
  // 会误把仓库根目录当成 workspace root，standalone 输出会多嵌一层 web/ 目录。
  turbopack: { root: __dirname },
  transpilePackages: ['@helios/blocks', '@helios/oui'],
  // 允许从局域网 IP / 主机名访问 dev 资源 (HMR / 字体); 仅 dev 生效。
  allowedDevOrigins: ['192.168.0.170', '100.86.95.70', 'ubuntu26'],
};

export default nextConfig;

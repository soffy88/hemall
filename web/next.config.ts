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

// F-05: 生产构建（next build）必须显式给出后端地址，缺了就 fail-fast。
// 服务端 /api 反代的 BACKEND_URL = HEMALL_BACKEND_URL || NEXT_PUBLIC_API_URL，
// 两者都缺时会静默回退 http://localhost:8030——容器内永远不通且难以排查。
// 浏览器侧已统一走同源 /api，不读 NEXT_PUBLIC_API_URL，故只需服务端地址。
// 本地构建示例: NEXT_PUBLIC_API_URL=http://localhost:8030 npm run build
if (
  (process.argv.includes('build') || process.env.npm_lifecycle_event === 'build') &&
  !process.env.NEXT_PUBLIC_API_URL &&
  !process.env.HEMALL_BACKEND_URL
) {
  throw new Error(
    '[audit F-05] next build 缺少后端地址：设置 NEXT_PUBLIC_API_URL（构建期）或 ' +
      'HEMALL_BACKEND_URL。Docker: docker build --build-arg NEXT_PUBLIC_API_URL=https://<host> ./web',
  );
}

export default nextConfig;

import type { NextConfig } from 'next';

// F-01/F-03（CSP 为 frontend.md 对 F-01 的修复建议）：生产环境安全响应头。
// - script-src 'unsafe-inline'：Next.js App Router 的 flight 内联脚本必须内联执行；
//   按请求下发 nonce 需要 middleware 参与渲染，本仓库暂不引入（报告已注明取舍）。
// - stripe 域名：stripe-payment-element.tsx:49 动态加载 js.stripe.com，
//   Payment Element iframe / 3DS 经 js.stripe.com 与 m.stripe.network，
//   confirmPayment 上行 api.stripe.com，回跳 hooks.stripe.com——漏放行会断支付。
// - img-src 含 https:：admin 的 media_url / video_url 为自由文本，可填外部地址，
//   拦截会破坏既有内容；图片不执行脚本，风险可控。
// - connect-src 'self'：浏览器侧已统一走同源 /api（F-01），无外部 XHR。
// dev (next dev) 不下发，避免 HMR websocket / 评估脚本被拦。
const isDevServer =
  process.argv.includes('dev') || process.env.npm_lifecycle_event === 'dev';

const csp = [
  "default-src 'self'",
  "base-uri 'self'",
  "object-src 'none'",
  "frame-ancestors 'self'",
  "script-src 'self' 'unsafe-inline' https://js.stripe.com",
  "style-src 'self' 'unsafe-inline'",
  "img-src 'self' data: blob: https:",
  "font-src 'self' data:",
  "connect-src 'self' https://api.stripe.com",
  "frame-src 'self' https://js.stripe.com https://hooks.stripe.com https://m.stripe.network",
  "form-action 'self' https://js.stripe.com",
].join('; ');

const nextConfig: NextConfig = {
  reactStrictMode: true,
  output: 'standalone',
  // repo 根目录下还有一份 package-lock.json (Python 后端仓库)，不设置的话 Next
  // 会误把仓库根目录当成 workspace root，standalone 输出会多嵌一层 web/ 目录。
  turbopack: { root: __dirname },
  transpilePackages: ['@helios/blocks', '@helios/oui'],
  // 允许从局域网 IP / 主机名访问 dev 资源 (HMR / 字体); 仅 dev 生效。
  allowedDevOrigins: ['192.168.0.170', '100.86.95.70', 'ubuntu26'],
  async headers() {
    if (isDevServer) return [];
    return [
      {
        source: '/(.*)',
        headers: [
          { key: 'Content-Security-Policy', value: csp },
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'SAMEORIGIN' },
          // 与后端 app/storefront.py 的 Referrer-Policy 保持一致。
          { key: 'Referrer-Policy', value: 'no-referrer' },
        ],
      },
    ];
  },
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

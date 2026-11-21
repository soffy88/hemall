/** 服务端专用：后端 API 地址（只被 route handler / middleware 引用，不进 client bundle）。
 *
 * F-05: 浏览器侧统一走同源 /api 反代，不再直连后端；这里只给 Next 服务端用。
 * 生产构建缺 NEXT_PUBLIC_API_URL 时 next.config.ts 直接让构建失败。
 */
export const BACKEND_URL =
  process.env.HEMALL_BACKEND_URL ||
  process.env.NEXT_PUBLIC_API_URL ||
  'http://localhost:8030';

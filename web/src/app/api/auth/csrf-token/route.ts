import { NextRequest } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 取 CSRF 双重提交 token：透传到后端 GET /auth/csrf-token。
 *  token 落在非 HttpOnly 的 hemall_csrf_token cookie 里，前端读后放
 *  X-CSRF-Token 头，与后端 csrf 中间件校验一致。 */
export async function GET(request: NextRequest) {
  return proxyToBackend(request, '/auth/csrf-token');
}

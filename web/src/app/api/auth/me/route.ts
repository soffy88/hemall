import { NextRequest } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 当前管理员会话：透传浏览器 cookie 到后端 GET /auth/me（专用会话校验接口）。
 *  不接触任何业务接口，不向客户端 JS 暴露令牌——后端只回 user_id/email/role。 */
export async function GET(request: NextRequest) {
  return proxyToBackend(request, '/auth/me');
}

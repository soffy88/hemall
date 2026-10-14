import { NextRequest } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 管理员登出：透传到后端 POST /auth/logout，清会话 Set-Cookie 原样回传。
 *  后端只注册了 @router.post('/auth/logout')，必须显式带 method —— 否则 fetch
 *  默认 GET，后端返回 405，会话 cookie 清不掉。 */
export async function POST(request: NextRequest) {
  return proxyToBackend(request, '/auth/logout', { method: 'POST' });
}

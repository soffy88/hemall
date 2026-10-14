import { NextRequest } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 当前顾客会话：透传浏览器 cookie 到后端 GET /auth/customer/me。 */
export async function GET(request: NextRequest) {
  return proxyToBackend(request, '/auth/customer/me');
}

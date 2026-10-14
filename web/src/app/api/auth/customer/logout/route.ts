import { NextRequest } from 'next/server';
import { proxyToBackend } from '@/lib/proxy';

/** 顾客登出：透传到后端 POST /auth/customer/logout，清会话 Set-Cookie 原样回传。 */
export async function POST(request: NextRequest) {
  return proxyToBackend(request, '/auth/customer/logout');
}

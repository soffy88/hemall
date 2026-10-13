import { NextRequest, NextResponse } from 'next/server';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8030';

export async function GET(request: NextRequest) {
  const sessionToken = request.cookies.get('staff_session')?.value;

  if (!sessionToken) {
    return NextResponse.json({ detail: 'Not authenticated' }, { status: 401 });
  }

  try {
    const response = await fetch(`${BACKEND_URL}/admin/users`, {
      headers: {
        Authorization: `Bearer ${sessionToken}`,
      },
    });

    if (!response.ok) {
      if (response.status === 401) {
        const res = NextResponse.json({ detail: 'Session expired' }, { status: 401 });
        res.cookies.set('staff_session', '', {
          httpOnly: true,
          secure: process.env.NODE_ENV === 'production',
          sameSite: 'lax',
          path: '/',
          maxAge: 0,
        });
        return res;
      }
      return NextResponse.json(
        { detail: 'Failed to validate session' },
        { status: response.status }
      );
    }

    const data = await response.json();
    return NextResponse.json({
      user_id: data.id || data.user_id,
      email: data.email,
    });
  } catch (error) {
    console.error('Session validation error:', error);
    return NextResponse.json(
      { detail: 'Internal server error' },
      { status: 500 }
    );
  }
}
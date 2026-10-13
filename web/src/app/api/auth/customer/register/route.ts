import { NextRequest, NextResponse } from 'next/server';

const BACKEND_URL = process.env.NEXT_PUBLIC_API_URL || 'http://localhost:8030';

export async function POST(request: NextRequest) {
  try {
    const body = await request.json();
    const { email, password, phone, name } = body;

    if (!email || !password) {
      return NextResponse.json(
        { detail: 'email and password are required' },
        { status: 400 }
      );
    }

    const response = await fetch(`${BACKEND_URL}/store/customers/register`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ email, password, phone, name }),
    });

    const data = await response.json();

    if (!response.ok) {
      return NextResponse.json(
        { detail: data.detail || 'Registration failed' },
        { status: response.status }
      );
    }

    const res = NextResponse.json({
      customer_id: data.customer_id,
      email: data.email,
    });

    res.cookies.set('customer_session', data.access_token, {
      httpOnly: true,
      secure: process.env.NODE_ENV === 'production',
      sameSite: 'lax',
      path: '/',
      maxAge: 60 * 60 * 24 * 30,
    });

    return res;
  } catch (error) {
    console.error('Customer registration error:', error);
    return NextResponse.json(
      { detail: 'Internal server error' },
      { status: 500 }
    );
  }
}
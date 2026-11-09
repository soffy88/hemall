import { afterEach, describe, expect, it, vi } from 'vitest';
import {
  bootChatwoot,
  chatwootDisplayName,
  getChatwootConfig,
  shouldShowChatwoot,
  syncChatwootUser,
} from './chatwoot';

afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
  delete (window as unknown as Record<string, unknown>).$chatwoot;
  delete (window as unknown as Record<string, unknown>).chatwootSDK;
});

describe('getChatwootConfig', () => {
  it('未配置时返回 null（不加载）', () => {
    vi.stubEnv('NEXT_PUBLIC_CHATWOOT_BASE_URL', '');
    vi.stubEnv('NEXT_PUBLIC_CHATWOOT_WEBSITE_TOKEN', '');
    expect(getChatwootConfig()).toBeNull();
  });

  it('只有 base url 或只有 token 时也返回 null', () => {
    vi.stubEnv('NEXT_PUBLIC_CHATWOOT_BASE_URL', 'https://support.sxueji.com');
    vi.stubEnv('NEXT_PUBLIC_CHATWOOT_WEBSITE_TOKEN', '');
    expect(getChatwootConfig()).toBeNull();
  });

  it('返回规范化后的 origin 与 token', () => {
    vi.stubEnv('NEXT_PUBLIC_CHATWOOT_BASE_URL', 'https://support.sxueji.com/');
    vi.stubEnv('NEXT_PUBLIC_CHATWOOT_WEBSITE_TOKEN', 'tok-123');
    expect(getChatwootConfig()).toEqual({
      baseUrl: 'https://support.sxueji.com',
      websiteToken: 'tok-123',
    });
  });

  it('非法地址返回 null', () => {
    vi.stubEnv('NEXT_PUBLIC_CHATWOOT_BASE_URL', 'ftp://nope');
    vi.stubEnv('NEXT_PUBLIC_CHATWOOT_WEBSITE_TOKEN', 'tok');
    expect(getChatwootConfig()).toBeNull();
  });
});

describe('shouldShowChatwoot', () => {
  it('商城页面显示', () => {
    expect(shouldShowChatwoot('/shop')).toBe(true);
    expect(shouldShowChatwoot('/shop/products/1')).toBe(true);
    expect(shouldShowChatwoot('/')).toBe(true);
  });

  it('后台/管理页不显示', () => {
    expect(shouldShowChatwoot('/admin')).toBe(false);
    expect(shouldShowChatwoot('/admin/orders')).toBe(false);
    expect(shouldShowChatwoot('/agent')).toBe(false);
    expect(shouldShowChatwoot('/supplier')).toBe(false);
  });

  it('前缀相同但不是后台路径时仍显示', () => {
    expect(shouldShowChatwoot('/administrator-guide')).toBe(true);
  });
});

describe('chatwootDisplayName', () => {
  it('只取邮箱 @ 前缀作为昵称', () => {
    expect(chatwootDisplayName({ customerId: 'c1', email: 'alice@example.com' })).toBe('alice');
    expect(chatwootDisplayName({ customerId: 'c1', email: null })).toBe('');
    expect(chatwootDisplayName(null)).toBe('');
  });
});

describe('syncChatwootUser', () => {
  it('已登录只上报用户 ID 与昵称，不带邮箱或手机号', () => {
    const setUser = vi.fn();
    const reset = vi.fn();
    (window as unknown as Record<string, unknown>).$chatwoot = { setUser, reset };

    const ok = syncChatwootUser({ customerId: 'cust-9', email: 'bob@example.com' });

    expect(ok).toBe(true);
    expect(setUser).toHaveBeenCalledTimes(1);
    expect(setUser).toHaveBeenCalledWith('cust-9', { name: 'bob' });
    const payload = JSON.stringify(setUser.mock.calls[0]);
    expect(payload).not.toContain('bob@example.com');
    expect(payload).not.toContain('phone');
    expect(reset).not.toHaveBeenCalled();
  });

  it('未登录时重置为匿名访客', () => {
    const setUser = vi.fn();
    const reset = vi.fn();
    (window as unknown as Record<string, unknown>).$chatwoot = { setUser, reset };

    expect(syncChatwootUser(null)).toBe(true);
    expect(reset).toHaveBeenCalledTimes(1);
    expect(setUser).not.toHaveBeenCalled();
  });

  it('widget 未就绪时返回 false，交给调用方重试', () => {
    expect(syncChatwootUser({ customerId: 'c1', email: null })).toBe(false);
  });
});

describe('bootChatwoot', () => {
  it('注入 sdk 脚本并设置 zh_CN，重复调用不重复注入', async () => {
    const run = vi.fn();
    (window as unknown as Record<string, unknown>).chatwootSDK = { run };
    document.querySelectorAll('script[data-hemall-chatwoot]').forEach((n) => n.remove());

    await bootChatwoot({ baseUrl: 'https://support.sxueji.com', websiteToken: 'tok' });
    await bootChatwoot({ baseUrl: 'https://support.sxueji.com', websiteToken: 'tok' });

    expect(window.chatwootSettings).toMatchObject({ locale: 'zh_CN' });
    expect(run).toHaveBeenCalledTimes(1);
    expect(run).toHaveBeenCalledWith({
      websiteToken: 'tok',
      baseUrl: 'https://support.sxueji.com',
    });
    expect(document.querySelectorAll('script[data-hemall-chatwoot]')).toHaveLength(0);
  });
});

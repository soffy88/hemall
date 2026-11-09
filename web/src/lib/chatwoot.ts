/** Chatwoot 网页客服 SDK 的加载与访客身份绑定。
 *
 * 由构建期环境变量控制：
 *   NEXT_PUBLIC_CHATWOOT_BASE_URL     Chatwoot 站点地址（如 https://support.sxueji.com）
 *   NEXT_PUBLIC_CHATWOOT_WEBSITE_TOKEN  网站收件箱的 website_token
 * 两者任一缺失时不加载任何脚本（默认关闭）。
 *
 * 隐私约束：登录用户只上报用户 ID 与昵称（取邮箱 @ 前缀），不传手机号/邮箱。
 */

export type ChatwootConfig = { baseUrl: string; websiteToken: string };

export type ChatwootUser = { customerId: string; email: string | null };

type ChatwootSdk = {
  run: (options: { websiteToken: string; baseUrl: string }) => void;
};

type ChatwootClient = {
  setUser: (identifier: string, user: Record<string, string>) => void;
  reset: () => void;
};

declare global {
  interface Window {
    chatwootSettings?: Record<string, unknown>;
    chatwootSDK?: ChatwootSdk;
    $chatwoot?: ChatwootClient;
  }
}

const SCRIPT_ATTR = 'data-hemall-chatwoot';

/** 读取并校验配置；未配置或地址非法时返回 null（调用方据此跳过加载）。
 * 注意：必须用 process.env.NEXT_PUBLIC_X 的直接属性访问，Next 才能在构建期把它内联进
 * 客户端 bundle；写成 process.env[name] 会内联失败。 */
export function getChatwootConfig(): ChatwootConfig | null {
  const rawBase = (process.env.NEXT_PUBLIC_CHATWOOT_BASE_URL || '').trim();
  const websiteToken = (process.env.NEXT_PUBLIC_CHATWOOT_WEBSITE_TOKEN || '').trim();
  if (!rawBase || !websiteToken) return null;
  let origin: string;
  try {
    const url = new URL(rawBase);
    if (url.protocol !== 'https:' && url.protocol !== 'http:') return null;
    origin = url.origin;
  } catch {
    return null;
  }
  return { baseUrl: origin, websiteToken };
}

/** 后台/管理页不显示聊天窗口。storefront 挂在 /shop 布局下，此判断是二次护栏。 */
export function shouldShowChatwoot(pathname: string | null | undefined): boolean {
  if (!pathname) return true;
  const hidden = ['/admin', '/agent', '/supplier'];
  return !hidden.some((prefix) => pathname === prefix || pathname.startsWith(`${prefix}/`));
}

/** 昵称：只用邮箱 @ 前缀，避免把完整邮箱/手机号带给第三方。 */
export function chatwootDisplayName(user: ChatwootUser | null): string {
  if (!user?.email) return '';
  return user.email.split('@')[0] || '';
}

let bootPromise: Promise<void> | null = null;

/** 注入 sdk.js 并启动 widget；重复调用复用同一个 Promise。 */
export function bootChatwoot(config: ChatwootConfig): Promise<void> {
  if (typeof window === 'undefined' || typeof document === 'undefined') return Promise.resolve();
  if (bootPromise) return bootPromise;

  bootPromise = new Promise<void>((resolve) => {
    window.chatwootSettings = {
      locale: 'zh_CN',
      position: 'right',
      type: 'standard',
    };
    const start = () => {
      window.chatwootSDK?.run({
        websiteToken: config.websiteToken,
        baseUrl: config.baseUrl,
      });
      resolve();
    };
    if (window.chatwootSDK) {
      start();
      return;
    }
    const script = document.createElement('script');
    script.src = `${config.baseUrl}/packs/js/sdk.js`;
    script.async = true;
    script.defer = true;
    script.setAttribute(SCRIPT_ATTR, '1');
    script.onload = start;
    // 加载失败（如子域名暂未解析）时静默跳过，不影响页面其它功能。
    script.onerror = () => resolve();
    document.head.appendChild(script);
  });

  return bootPromise;
}

/**
 * 同步当前访客身份到 widget。
 * 已登录：setUser(id, { name })，只含 ID 与昵称；未登录：reset()。
 * widget 尚未就绪时返回 false，由调用方重试。
 */
export function syncChatwootUser(user: ChatwootUser | null): boolean {
  if (typeof window === 'undefined') return false;
  const client = window.$chatwoot;
  if (!client) return false;
  if (user?.customerId) {
    const name = chatwootDisplayName(user);
    client.setUser(user.customerId, name ? { name } : {});
  } else {
    client.reset();
  }
  return true;
}

/** 每个测试文件的前置：把后端地址钉死，避免用例间/机器间受环境变量影响。 */
process.env.HEMALL_BACKEND_URL = 'http://backend.test';
process.env.NEXT_PUBLIC_API_URL = 'http://backend.test';

/**
 * Node >= 22 自带一个实验性的 `localStorage` / `sessionStorage` 全局对象；没给
 * `--localstorage-file` 时它们是 undefined，会盖掉 jsdom 的实现，测试里
 * `window.localStorage.clear()` 直接抛 TypeError。这里统一换成内存版 Storage，
 * 让 device_id / 提货票据这类持久化键在用例里可断言、可重置。
 */
function memoryStorage(): Storage {
  let map = new Map<string, string>();
  return {
    get length() {
      return map.size;
    },
    key: (i: number) => [...map.keys()][i] ?? null,
    getItem: (k: string) => (map.has(k) ? (map.get(k) as string) : null),
    setItem: (k: string, v: string) => {
      map.set(String(k), String(v));
    },
    removeItem: (k: string) => {
      map.delete(k);
    },
    clear: () => {
      map = new Map();
    },
  } as Storage;
}

for (const target of [globalThis, globalThis.window]) {
  if (!target) continue;
  for (const name of ['localStorage', 'sessionStorage'] as const) {
    Object.defineProperty(target, name, {
      value: memoryStorage(),
      configurable: true,
      writable: true,
    });
  }
}

export {};
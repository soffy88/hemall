/** 统一的错误消息提取（strict 模式下 catch 变量是 unknown，不能直接 `.message`）。
 *
 * 返回可读消息，取不到时返回 fallback：
 * - 字符串：原样返回；
 * - 对象且有字符串 message（Error / OmodulResult.error / 后端错误体）：返回 message；
 * - 其它（undefined、数字、无 message 的对象等）：返回 fallback。
 */
export function errMessage(e: unknown, fallback = ''): string {
  if (typeof e === 'string') return e;
  if (e && typeof e === 'object') {
    const m = (e as { message?: unknown }).message;
    if (typeof m === 'string' && m) return m;
  }
  return fallback;
}

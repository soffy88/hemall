/** hemall 前端 — 金额格式化，按币种选符号（而不是全站硬编码 ¥）。 */

const CURRENCY_SYMBOLS: Record<string, string> = {
  CNY: '¥',
  USD: '$',
  EUR: '€',
  GBP: '£',
  JPY: '¥',
  HKD: 'HK$',
};

export function formatMoney(cents: number | null | undefined, currency: string = 'CNY'): string {
  if (cents == null) return '—';
  const symbol = CURRENCY_SYMBOLS[currency] || `${currency} `;
  return `${symbol}${(cents / 100).toFixed(2)}`;
}

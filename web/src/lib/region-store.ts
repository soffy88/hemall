/** hemall 前端 — 商城区域选择（简单 sessionStorage 方案）。 */

const KEY = 'hemall_region';

export interface RegionSelection {
  code: string;
  currency: string;
}

export function getSelectedRegion(): RegionSelection | null {
  if (typeof window === 'undefined') return null;
  try {
    return JSON.parse(sessionStorage.getItem(KEY) || 'null');
  } catch {
    return null;
  }
}

export function setSelectedRegion(region: RegionSelection): void {
  sessionStorage.setItem(KEY, JSON.stringify(region));
}

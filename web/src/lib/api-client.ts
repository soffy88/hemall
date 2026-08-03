/** hemall 前端 — API 客户端，覆盖所有后端接口。 */

import type {
  Address,
  AdminRegion,
  AppUser,
  BatchJob,
  Cart,
  CheckoutResult,
  Claim,
  Customer,
  CustomerAddress,
  CustomerGroup,
  CustomerTokenResponse,
  DashboardKPIs,
  Discount,
  Fulfillment,
  GiftCard,
  Health,
  Order,
  OmodulResult,
  PriceList,
  Product,
  ProductCategory,
  ProductCollection,
  RecommendationResult,
  Region,
  ReturnRequest,
  RmaClaim,
  SalesChannel,
  StockLocation,
  StorefrontProduct,
  SubmitClaimResult,
  SupplierStatus,
  Swap,
  TaxRate,
  TokenResponse,
} from '@/types/api';

/**
 * 后端地址解析：NEXT_PUBLIC_API_URL 有值就用它（生产/固定域名场景）；否则在浏览器里
 * 用"当前访问前端用的 host + 8030 端口"推导——同一台机器可能同时挂在局域网 IP 和
 * Tailscale IP 下，写死单个 IP 只对那一种访问路径有效，动态推导才能两边都通。
 */
function resolveApiBase(): string {
  if (process.env.NEXT_PUBLIC_API_URL) return process.env.NEXT_PUBLIC_API_URL;
  if (typeof window !== 'undefined') {
    return `${window.location.protocol}//${window.location.hostname}:8030`;
  }
  return 'http://localhost:8030';
}

const BASE = resolveApiBase();

function token(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return JSON.parse(localStorage.getItem('hemall_auth') || 'null')?.token;
  } catch {
    return null;
  }
}

function customerToken(): string | null {
  if (typeof window === 'undefined') return null;
  try {
    return JSON.parse(localStorage.getItem('hemall_customer_auth') || 'null')?.token;
  } catch {
    return null;
  }
}

/** FastAPI 422 校验错误的 detail 是对象数组（非字符串），需拍平成可读文本。 */
function errorMessage(body: any, status: number): string {
  if (typeof body?.detail === 'string') return body.detail;
  if (Array.isArray(body?.detail)) {
    return body.detail.map((d: any) => d?.msg || JSON.stringify(d)).join('; ');
  }
  if (body?.error?.message) return body.error.message;
  return `HTTP ${status}`;
}

async function request<T>(
  path: string,
  opts: RequestInit = {},
  auth: boolean | 'customer' = false,
): Promise<T> {
  const headers: Record<string, string> = {
    'Content-Type': 'application/json',
    ...(opts.headers as Record<string, string> || {}),
  };
  if (auth === 'customer') {
    const t = customerToken();
    if (t) headers['Authorization'] = `Bearer ${t}`;
  } else if (auth) {
    const t = token();
    if (t) headers['Authorization'] = `Bearer ${t}`;
  }
  const res = await fetch(`${BASE}${path}`, { ...opts, headers });
  if (!res.ok) {
    const body = await res.json().catch(() => ({ detail: res.statusText }));
    throw new Error(errorMessage(body, res.status));
  }
  return res.json();
}

// ── Phase 7 Task 3: 设备指纹 (空间-行为矩阵的数据源标识) ──────────────
// 零登录端点 (nearby-feed / 限流层) 都用 X-Device-Id 识别设备；浏览器端用
// localStorage 持久化一个随机指纹，checkout 后后端据此记录购买轨迹，Feed
// 才能把关联商品 (买过牛肉 → 番茄/洋葱) 插队到视野最前方。
export function deviceId(): string {
  if (typeof window === 'undefined') return '';
  let id = window.localStorage.getItem('hemall_device_id');
  if (!id) {
    id = `dev-${Math.random().toString(36).slice(2, 10)}${Date.now().toString(36)}`;
    window.localStorage.setItem('hemall_device_id', id);
  }
  return id;
}

/** 通用 omodul 写端点调用：POST /<domain>/<name>，请求体即 Input 模型。 */
function omodul<T = OmodulResult>(domain: string, name: string, data: object, auth: boolean = true) {
  return request<T>(`/${domain}/${name}`, { method: 'POST', body: JSON.stringify(data) }, auth);
}

// ── Auth ────────────────────────────────────────────────────────

export const api = {
  health: () => request<Health>('/health'),

  login: (email: string, password: string) =>
    request<TokenResponse>('/auth/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  // ── Admin: Products ─────────────────────────────────────────

  adminListProducts: (params?: { status?: string; search?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.status) qs.set('status', params.status);
    if (params?.search) qs.set('search', params.search);
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    return request<Product[]>(`/admin/products?${qs}`, {}, true);
  },

  adminGetProduct: (id: string) =>
    request<Product>(`/admin/products/${id}`, {}, true),

  createProduct: (data: { title: string; slug: string; description?: string; status?: string }) =>
    request<OmodulResult>('/catalog/create_product', {
      method: 'POST',
      body: JSON.stringify(data),
    }, true),

  updateProduct: (data: { product_id: string; title?: string; description?: string; status?: string }) =>
    request<OmodulResult>('/catalog/update_product', {
      method: 'POST',
      body: JSON.stringify(data),
    }, true),

  deleteProduct: (data: { product_id: string }) =>
    request<OmodulResult>('/catalog/delete_product', {
      method: 'POST',
      body: JSON.stringify(data),
    }, true),

  createProductVariant: (data: { product_id: string; sku_code: string; option_values?: Record<string, string>; reference_price_cents?: number }) =>
    request<OmodulResult>('/catalog/create_product_variant', {
      method: 'POST',
      body: JSON.stringify(data),
    }, true),

  createInventoryBatch: (data: { variant_id: string; batch_no: string; location_id: string; stock_qty: number; retail_price_cents: number; cost_price_cents: number; video_url: string; currency?: string }) =>
    request<OmodulResult>('/inventory/create_inventory_batch', {
      method: 'POST',
      body: JSON.stringify(data),
    }, true),

  // ── Admin: Orders ───────────────────────────────────────────

  adminListOrders: (params?: { status?: string; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.status) qs.set('status', params.status);
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    return request<Order[]>(`/admin/orders?${qs}`, {}, true);
  },

  adminGetOrder: (id: string) =>
    request<Order>(`/admin/orders/${id}`, {}, true),

  // ── Admin: Customers ────────────────────────────────────────

  adminListCustomers: (params?: { limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    return request<Customer[]>(`/admin/customers?${qs}`, {}, true);
  },

  // ── Admin: Dashboard ────────────────────────────────────────

  adminDashboardKPIs: () =>
    request<DashboardKPIs>('/admin/dashboard/kpis', {}, true),

  // ── Admin: Fulfillments / Marketing / Aftersales / Inventory settings ──

  adminListFulfillments: (orderId: string) =>
    request<Fulfillment[]>(`/admin/orders/${orderId}/fulfillments`, {}, true),

  adminListDiscounts: () =>
    request<Discount[]>('/admin/discounts', {}, true),

  adminListGiftCards: () =>
    request<GiftCard[]>('/admin/gift-cards', {}, true),

  adminListReturns: (orderId?: string) =>
    request<ReturnRequest[]>(`/admin/returns${orderId ? `?order_id=${orderId}` : ''}`, {}, true),

  adminListSwaps: (orderId?: string) =>
    request<Swap[]>(`/admin/swaps${orderId ? `?order_id=${orderId}` : ''}`, {}, true),

  adminListClaims: (orderId?: string) =>
    request<Claim[]>(`/admin/claims${orderId ? `?order_id=${orderId}` : ''}`, {}, true),

  adminListPriceLists: () =>
    request<PriceList[]>('/admin/price-lists', {}, true),

  adminListStockLocations: () =>
    request<StockLocation[]>('/admin/stock-locations', {}, true),

  adminListSalesChannels: () =>
    request<SalesChannel[]>('/admin/sales-channels', {}, true),

  adminListRegions: () =>
    request<AdminRegion[]>('/admin/regions', {}, true),

  adminListTaxRates: (regionCode?: string) =>
    request<TaxRate[]>(`/admin/tax-rates${regionCode ? `?region_code=${regionCode}` : ''}`, {}, true),

  adminListCategories: () =>
    request<ProductCategory[]>('/admin/categories', {}, true),

  adminListCollections: () =>
    request<ProductCollection[]>('/admin/collections', {}, true),

  adminListCustomerGroups: () =>
    request<CustomerGroup[]>('/admin/customer-groups', {}, true),

  adminListCustomerAddresses: (customerId: string) =>
    request<CustomerAddress[]>(`/admin/customers/${customerId}/addresses`, {}, true),

  adminListUsers: () =>
    request<AppUser[]>('/admin/users', {}, true),

  adminListBatchJobs: () =>
    request<BatchJob[]>('/admin/batch-jobs', {}, true),

  // ── Storefront: Products ────────────────────────────────────

  storeProducts: (params?: { search?: string; min_price?: number; max_price?: number; limit?: number; offset?: number }) => {
    const qs = new URLSearchParams();
    if (params?.search) qs.set('search', params.search);
    if (params?.min_price) qs.set('min_price', String(params.min_price));
    if (params?.max_price) qs.set('max_price', String(params.max_price));
    if (params?.limit) qs.set('limit', String(params.limit));
    if (params?.offset) qs.set('offset', String(params.offset));
    return request<StorefrontProduct[]>(`/store/products?${qs}`);
  },

  storeProduct: (id: string) =>
    request<StorefrontProduct>(`/store/products/${id}`),

  storeRegions: () =>
    request<Region[]>('/store/regions'),

  // ── Storefront: 推荐 (热门 / 相似商品；product_id 列表需自行对照 storeProducts 数据渲染) ──

  storeRecommendHot: (topK: number = 12) =>
    request<{ data: RecommendationResult }>(`/recommend/hot?top_k=${topK}`),

  storeRecommendSimilar: (productId: string, topK: number = 8) =>
    request<{ data: RecommendationResult }>(`/recommend/${productId}/similar?top_k=${topK}`),

  // ── Storefront: Cart ────────────────────────────────────────

  createCart: (regionCode: string = 'cn-east', currency: string = 'CNY') =>
    request<{ cart_id: string; currency: string; region_code: string }>('/store/carts', {
      method: 'POST',
      body: JSON.stringify({ region_code: regionCode, currency }),
    }),

  getCart: (cartId: string) =>
    request<Cart>(`/store/carts/${cartId}`),

  addLineItem: (cartId: string, batchId: string, quantity: number = 1) =>
    request<{ line_item_id: string; new_quantity: number }>('/store/carts/line-items', {
      method: 'POST',
      body: JSON.stringify({ cart_id: cartId, batch_id: batchId, quantity }),
    }),

  updateLineItem: (cartId: string, lineItemId: string, quantity: number) =>
    request<{ line_item_id: string; new_quantity: number }>('/store/carts/line-items', {
      method: 'PUT',
      body: JSON.stringify({ cart_id: cartId, line_item_id: lineItemId, quantity }),
    }),

  deleteLineItem: (cartId: string, lineItemId: string) =>
    request<{ ok: boolean }>('/store/carts/line-items', {
      method: 'DELETE',
      body: JSON.stringify({ cart_id: cartId, line_item_id: lineItemId }),
    }),

  setCartBillingAddress: (cartId: string, address: Address) =>
    request<{ ok: boolean }>('/store/carts/billing-address', {
      method: 'POST',
      body: JSON.stringify({ cart_id: cartId, address }),
    }),

  setCartShippingAddress: (cartId: string, address: Address) =>
    request<{ ok: boolean }>('/store/carts/shipping-address', {
      method: 'POST',
      body: JSON.stringify({ cart_id: cartId, address }),
    }),

  setCartCustomer: (cartId: string, customerId: string) =>
    request<{ ok: boolean }>('/store/carts/customer', {
      method: 'POST',
      body: JSON.stringify({ cart_id: cartId, customer_id: customerId }),
    }),

  // ── Storefront: Discounts / gift cards / shipping / region ──

  storeApplyDiscount: (cartId: string, code: string) =>
    request<{ discount_id: string; discount_cents: number; grand_total_cents: number }>('/store/carts/discount', {
      method: 'POST',
      body: JSON.stringify({ cart_id: cartId, code }),
    }),

  storeRemoveDiscount: (cartId: string, discountId: string) =>
    request<{ ok: boolean }>('/store/carts/discount', {
      method: 'DELETE',
      body: JSON.stringify({ cart_id: cartId, discount_id: discountId }),
    }),

  storeApplyGiftCard: (cartId: string, code: string) =>
    request<{ gift_card_id: string; applied_cents: number; amount_due_cents: number }>('/store/carts/gift-card', {
      method: 'POST',
      body: JSON.stringify({ cart_id: cartId, code }),
    }),

  storeRemoveGiftCard: (cartId: string, giftCardId: string) =>
    request<{ ok: boolean }>('/store/carts/gift-card', {
      method: 'DELETE',
      body: JSON.stringify({ cart_id: cartId, gift_card_id: giftCardId }),
    }),

  storeAddShippingMethod: (cartId: string, methodName: string, priceCents: number) =>
    request<{ shipping_cents: number; grand_total_cents: number }>('/store/carts/shipping-method', {
      method: 'POST',
      body: JSON.stringify({ cart_id: cartId, method_name: methodName, price_cents: priceCents }),
    }),

  storeSetCartRegion: (cartId: string, regionCode: string, currency: string) =>
    request<{ ok: boolean }>('/store/carts/region', {
      method: 'PUT',
      body: JSON.stringify({ cart_id: cartId, region_code: regionCode, currency }),
    }),

  // ── Storefront: Checkout ────────────────────────────────────

  checkout: (data: { cart_id: string; billing_address?: Address; shipping_address?: Address; customer_id?: string }) =>
    request<CheckoutResult>('/store/checkout', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  lookupOrder: (token: string) =>
    request<Order>(`/store/orders/lookup?token=${encodeURIComponent(token)}`),

  // ── Storefront: 顾客账号 ───────────────────────────────────

  customerRegister: (data: { email: string; password: string; phone?: string; name?: string }) =>
    request<CustomerTokenResponse>('/store/customers/register', {
      method: 'POST',
      body: JSON.stringify(data),
    }),

  customerLogin: (email: string, password: string) =>
    request<CustomerTokenResponse>('/store/customers/login', {
      method: 'POST',
      body: JSON.stringify({ email, password }),
    }),

  customerMe: () =>
    request<Customer>('/store/customers/me', {}, 'customer'),

  customerUpdateMe: (data: { email?: string; phone?: string; name?: string }) =>
    request<Customer>('/store/customers/me', {
      method: 'PUT',
      body: JSON.stringify(data),
    }, 'customer'),

  customerMyOrders: () =>
    request<Order[]>('/store/customers/me/orders', {}, 'customer'),

  customerMyAddresses: () =>
    request<CustomerAddress[]>('/store/customers/me/addresses', {}, 'customer'),

  customerAddAddress: (data: Address & { is_default?: boolean }) =>
    request<CustomerAddress[]>('/store/customers/me/addresses', {
      method: 'POST',
      body: JSON.stringify(data),
    }, 'customer'),

  customerUpdateAddress: (addressId: string, data: Partial<Address> & { is_default?: boolean }) =>
    request<CustomerAddress[]>(`/store/customers/me/addresses/${addressId}`, {
      method: 'PUT',
      body: JSON.stringify(data),
    }, 'customer'),

  customerDeleteAddress: (addressId: string) =>
    request<{ ok: boolean }>(`/store/customers/me/addresses/${addressId}`, {
      method: 'DELETE',
    }, 'customer'),

  // ── 售后：RMA 客诉自助报案 ──────────────────────────────────

  customerSubmitClaim: (data: { order_id: string; batch_id: string; evidence_image_url: string }) =>
    request<SubmitClaimResult>('/store/customers/me/claims', {
      method: 'POST',
      body: JSON.stringify(data),
    }, 'customer'),

  customerMyClaims: () =>
    request<RmaClaim[]>('/store/customers/me/claims', {}, 'customer'),

  // ── Region ──────────────────────────────────────────────────

  createRegion: (data: { code: string; name: string; currency: string; payment_provider_names?: string[] }) =>
    request<OmodulResult>('/settings/create_region', {
      method: 'POST',
      body: JSON.stringify(data),
    }, true),

  // ── Stock Location ──────────────────────────────────────────

  createStockLocation: (data: { name: string; region_code: string; channel_tags?: string[] }) =>
    request<OmodulResult>('/inventory/create_stock_location', {
      method: 'POST',
      body: JSON.stringify(data),
    }, true),

  // ── Settings: Region / Tax ──────────────────────────────────

  updateRegion: (data: { code: string; name?: string; currency?: string; payment_provider_names?: string[]; status?: string }) =>
    omodul('settings', 'update_region', data),

  deleteRegion: (data: { code: string }) =>
    omodul('settings', 'delete_region', data),

  createTaxRate: (data: { region_code: string; name: string; rate_percent: number }) =>
    omodul('settings', 'create_tax_rate', data),

  updateTaxRate: (data: { tax_rate_id: string; name?: string; rate_percent?: number; status?: string }) =>
    omodul('settings', 'update_tax_rate', data),

  deleteTaxRate: (data: { tax_rate_id: string }) =>
    omodul('settings', 'delete_tax_rate', data),

  // ── Customers: accounts / addresses / groups ────────────────

  createUser: (data: { email: string; password: string; name?: string }) =>
    omodul('customers', 'create_user', data, false),

  updateUser: (data: { user_id: string; email?: string; password?: string; name?: string; status?: string }) =>
    omodul('customers', 'update_user', data),

  resetUserPassword: (data: { user_id: string; notification_provider_name?: string }) =>
    omodul('customers', 'reset_user_password', data),

  createCustomer: (data: { email: string; phone?: string; name?: string }) =>
    omodul('customers', 'create_customer', data, false),

  updateCustomer: (data: { customer_id: string; email?: string; phone?: string; name?: string; status?: string }) =>
    omodul('customers', 'update_customer', data),

  addCustomerAddress: (data: { customer_id: string } & Address & { is_default?: boolean }) =>
    omodul('customers', 'add_customer_address', data),

  updateCustomerAddress: (data: { address_id: string } & Partial<Address> & { is_default?: boolean }) =>
    omodul('customers', 'update_customer_address', data),

  deleteCustomerAddress: (data: { address_id: string }) =>
    omodul('customers', 'delete_customer_address', data),

  createCustomerGroup: (data: { name: string }) =>
    omodul('customers', 'create_customer_group', data),

  assignCustomerToGroup: (data: { customer_id: string; group_id: string }) =>
    omodul('customers', 'assign_customer_to_group', data),

  // ── Catalog: variants / options / categories / collections ──

  updateProductVariant: (data: { variant_id: string; sku_code?: string; option_values?: Record<string, string>; reference_price_cents?: number; status?: string }) =>
    omodul('catalog', 'update_product_variant', data),

  deleteProductVariant: (data: { variant_id: string }) =>
    omodul('catalog', 'delete_product_variant', data),

  createProductOption: (data: { product_id: string; name: string }) =>
    omodul('catalog', 'create_product_option', data),

  updateProductOption: (data: { option_id: string; name: string }) =>
    omodul('catalog', 'update_product_option', data),

  deleteProductOption: (data: { option_id: string }) =>
    omodul('catalog', 'delete_product_option', data),

  createProductCategory: (data: { name: string; slug: string; parent_id?: string | null }) =>
    omodul('catalog', 'create_product_category', data),

  updateProductCategory: (data: { category_id: string; name?: string; slug?: string; parent_id?: string | null; status?: string }) =>
    omodul('catalog', 'update_product_category', data),

  deleteProductCategory: (data: { category_id: string }) =>
    omodul('catalog', 'delete_product_category', data),

  createProductCollection: (data: { name: string; slug: string }) =>
    omodul('catalog', 'create_product_collection', data),

  updateProductCollection: (data: { collection_id: string; name?: string; slug?: string; status?: string }) =>
    omodul('catalog', 'update_product_collection', data),

  deleteProductCollection: (data: { collection_id: string }) =>
    omodul('catalog', 'delete_product_collection', data),

  // ── Inventory: price lists / locations / channels ───────────

  createPriceList: (data: { name: string; currency?: string; starts_at?: string | null; ends_at?: string | null }) =>
    omodul('inventory', 'create_price_list', data),

  updatePriceList: (data: { price_list_id: string; name?: string; currency?: string; starts_at?: string | null; ends_at?: string | null; status?: string }) =>
    omodul('inventory', 'update_price_list', data),

  deletePriceList: (data: { price_list_id: string }) =>
    omodul('inventory', 'delete_price_list', data),

  addPricesToList: (data: { price_list_id: string; items: { variant_id: string; price_cents: number }[] }) =>
    omodul('inventory', 'add_prices_to_list', data),

  removePricesFromList: (data: { price_list_id: string; variant_ids: string[] }) =>
    omodul('inventory', 'remove_prices_from_list', data),

  updateStockLocation: (data: { location_id: string; name?: string; region_code?: string; lat?: number; lng?: number; channel_tags?: string[]; status?: string }) =>
    omodul('inventory', 'update_stock_location', data),

  deleteStockLocation: (data: { location_id: string }) =>
    omodul('inventory', 'delete_stock_location', data),

  adjustInventoryLevel: (data: { batch_id: string; delta: number; reason: string }) =>
    omodul('inventory', 'adjust_inventory_level', data),

  createSalesChannel: (data: { name: string }) =>
    omodul('inventory', 'create_sales_channel', data),

  updateSalesChannel: (data: { channel_id: string; name?: string; status?: string }) =>
    omodul('inventory', 'update_sales_channel', data),

  deleteSalesChannel: (data: { channel_id: string }) =>
    omodul('inventory', 'delete_sales_channel', data),

  publishProductsToChannel: (data: { channel_id: string; product_ids: string[] }) =>
    omodul('inventory', 'publish_products_to_channel', data),

  unpublishProductsFromChannel: (data: { channel_id: string; product_ids: string[] }) =>
    omodul('inventory', 'unpublish_products_from_channel', data),

  // ── Marketing: discounts / gift cards ───────────────────────

  createDiscount: (data: { code: string }) =>
    omodul('marketing', 'create_discount', data),

  updateDiscount: (data: { discount_id: string; status: string }) =>
    omodul('marketing', 'update_discount', data),

  deleteDiscount: (data: { discount_id: string }) =>
    omodul('marketing', 'delete_discount', data),

  createDiscountRule: (data: { discount_id: string; rule_type: string; amount_cents?: number; percent?: number; min_subtotal_cents?: number; region_codes?: string[]; valid_from?: string; valid_until?: string; max_uses?: number }) =>
    omodul('marketing', 'create_discount_rule', data),

  updateDiscountRule: (data: { rule_id: string; amount_cents?: number; percent?: number; min_subtotal_cents?: number; region_codes?: string[]; valid_from?: string; valid_until?: string; max_uses?: number }) =>
    omodul('marketing', 'update_discount_rule', data),

  createDiscountCondition: (data: { discount_id: string; condition_type: string; target_id?: string }) =>
    omodul('marketing', 'create_discount_condition', data),

  deleteDiscountCondition: (data: { condition_id: string }) =>
    omodul('marketing', 'delete_discount_condition', data),

  createGiftCard: (data: { code: string; initial_balance_cents: number; currency?: string; expires_at?: string | null }) =>
    omodul('marketing', 'create_gift_card', data),

  updateGiftCard: (data: { gift_card_id: string; status?: string; expires_at?: string | null }) =>
    omodul('marketing', 'update_gift_card', data),

  deleteGiftCard: (data: { gift_card_id: string }) =>
    omodul('marketing', 'delete_gift_card', data),

  // ── Cart: raw omodul writes (admin-authenticated) ───────────
  // 顾客侧购物车流程走上面的 /store/* 封装；这些是底层 omodul 直连端点。

  updateCartStatus: (data: { cart_id: string; status?: string; customer_id?: string; currency?: string }) =>
    omodul('cart', 'update_cart', data),

  setCartRegion: (data: { cart_id: string; region_code: string; currency: string }) =>
    omodul('cart', 'set_cart_region', data),

  applyDiscountToCart: (data: { cart_id: string; code: string }) =>
    omodul('cart', 'apply_discount_to_cart', data),

  removeDiscountFromCart: (data: { cart_id: string; discount_id: string }) =>
    omodul('cart', 'remove_discount_from_cart', data),

  applyGiftCardToCart: (data: { cart_id: string; code: string }) =>
    omodul('cart', 'apply_gift_card_to_cart', data),

  removeGiftCardFromCart: (data: { cart_id: string; gift_card_id: string }) =>
    omodul('cart', 'remove_gift_card_from_cart', data),

  addShippingMethodToCart: (data: { cart_id: string; method_name: string; price_cents: number }) =>
    omodul('cart', 'add_shipping_method_to_cart', data),

  createPaymentSessions: (data: { cart_id: string; provider_names: string[] }) =>
    omodul('cart', 'create_payment_sessions', data),

  updatePaymentSessions: (data: { cart_id: string }) =>
    omodul('cart', 'update_payment_sessions', data),

  setPaymentSession: (data: { cart_id: string; provider_name: string }) =>
    omodul('cart', 'set_payment_session', data),

  // ── Checkout: orders ─────────────────────────────────────────

  authorizePaymentForCart: (data: { cart_id: string }) =>
    omodul('checkout', 'authorize_payment_for_cart', data),

  completeCheckoutRaw: (data: { cart_id: string }) =>
    omodul('checkout', 'complete_checkout', data),

  updateOrder: (data: { order_id: string; status?: string; shipping_address?: Record<string, unknown> }) =>
    omodul('checkout', 'update_order', data),

  cancelOrder: (data: { order_id: string }) =>
    omodul('checkout', 'cancel_order', data),

  archiveOrder: (data: { order_id: string }) =>
    omodul('checkout', 'archive_order', data),

  createDraftOrder: (data: { customer_id?: string; region_code?: string; currency?: string; line_items: { batch_id: string; quantity: number }[]; billing_address?: Record<string, unknown>; shipping_address?: Record<string, unknown> }) =>
    omodul('checkout', 'create_draft_order', data),

  updateDraftOrder: (data: { order_id: string; customer_id?: string; billing_address?: Record<string, unknown>; shipping_address?: Record<string, unknown> }) =>
    omodul('checkout', 'update_draft_order', data),

  deleteDraftOrder: (data: { order_id: string }) =>
    omodul('checkout', 'delete_draft_order', data),

  markDraftOrderPaid: (data: { order_id: string; payment_provider_name?: string; payment_intent_id?: string }) =>
    omodul('checkout', 'mark_draft_order_paid', data),

  // ── Fulfillment ──────────────────────────────────────────────

  createFulfillment: (data: { order_id: string; items: { order_line_item_id: string; quantity: number }[] }) =>
    omodul('fulfillment', 'create_fulfillment', data),

  cancelFulfillment: (data: { fulfillment_id: string }) =>
    omodul('fulfillment', 'cancel_fulfillment', data),

  shipFulfillment: (data: { fulfillment_id: string; provider_name: string; shipment_info: Record<string, unknown> }) =>
    omodul('fulfillment', 'ship_fulfillment', data),

  // ── Aftersales: payment / returns / swaps / claims ──────────

  capturePayment: (data: { order_id: string }) =>
    omodul('aftersales', 'capture_payment', data),

  refundPayment: (data: { order_id: string; amount_cents: number }) =>
    omodul('aftersales', 'refund_payment', data),

  createReturnRequest: (data: { order_id: string; items: { order_line_item_id: string; quantity: number; reason?: string }[] }) =>
    omodul('aftersales', 'create_return_request', data),

  receiveReturn: (data: { return_request_id: string }) =>
    omodul('aftersales', 'receive_return', data),

  cancelReturn: (data: { return_request_id: string }) =>
    omodul('aftersales', 'cancel_return', data),

  createSwap: (data: { order_id: string; return_items: { order_line_item_id: string; quantity: number }[]; new_items: { batch_id: string; quantity: number }[] }) =>
    omodul('aftersales', 'create_swap', data),

  cancelSwap: (data: { swap_id: string }) =>
    omodul('aftersales', 'cancel_swap', data),

  fulfillSwap: (data: { swap_id: string }) =>
    omodul('aftersales', 'fulfill_swap', data),

  processSwapPayment: (data: { swap_id: string }) =>
    omodul('aftersales', 'process_swap_payment', data),

  createClaim: (data: { order_id: string; claim_type?: string; items: { order_line_item_id: string; quantity: number; reason?: string }[]; new_items?: { batch_id: string; quantity: number }[] }) =>
    omodul('aftersales', 'create_claim', data),

  cancelClaim: (data: { claim_id: string }) =>
    omodul('aftersales', 'cancel_claim', data),

  fulfillClaim: (data: { claim_id: string }) =>
    omodul('aftersales', 'fulfill_claim', data),

  // ── Batch jobs ───────────────────────────────────────────────

  createBatchJob: (data: { job_type: string; payload?: Record<string, unknown> }) =>
    omodul('batch', 'create_batch_job', data),

  cancelBatchJob: (data: { batch_job_id: string }) =>
    omodul('batch', 'cancel_batch_job', data),

  // ── hemall 扩展域 (供应链 / 交易锁单 / 物理流转 / 分润结算) ──
  // 鉴权边界见 app/ext/registry.py 的 ADMIN_OPS：仓管/结算侧需要 admin
  // token (omodul 第四参传 true)，顾客自助侧 (加车/结账/集单等) 公开 (传 false)。
  // 统一改造收尾：域名不带独立扩展前缀 (见 app/ext/registry.py 的 DOMAINS
  // 映射说明)；JS 函数名统一以 hemall 为前缀，与后端域名字符串解耦——后端
  // 认的是 omodul('domain', 'name', ...) 的域名字符串，函数名只是前端封装。

  hemallCreateInventoryBatch: (data: { variant_id: string; location_id: string; video_url: string; stock_qty: number; cost_price: number; retail_price: number; expiration_time?: string; supplier_id?: string }) =>
    omodul('supply-chain', 'create_inventory_batch', data, true),

  hemallMarkBatchForDisposal: (data: { batch_id: string; reason?: string }) =>
    omodul('supply-chain', 'mark_batch_for_disposal', data, true),

  hemallBatchSettlement: (data: { batch_id: string; supplier_account: string }) =>
    omodul('supply-chain', 'batch_settlement', data, true),

  hemallCreateCrowdIntent: (data: { variant_id: string; customer_ref: string; prepaid_amount: number }) =>
    omodul('community', 'create_crowd_intent', data, false),

  // 加车/运费/结账统一改造后直接走共享的 addLineItem / storeAddShippingMethod /
  // checkout（见上面 Storefront 分组）——扩展域旧版的 add_line_item_to_cart /
  // cart_shipping_method_set / complete_checkout 已删除，不再有独立的 cart/* 端点。

  hemallProcessSubscription: (data: { customer_ref: string; plan_fee_cents: number; duration_days?: number }) =>
    omodul('membership', 'process_subscription', data, false),

  hemallConfirmBatchPick: (data: { order_line_item_id: string; worker_id: string }) =>
    omodul('fulfillment', 'confirm_batch_pick', data, true),

  hemallToteDepositAndRefund: (data: { tote_id: string; action: 'charge' | 'refund' }) =>
    omodul('aftersales', 'tote_deposit_and_refund', data, false),

  hemallProcessDropReturn: (data: { order_line_item_id: string; reason?: string }) =>
    omodul('aftersales', 'process_drop_return', data, false),

  hemallCommissionNewLocation: (data: { host_id: string; address: string; lat: number; lon: number }) =>
    omodul('inventory', 'commission_new_location', data, true),

  hemallDispatchLaborPayment: (data: { worker_id: string; payout_account: string }) =>
    omodul('settlement', 'dispatch_labor_payment', data, true),

  hemallDispatchHostDividend: (data: { host_id: string; location_id: string; payout_account: string; tote_count: number }) =>
    omodul('settlement', 'dispatch_host_dividend', data, true),

  hemallExecuteAmbientReplenishment: (data: { customer_ref: string; variant_id: string; qty: number; family_size: number; purchase_history: { purchased_at: string; quantity: number }[]; customer_lat: number; customer_lon: number }) =>
    omodul('inventory', 'execute_ambient_replenishment', data, false),

  // ── hemall 扩展 v2.0: 供应商治理 / 全自动仲裁 / 裂变 ──────────────
  // 同样的鉴权约定：仓管/处罚/仲裁执行侧 admin token，供应商/顾客/邻居这类
  // "外部人自助操作" 公开。见 app/ext/registry.py 的 ADMIN_OPS 注释。

  hemallClaimOrigin: (data: { wallet_account: string; spatial_polygon: { type: string; coordinates: number[][][] }; polygon_name?: string }) =>
    omodul('supply-chain', 'claim_origin_workflow', data, false),

  // 小型供应商门户：查询自己的入驻状态。供应商目前没有真正的登录体系，
  // wallet_account 是唯一的"准身份"标识，见后端 app/routers.py 的注释。
  supplierLookup: (walletAccount: string) =>
    request<SupplierStatus>(`/supply-chain/suppliers/lookup?wallet_account=${encodeURIComponent(walletAccount)}`, {}, false),

  hemallExecuteSlashing: (data: { supplier_id: string; order_id: string; penalty_base_amount: number; new_trust_score: number; reason: string }) =>
    omodul('supply-chain', 'execute_slashing_workflow', data, true),

  hemallReportPhantomStock: (data: { order_line_item_id: string; worker_id: string; reason?: string }) =>
    omodul('fulfillment', 'report_phantom_stock_workflow', data, true),

  hemallExecutePeerDelivery: (data: { order_id: string; tote_id: string; neighbor_id: string; bounty_amount: number }) =>
    omodul('fulfillment', 'execute_peer_delivery_workflow', data, false),

  hemallProcessCreditGatedRma: (data: { order_id: string; batch_id: string; user_id: string; user_trust_score: number; batch_anomaly_rate?: number; route_risk?: number }) =>
    omodul('aftersales', 'process_credit_gated_rma_workflow', data, false),

  hemallExecuteLiabilityRouting: (data: { order_id: string; batch_id: string; user_id: string; evidence_image_url: string; vlm_damage_type: string; vlm_severity: number; fraud_probability: number; credibility_decision: 'instant' | 'honeypot' }) =>
    omodul('aftersales', 'execute_liability_routing_workflow', data, true),

  hemallGenerateCrushingOffer: (data: { customer_ref: string; receipt_items: { item: string; qty?: number; price: number }[] }) =>
    omodul('marketing', 'generate_crushing_offer_workflow', data, false),

  // 手写端点 (不是裸 omodul)：execute_ambient_intake_workflow 的 video_stream
  // 是 bytes，HTTP 契约简化成纯文本字段，由后端路由做 str->bytes 转换。
  hemallExecuteAmbientIntake: (data: { video_stream_text: string }) =>
    request<OmodulResult>('/supply-chain/execute_ambient_intake_workflow', {
      method: 'POST',
      body: JSON.stringify(data),
    }, true),

  // 手写端点：不是 omodul，是拉起 autonomous_triage_engine 的信号入口——
  // 公开 (顾客自助报案)，2 秒内跑完 VLM 判损 + 信誉裁决 + 退款/责任方扣款全链路。
  hemallSubmitRmaClaim: (data: { order_id: string; batch_id: string; user_id: string; evidence_image_url: string; user_trust_score: number; route_risk?: number }) =>
    request<OmodulResult>('/aftersales/submit_rma_claim', {
      method: 'POST',
      body: JSON.stringify(data),
    }, false),

  // ── hemall 扩展 v4.0: 微信视频号社交播报 ──────────────────────────
  // 需要 admin token——发一条视频号是运营/引擎的动作，不是顾客自助操作。

  hemallExecuteChannelBroadcast: (data: { batch_id: string; broadcast_type: 'fresh_arrival' | 'clearance'; market_price: number; access_token: string }) =>
    omodul('marketing', 'execute_channel_broadcast_workflow', data, true),

  // ── hemall 扩展 v2.0/v4.0: 只读列表 (运营巡查用) ────────────────────

  adminListHemallSuppliers: () =>
    request<Array<{ id: string; wallet_account: string; spatial_polygon: unknown; trust_score: number; escrow_balance: number; status: string }>>('/admin/supply-chain/suppliers', {}, true),

  adminListHemallRmaClaims: () =>
    request<Array<{ id: string; order_id: string; batch_id: string; user_id: string; evidence_image_url: string | null; vlm_damage_type: string | null; vlm_severity: number | null; decision: string | null; liable_party: string | null; created_at: string }>>('/admin/aftersales/rma-claims', {}, true),

  adminListHemallBroadcastLogs: () =>
    request<Array<{ id: string; batch_id: string; broadcast_type: string; llm_copywriting: string; wechat_feed_id: string | null; mini_program_path: string; status: string; error_message: string | null; created_at: string; published_at: string | null }>>('/admin/marketing/broadcast-logs', {}, true),

  // ── hemall 扩展 v5.0: 全局定价与冷启动预言机 ───────────────────────
  // 众包核销 (顾客上传小票) 和反向竞标 (供应商申报报价) 都是"外部人自助
  // 操作"，跟 claim_origin_workflow / process_credit_gated_rma_workflow 同类，公开无需 admin token。

  // 手写端点：receipt_image 是 bytes，HTTP 契约简化成纯文本字段，由后端
  // 路由做 str->bytes 转换 (跟 hemallExecuteAmbientIntake 同一个套路)。
  // customer_id (不是 user_id)：统一改造后奖励余额落在真实 customer.system_
  // balance 上，需要一个真实 customer.id，不再是自由字符串。
  hemallRewardCrowdsourcedBenchmark: (data: { customer_id: string; receipt_image_text: string }) =>
    request<OmodulResult>('/marketing/reward_crowdsourced_benchmark_workflow', {
      method: 'POST',
      body: JSON.stringify(data),
    }, false),

  hemallSubmitSupplierReverseAuction: (data: { batch_id: string; supplier_bid_price_per_gram: number; batch_cost_price: number }) =>
    omodul('supply-chain', 'submit_supplier_reverse_auction_workflow', data, false),

  // ── hemall 扩展 v5.0: 只读列表 (运营巡查用) ──────────────────────────

  adminListHemallPriceBenchmarks: () =>
    request<Array<{ id: string; variant_id: string | null; raw_item_name: string | null; source_type: string; competitor_name: string | null; raw_price_cents: number; raw_unit: string; normalized_price_per_unit: number | null; captured_at: string }>>('/admin/marketing/price-benchmarks', {}, true),

  adminListHemallProbeLogs: () =>
    request<Array<{ id: string; batch_id: string; probe_price_cents: number; traffic_exposure: number; observed_sales_velocity: number | null; status: string; created_at: string; resolved_at: string | null }>>('/admin/marketing/probe-logs', {}, true),

  // ── hemall 扩展 v6.0: 抖音拓客与数字领主 ───────────────────────────
  // 云加盟认领是"外部人自助操作" (跟 claim_origin_workflow 同类)，公开无需
  // admin token；领主契约册封 ("500 单点火"阈值由运营后台自行核实) 和转化
  // 落账 (公开的话任何人都能编造推荐关系骗分润) 都需要 admin token。

  hemallProcessCloudFranchiseClaim: (data: { douyin_uid: string; address: string; lat: number; lon: number }) =>
    omodul('growth', 'process_cloud_franchise_claim_workflow', data, false),

  hemallBindDigitalLordContract: (data: { douyin_uid: string; location_id: string }) =>
    omodul('growth', 'bind_digital_lord_contract_workflow', data, true),

  hemallRecordDouyinConversion: (data: { order_id: string; douyin_uid?: string }) =>
    omodul('growth', 'record_douyin_conversion_workflow', data, true),

  // ── hemall 扩展 v6.0: 只读列表 (运营巡查用) ──────────────────────────

  adminListHemallAffiliateContracts: () =>
    request<Array<{ id: string; douyin_uid: string; contract_type: string; bound_entity_id: string; commission_logic: unknown; status: string; created_at: string }>>('/admin/growth/affiliate-contracts', {}, true),

  adminListHemallConversionLogs: () =>
    request<Array<{ id: string; order_id: string; douyin_uid: string; contract_id: string | null; dividend_amount_cents: number; settlement_status: string; created_at: string }>>('/admin/growth/conversion-logs', {}, true),

  // ── 补天计划 Task 2.1/3.1: 位置 Feed + 零号探针 ──────────────────────

  // 位置 Feed 流 (公开，零登录)：按当前坐标找最近 active 微仓，只返回有货且
  // 在安全货架期内的批次——"人找货"到"地理位置找货"。Phase 7 升维：带
  // X-Device-Id 设备指纹，后端按购买轨迹把关联商品 (boosted=true) 插队。
  getNearbyFeed: (lat: number, lon: number, limit?: number) =>
    request<{
      nearest_location: { id: string; distance_km: number } | null;
      safety_margin_hours: number;
      behavior_boosted: boolean;
      batches: Array<{
        batch_id: string;
        variant_id: string;
        product_id: string;
        title: string;
        sku_code: string;
        retail_price_cents: number;
        stock_qty: number;
        expiration_time: string | null;
        video_url: string | null;
        location_name: string;
        affinity: number;
        boosted: boolean;
      }>;
    }>(
      `/store/nearby-feed?lat=${lat}&lon=${lon}${limit ? `&limit=${limit}` : ''}`,
      { method: 'GET', headers: { 'X-Device-Id': deviceId() } },
      false,
    ),

  // 零号探针触发器 (Admin Ops)：运营在批次上架时手动激活试探单做市。
  triggerInitialProbe: (data: { batch_id: string; initial_price: number }) =>
    omodul('marketing', 'trigger_initial_probe_workflow', data, true),
};

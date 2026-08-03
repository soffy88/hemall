/** hemall 前端 — 后端 API 类型定义 */

export interface TokenResponse {
  access_token: string;
  token_type: string;
  user_id: string;
  email: string;
}

export interface AuthUser {
  id: string;
  email: string;
}

export interface CustomerTokenResponse {
  access_token: string;
  token_type: string;
  customer_id: string;
  email: string;
}

export interface Health {
  status: string;
  version: string;
  database: 'up' | 'down';
}

export interface OmodulResult {
  status: 'completed' | 'failed' | 'cancelled';
  error?: { type: string; message: string } | null;
  [key: string]: unknown;
}

// ── Product types ───────────────────────────────────────────────

export interface InventoryBatch {
  id: string;
  batch_no: string;
  stock_qty: number;
  reserved_qty: number;
  available_qty: number;
  retail_price_cents: number;
  cost_price_cents: number;
  currency: string;
  location_id: string;
  status: string;
}

export interface ProductVariant {
  id: string;
  sku_code: string;
  option_values: Record<string, string>;
  reference_price_cents: number | null;
  status: string;
  batches: InventoryBatch[];
  total_stock: number;
  min_price_cents: number | null;
}

export interface ProductOption {
  id: string;
  name: string;
}

export interface Product {
  id: string;
  title: string;
  slug: string;
  description: string | null;
  category_id: string | null;
  status: string;
  created_at: string;
  updated_at: string | null;
  options: ProductOption[];
  variants: ProductVariant[];
  total_stock: number;
  min_price_cents: number | null;
}

// ── Order types ─────────────────────────────────────────────────

export interface OrderLineItem {
  id: string;
  batch_id: string | null;
  quantity: number;
  unit_price_cents: number;
  line_total_cents: number;
  product_title: string | null;
  variant_sku: string | null;
  option_values: Record<string, string> | null;
}

// ── RMA 客诉自助报案 ───────────────────────────────────────────────

export interface RmaClaim {
  id: string;
  order_id: string;
  batch_id: string | null;
  evidence_image_url: string | null;
  vlm_damage_type: string | null;
  vlm_severity: number | null;
  decision: 'instant_refund' | 'drop_to_bin' | 'rejected' | null;
  liable_party: string | null;
  refund_amount_cents: number | null;
  created_at: string;
}

export interface SubmitClaimResult {
  claim_id: string;
  decision: 'instant_refund' | 'drop_to_bin' | 'rejected';
  liable_party: string | null;
  refund_amount_cents: number | null;
}

// ── 小型供应商门户 ─────────────────────────────────────────────────

export interface SupplierStatus {
  id: string;
  wallet_account: string;
  polygon_name: string | null;
  trust_score: number;
  escrow_balance: number;
  status: string;
  created_at: string;
}

export interface Address {
  recipient_name: string;
  phone: string;
  address_line1: string;
  address_line2?: string;
  city: string;
  region_code?: string;
  postal_code: string;
}

export interface Order {
  id: string;
  cart_id: string;
  customer_id: string | null;
  region_code: string | null;
  currency: string;
  status: string;
  subtotal_cents: number;
  discount_cents: number;
  tax_cents: number;
  shipping_cents: number;
  grand_total_cents: number;
  payment_provider_name: string | null;
  billing_address: Address | null;
  shipping_address: Address | null;
  created_at: string;
  updated_at: string | null;
  promised_delivery_at: string | null;
  sla_compensated_at: string | null;
  line_items: OrderLineItem[];
}

// ── Customer types ──────────────────────────────────────────────

export interface Customer {
  id: string;
  email: string;
  phone: string | null;
  name: string | null;
  customer_group_id: string | null;
  status: string;
  created_at: string;
  order_count: number;
  total_spent_cents: number;
}

// ── Cart types ──────────────────────────────────────────────────

export interface CartLineItem {
  id: string;
  batch_id: string;
  quantity: number;
  unit_price_cents: number;
  line_total_cents: number;
  product_title: string | null;
  variant_sku: string | null;
  option_values: Record<string, string> | null;
  available_qty: number;
}

export interface CartDiscount {
  id: string;
  discount_id: string;
  code: string;
  applied_amount_cents: number;
}

export interface CartGiftCard {
  id: string;
  gift_card_id: string;
  code: string;
  applied_amount_cents: number;
}

export interface Cart {
  id: string;
  customer_id: string | null;
  region_code: string | null;
  currency: string;
  status: string;
  subtotal_cents: number;
  discount_cents: number;
  tax_cents: number;
  shipping_cents: number;
  grand_total_cents: number;
  billing_address: Address | null;
  shipping_address: Address | null;
  created_at: string;
  line_items: CartLineItem[];
  discounts: CartDiscount[];
  gift_cards: CartGiftCard[];
}

// ── Dashboard KPI ───────────────────────────────────────────────

export interface DashboardKPIs {
  total_orders: number;
  total_revenue_cents: number;
  pending_orders: number;
  revenue_30d_cents: number;
  total_products: number;
  published_products: number;
  total_customers: number;
}

// ── Storefront types ────────────────────────────────────────────

export interface StorefrontProduct {
  id: string;
  title: string;
  slug: string;
  description: string | null;
  category_id: string | null;
  variants: ProductVariant[];
  total_stock: number;
  min_price_cents: number | null;
}

export interface Region {
  code: string;
  name: string;
  currency: string;
}

export interface RecommendedProduct {
  product_id: string;
  name: string;
  main_image_url: string | null;
  selling_price_cents: number | null;
  rating_avg: number;
  sold_count: number;
  reason: string | null;
}

export interface RecommendationResult {
  products: RecommendedProduct[];
  algorithm: string;
  total: number;
  personalized: boolean;
  refresh_time: string;
}

export interface CheckoutResult {
  order_id: string;
  grand_total_cents: number;
  receipt_token: string;
}

// ── Phase 9: BFF v9.0 做市量化 Feed 契约 ──────────────────────
// 前端绝不拉取多余富文本详情；后端把做市属性量化成标量字段，前端
// 根据 tag_type 在流 (HeroCard) 和网格 (GridItem) 之间切换渲染引擎。

export type FeedTagType = 'clearance' | 'fresh' | 'standard';

export interface FeedItem {
  batch_id: string;
  sku_name: string;
  tag_type: FeedTagType;
  retail_price: number;      // 现价 (分)
  benchmark_price: number;   // 爬虫基准价 (分) → 划线价
  stock_qty: number;         // 真实物理库存 (可用量)
  observed_velocity: number; // 过去一小时流速
  media_url: string | null;  // 实景视频或图片
  affinity_boosted: boolean; // 行为算法强制插队
}

export interface NearbyFeedResponse {
  location_context: {
    node_id: string | null;
    node_name: string | null;
    distance_meters: number | null;
    user_system_balance: number; // 分 (15元 → 1500)
  };
  feed_items: FeedItem[];
}

export interface CartLockResponse {
  status: 'locked' | 'failed';
  batch_id: string;
  reason?: 'oversold' | 'insufficient';
  available_qty?: number;
  qty?: number;
  locked_until?: string; // ISO timestamp
  ttl_seconds?: number;
}

// 薛定谔购物车项 (前端状态机)
export interface OptimisticCartItem {
  batch_id: string;
  sku_name: string;
  retail_price: number;
  qty: number;
  locked_until: number; // Unix 毫秒
  status: 'locking' | 'locked' | 'failed' | 'expired';
}

// 弱网离线核销凭证 (PWA Service Worker 硬缓存)
export interface PickupTicket {
  order_id: string;
  pickup_code: string;      // 加密 JWT 提货码
  node_name: string;
  grand_total_cents: number;
  issued_at: string;
  expires_at: string;
}

// ── Marketing ───────────────────────────────────────────────────

export interface DiscountRule {
  id: string;
  rule_type: string;
  amount_cents: number | null;
  percent: number | null;
  min_subtotal_cents: number | null;
  region_codes: string[] | null;
  valid_from: string | null;
  valid_until: string | null;
  max_uses: number | null;
  uses_count: number;
}

export interface DiscountCondition {
  id: string;
  condition_type: string;
  target_id: string | null;
}

export interface Discount {
  id: string;
  code: string;
  status: string;
  created_at: string;
  updated_at: string | null;
  rule: DiscountRule | null;
  conditions: DiscountCondition[];
}

export interface GiftCard {
  id: string;
  code: string;
  initial_balance_cents: number;
  balance_cents: number;
  currency: string;
  status: string;
  expires_at: string | null;
  created_at: string;
  updated_at: string | null;
}

// ── Aftersales ──────────────────────────────────────────────────

export interface ReturnRequest {
  id: string;
  order_id: string;
  status: string;
  items: { order_line_item_id: string; quantity: number; reason?: string }[];
  refund_amount_cents: number | null;
  created_at: string;
  updated_at: string | null;
}

export interface Swap {
  id: string;
  order_id: string;
  status: string;
  return_items: { order_line_item_id: string; quantity: number }[];
  new_items: { batch_id: string; quantity: number }[];
  price_difference_cents: number;
  payment_status: string;
  fulfillment_id: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface Claim {
  id: string;
  order_id: string;
  status: string;
  claim_type: string;
  items: { order_line_item_id: string; quantity: number; reason?: string }[];
  refund_amount_cents: number | null;
  new_items: { batch_id: string; quantity: number }[] | null;
  fulfillment_id: string | null;
  created_at: string;
  updated_at: string | null;
}

export interface Fulfillment {
  id: string;
  order_id: string;
  status: string;
  items: { order_line_item_id: string; quantity: number }[];
  provider_name: string | null;
  tracking_number: string | null;
  carrier: string | null;
  created_at: string;
  updated_at: string | null;
}

// ── Inventory settings ─────────────────────────────────────────

export interface PriceListItem {
  id: string;
  variant_id: string;
  price_cents: number;
  sku_code: string | null;
}

export interface PriceList {
  id: string;
  name: string;
  currency: string;
  starts_at: string | null;
  ends_at: string | null;
  status: string;
  created_at: string;
  items: PriceListItem[];
}

export interface StockLocation {
  id: string;
  name: string;
  region_code: string;
  lat: number | null;
  lng: number | null;
  channel_tags: string[];
  status: string;
  created_at: string;
}

export interface SalesChannel {
  id: string;
  name: string;
  status: string;
  created_at: string;
  product_count: number;
}

// ── Settings ────────────────────────────────────────────────────

export interface AdminRegion {
  code: string;
  name: string;
  currency: string;
  payment_provider_names: string[];
  status: string;
  created_at: string;
}

export interface TaxRate {
  id: string;
  region_code: string;
  name: string;
  rate_percent: number;
  status: string;
  created_at: string;
}

// ── Catalog taxonomy ────────────────────────────────────────────

export interface ProductCategory {
  id: string;
  name: string;
  slug: string;
  parent_id: string | null;
  parent_name: string | null;
  status: string;
  created_at: string;
}

export interface ProductCollection {
  id: string;
  name: string;
  slug: string;
  status: string;
  created_at: string;
  product_count: number;
}

// ── Customers: groups / addresses ────────────────────────────────

export interface CustomerGroup {
  id: string;
  name: string;
  created_at: string;
  member_count: number;
}

export interface CustomerAddress extends Address {
  id: string;
  customer_id: string;
  is_default: boolean;
  created_at: string;
}

// ── Admin accounts ──────────────────────────────────────────────

export interface AppUser {
  id: string;
  email: string;
  name: string | null;
  status: string;
  created_at: string;
  updated_at: string | null;
}

// ── Batch jobs ───────────────────────────────────────────────────

export interface BatchJob {
  id: string;
  job_type: string;
  status: string;
  payload: Record<string, unknown> | null;
  result: Record<string, unknown> | null;
  created_at: string;
  updated_at: string | null;
}

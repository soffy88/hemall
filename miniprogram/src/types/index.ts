// 宋鲜生小程序类型定义（基于后端 API 响应）

// ==================== 基础类型 ====================

export interface ApiResponse<T = any> {
    data?: T
    error?: {
        message: string
        code?: string
    }
}

export interface PaginatedResponse<T> {
    items: T[]
    total: number
    page: number
    page_size: number
}

// ==================== 认证相关 ====================

export interface WeChatLoginRequest {
    code: string
}

export interface WeChatLoginResponse {
    access_token: string
    refresh_token?: string
    token_type: string
    expires_in: number
    customer_id: string
    email: string
    wechat_bound: boolean
}

export interface CustomerTokenResponse {
    access_token: string
    token_type: string
    expires_in: number
    customer_id: string
    email: string
}

export interface CustomerProfile {
    id: string
    email: string
    name: string
    phone: string
    avatar_url?: string
    wechat_bound: boolean
    created_at: string
    updated_at: string
}

// ==================== 分类 ====================

export interface Category {
    id: string
    name: string
    slug: string
    count: number
}

// ==================== 商品 ====================

export interface ProductBatch {
    id: string
    batch_no: string
    available_qty: number
    retail_price_cents: number
    currency: string
    shelf_image_url?: string
    media_assets?: any
}

export interface ProductVariant {
    id: string
    sku_code: string
    option_values: Record<string, any>
    reference_price_cents: number
    batches: ProductBatch[]
    total_stock: number
    min_price_cents: number | null
}

export interface Product {
    id: string
    title: string
    slug: string
    description: string
    category_id: string
    category_name: string
    category_slug: string
    variants: ProductVariant[]
    total_stock: number
    min_price_cents: number | null
}

export interface ProductListParams {
    search?: string
    min_price?: number
    max_price?: number
    limit?: number
    offset?: number
    category_id?: string
}

// ==================== 购物车 ====================

export interface CartAddress {
    recipient_name: string
    phone: string
    address_line1: string
    address_line2: string
    city: string
    region_code: string
    postal_code: string
}

export interface CartLineItem {
    id: string
    batch_id: string | null
    variant_id: string
    quantity: number
    unit_price_cents: number
    line_total_cents: number
    product_title: string
    variant_sku: string
    option_values: Record<string, any>
    available_qty: number
    preorder_qty_preview: number
}

export interface CartDiscount {
    id: string
    discount_id: string
    code: string
    applied_amount_cents: number
}

export interface CartGiftCard {
    id: string
    gift_card_id: string
    code: string
    applied_amount_cents: number
}

export interface Cart {
    id: string
    customer_id: string | null
    region_code: string | null
    currency: string
    status: string
    subtotal_cents: number
    discount_cents: number
    tax_cents: number
    shipping_cents: number
    grand_total_cents: number
    billing_address: CartAddress | null
    shipping_address: CartAddress | null
    created_at: string
    updated_at: string
    line_items: CartLineItem[]
    discounts: CartDiscount[]
    gift_cards: CartGiftCard[]
}

export interface CreateCartRequest {
    region_code: string
    currency: string
}

export interface CreateCartResponse {
    cart_id: string
    cart_token: string | null
    currency: string
    region_code: string
}

export interface AddLineItemRequest {
    cart_id: string
    batch_id?: string
    variant_id?: string
    quantity: number
}

export interface AddLineItemResponse {
    line_item_id: string
    new_quantity: number
}

export interface UpdateLineItemRequest {
    cart_id: string
    line_item_id: string
    quantity: number
}

export interface UpdateLineItemResponse {
    line_item_id: string
    new_quantity: number
}

export interface DeleteLineItemRequest {
    cart_id: string
    line_item_id: string
}

export interface SetCartAddressRequest {
    cart_id: string
    address: CartAddress
}

export interface SetCartCustomerRequest {
    cart_id: string
    customer_id: string
}

export interface ApplyDiscountRequest {
    cart_id: string
    code: string
}

export interface ApplyDiscountResponse {
    discount_id: string
    discount_cents: number
    grand_total_cents: number
}

export interface ApplyGiftCardRequest {
    cart_id: string
    code: string
}

export interface ApplyGiftCardResponse {
    gift_card_id: string
    applied_cents: number
    amount_due_cents: number
}

// ==================== 结算/订单 ====================

export interface CheckoutRequest {
    cart_id: string
    billing_address?: CartAddress
    shipping_address?: CartAddress
    customer_id?: string
    provider?: string
}

export interface JSAPIParams {
    appId: string
    timeStamp: string
    nonceStr: string
    package: string
    signType: string
    paySign: string
}

export interface PaymentResponse {
    payment_id: string
    order_id: string
    amount: string
    amount_cents: number
    currency: string
    provider: string
    status: string
    qr_code?: string
    code_url?: string
    client_secret?: string
    jsapi?: JSAPIParams
    provider_result?: any
}

export interface CheckoutResponse {
    order_id: string
    grand_total_cents: number
    currency: string
    receipt_token: string
    payment: PaymentResponse
}

export interface OrderLineItem {
    id: string
    batch_id: string | null
    variant_id: string
    quantity: number
    preorder_qty: number
    unit_price_cents: number
    line_total_cents: number
    product_title: string
    variant_sku: string
    option_values: Record<string, any>
}

export interface Order {
    id: string
    cart_id: string | null
    customer_id: string
    region_code: string | null
    currency: string
    status: string
    subtotal_cents: number
    discount_cents: number
    tax_cents: number
    shipping_cents: number
    grand_total_cents: number
    payment_provider_name: string | null
    billing_address: CartAddress | null
    shipping_address: CartAddress | null
    created_at: string
    updated_at: string
    promised_delivery_at: string | null
    sla_compensated_at: string | null
    line_items: OrderLineItem[]
}

// ==================== 地址 ====================

export interface CustomerAddress {
    id: string
    customer_id: string
    recipient_name: string
    phone: string
    address_line1: string
    address_line2: string
    city: string
    region_code: string
    postal_code: string
    is_default: boolean
    created_at: string
    updated_at: string
}

export interface AddAddressRequest {
    recipient_name: string
    phone: string
    address_line1: string
    address_line2: string
    city: string
    region_code: string
    postal_code: string
    is_default: boolean
}

export interface UpdateAddressRequest {
    recipient_name?: string
    phone?: string
    address_line1?: string
    address_line2?: string
    city?: string
    region_code?: string
    postal_code?: string
    is_default?: boolean
}

// ==================== 会员/积分/储值 ====================

export interface MembershipSummary {
    customer_id: string
    tier: string
    points_balance: number
    stored_value_balance_cents: number
    voucher_balance_cents: number
}

export interface PointsEntry {
    id: string
    kind: string
    status: string
    order_id: string | null
    ref_id: string | null
    amount_cents: number
    base_cents: number
    balance_after_cents: number
    idempotency_key: string
    created_at: string
}

export interface StoredValueEntry {
    id: string
    kind: string
    status: string
    order_id: string | null
    ref_id: string | null
    amount_cents: number
    balance_after_cents: number
    external_payment_ref: string | null
    note: string
    created_at: string
}

// ==================== 配送方式 ====================

export interface ShippingMethod {
    id: string
    name: string
    type: 'delivery' | 'pickup'
    fee_cents: number
    min_order_cents: number
    radius_km: number
    is_active: boolean
    config_json: any
}

// ==================== 客服 ====================

export interface CustomerServiceConfig {
    type: 'phone' | 'wechat' | 'url' | 'mini_program'
    content: string
    label: string
}

// ==================== 订单补差（称重商品多退少补） ====================

export interface OrderAdjustment {
    id: string
    order_id: string
    line_item_id: string
    adjust_type: 'supplement' | 'refund'
    weight_grams: number
    amount_cents: number
    created_at: string
}
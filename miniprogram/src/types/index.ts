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

export interface PickupPoint {
    id: string
    name: string
    address: string
    phone: string
    latitude: number
    longitude: number
    business_hours: { start: string; end: string }
    time_slots: PickupTimeSlot[]
    is_active: boolean
}

export interface PickupTimeSlot {
    id: string
    start_time: string  // HH:mm
    end_time: string    // HH:mm
    capacity: number
    booked_count: number
    is_available: boolean
}

export interface PickupSettings {
    points: PickupPoint[]
    default_point_id?: string
}

// ==================== 配送时段 ====================

export interface DeliverySlot {
    date: string
    start: string
    end: string
    capacity: number
    booked: number
    remaining: number
    label: string
    eta_text: string
}

export interface DeliveryOptions {
    enabled: boolean
    ok: boolean
    reason: string
    fee_cents: number
    distance_km: number | null
    in_range: boolean
    min_order_cents: number
    subtotal_cents: number
    open_now: boolean
    slot: DeliverySlot | null
    slots: DeliverySlot[]
    store: {
        name: string
        address: string
    }
    note: string
}

// ==================== 结算请求扩展 ====================

export interface CheckoutRequest {
    cart_id: string
    billing_address?: CartAddress
    shipping_address?: CartAddress
    customer_id?: string
    provider?: string
    pickup_point_id?: string
    pickup_time_slot_id?: string
    delivery_slot_id?: string
}

// ==================== 个性化推荐（再来一单、常买清单、共购推荐、菜谱） ====================

export interface ReorderLastOrder {
    id: string
    status: string
    created_at: string
    line_items: ReorderLineItem[]
}

export interface ReorderLineItem {
    variant_id: string
    batch_id: string | null
    quantity: number
    pricing_mode: string
    unit_price_cents: number
    unit_price_cents_per_jin: number | null
    price_basis_grams: number
    weight_option_label: string | null
    product_id: string
    title: string
}

export interface ReorderApplyRequest {
    order_id: string
    variant_qty_overrides?: Record<string, number>
    skip_variant_ids?: string[]
}

export interface ReorderApplyResponse {
    cart_id: string
    added_lines: number
    skipped_lines: number
    message: string
}

export interface FrequentItem {
    variant_id: string
    sku_code: string
    product_name: string
    total_qty: number
    purchase_count: number
    avg_price_cents: number
    last_purchase_at: string
    image_url: string | null
}

export interface CopurchaseItem {
    variant_id: string
    sku_code: string
    product_title: string
    option_values: Record<string, any>
    cooccurrence_count: number
    image_url: string | null
    min_price_cents: number | null
}

export interface HomepageRecommendation {
    variant_id: string
    sku_code: string
    product_title: string
    option_values: Record<string, any>
    score: number
    image_url: string | null
    min_price_cents: number | null
}

export interface Recipe {
    id: string
    title: string
    description: string
    cover_image_url: string | null
    servings: number
    prep_time_minutes: number
    cook_time_minutes: number
    difficulty: 'easy' | 'medium' | 'hard'
    tags: string[]
    ingredients: RecipeIngredient[]
    steps: RecipeStep[]
    is_published: boolean
    created_at: string
    updated_at: string
}

export interface RecipeIngredient {
    variant_id: string
    product_title: string
    quantity: number
    unit: string
    note: string | null
}

export interface RecipeStep {
    step_number: number
    instruction: string
    image_url: string | null
}

// ==================== 客服 ====================

export interface CustomerServiceConfig {
    type: 'phone' | 'wechat' | 'url' | 'mini_program'
    content: string
    label: string
}

// ==================== 团购（拼单） ====================

export interface PintuanGroup {
    group_id: string
    variant_id: string
    creator_id: string
    pintuan_price_cents: number
    target_size: number
    status: 'open' | 'formed' | 'expired'
    participant_count: number
    expires_at: string
    created_at: string
    updated_at: string
    variant?: {
        id: string
        sku_code: string
        option_values: Record<string, any>
        product_title?: string
        title?: string
        slug?: string
        image_url?: string
    }
    participants?: PintuanParticipant[]
    total_pick_qty?: number
}

export interface PintuanParticipant {
    customer_id: string
    quantity: number
    created_at: string
    nickname_masked?: string
    name?: string
    phone?: string
}

export interface PintuanGroupListResponse {
    items: PintuanGroup[]
    limit: number
    offset: number
}

export interface CreatePintuanGroupRequest {
    variant_id: string
    pintuan_price_cents: number
    target_size: number
    duration_minutes?: number
    quantity?: number
}

export interface JoinPintuanGroupRequest {
    quantity?: number
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

// ==================== 售后申请 ====================

export interface AftersalesClaimRequest {
    order_id: string
    line_item_id: string
    reason: string
    description: string
    images: string[]
    quantity: number
}

// ==================== 轮播图 ====================

export interface BannerItem {
    id: string
    image_url: string
    link_type: 'none' | 'product' | 'category' | 'pintuan' | 'pickup' | 'url'
    link_target: string
    title: string
    subtitle: string
    sort_order: number
    is_active: boolean
    start_at: string | null
    end_at: string | null
}
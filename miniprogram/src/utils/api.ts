// 宋鲜生小程序 API 服务层

import { http } from './request'
import type {
    Category,
    Product,
    ProductListParams,
    Cart,
    CreateCartRequest,
    CreateCartResponse,
    AddLineItemRequest,
    AddLineItemResponse,
    UpdateLineItemRequest,
    UpdateLineItemResponse,
    DeleteLineItemRequest,
    SetCartAddressRequest,
    SetCartCustomerRequest,
    ApplyDiscountRequest,
    ApplyDiscountResponse,
    ApplyGiftCardRequest,
    ApplyGiftCardResponse,
    CheckoutRequest,
    CheckoutResponse,
    Order,
    CustomerAddress,
    AddAddressRequest,
    UpdateAddressRequest,
    MembershipSummary,
    PointsEntry,
    StoredValueEntry,
    ShippingMethod,
    CustomerServiceConfig,
    OrderAdjustment,
    WeChatLoginResponse,
    CustomerProfile,
    ProductBatch,
    ProductVariant,
    PintuanGroup,
    PintuanGroupListResponse,
    CreatePintuanGroupRequest,
    JoinPintuanGroupRequest,
    PintuanParticipant,
    PickupPoint,
    PickupTimeSlot,
    PickupSettings,
    DeliveryOptions,
    DeliverySlot,
    ReorderLastOrder,
    ReorderApplyRequest,
    ReorderApplyResponse,
    FrequentItem,
    CopurchaseItem,
    HomepageRecommendation,
    Recipe,
    RecipeIngredient,
    RecipeStep,
    BannerItem,
    AftersalesClaimRequest
} from '../types'

// ==================== 认证 API ====================

export const authApi = {
    // 微信登录
    wechatLogin(code: string) {
        return http.post<WeChatLoginResponse>('/store/customers/wechat-login', { code })
    },

    // 微信手机号登录
    wechatPhoneLogin(code: string, referralCode?: string) {
        return http.post<WeChatLoginResponse>('/store/customers/wechat-phone-login', { code, referral_code: referralCode })
    },

    // 微信绑定（已登录用户）
    wechatBind(code: string) {
        return http.post<{ wechat_bound: boolean }>('/store/customers/wechat-bind', { code })
    },

    // 获取当前用户信息
    getProfile() {
        return http.get<CustomerProfile>('/store/customers/me')
    },

    // 更新用户信息
    updateProfile(data: Partial<CustomerProfile>) {
        return http.put<CustomerProfile>('/store/customers/me', data)
    },

    // 订阅消息下发
    sendSubscribeMessage(templateId: string, page: string, data: Record<string, { value: string }>) {
        return http.post<{ errcode: number; errmsg: string }>('/store/customers/subscribe-message', {
            template_id: templateId,
            page,
            data,
        })
    },

    // 生成小程序码
    getWxacode(scene: string, page?: string, width = 430, autoColor = false, lineColor?: { r: number; g: number; b: number }, isHyaline = false) {
        return http.post<ArrayBuffer>('/store/wxacode', {
            scene,
            page,
            width,
            auto_color: autoColor,
            line_color: lineColor,
            is_hyaline: isHyaline,
        }, { responseType: 'arraybuffer' })
    }
}

// ==================== 分类 API ====================

export const categoryApi = {
    // 获取分类列表
    getCategories() {
        return http.get<Category[]>('/store/categories')
    }
}

// ==================== 商品 API ====================

export const productApi = {
    // 获取商品列表
    getProducts(params: ProductListParams = {}) {
        const searchParams = new URLSearchParams()
        if (params.search) searchParams.set('search', params.search)
        if (params.min_price !== undefined) searchParams.set('min_price', String(params.min_price))
        if (params.max_price !== undefined) searchParams.set('max_price', String(params.max_price))
        if (params.limit) searchParams.set('limit', String(params.limit))
        if (params.offset) searchParams.set('offset', String(params.offset))
        const query = searchParams.toString()
        return http.get<Product[]>(`/store/products${query ? '?' + query : ''}`)
    },

    // 获取商品详情
    getProduct(productId: string) {
        return http.get<Product>(`/store/products/${productId}`)
    }
}

// ==================== 购物车 API ====================

export const cartApi = {
    // 创建购物车
    createCart(data: CreateCartRequest) {
        return http.post<CreateCartResponse>('/store/carts', data)
    },

    // 获取购物车详情
    getCart(cartId: string) {
        return http.get<Cart>(`/store/carts/${cartId}`)
    },

    // 加购
    addLineItem(data: AddLineItemRequest) {
        return http.post<AddLineItemResponse>('/store/carts/line-items', data)
    },

    // 改量
    updateLineItem(data: UpdateLineItemRequest) {
        return http.put<UpdateLineItemResponse>('/store/carts/line-items', data)
    },

    // 删行
    deleteLineItem(data: DeleteLineItemRequest) {
        return http.delete<{ ok: boolean }>('/store/carts/line-items', data)
    },

    // 设置账单地址
    setBillingAddress(data: SetCartAddressRequest) {
        return http.post<{ ok: boolean }>('/store/carts/billing-address', data)
    },

    // 设置收货地址
    setShippingAddress(data: SetCartAddressRequest) {
        return http.post<{ ok: boolean }>('/store/carts/shipping-address', data)
    },

    // 绑定购物车到用户
    setCustomer(data: SetCartCustomerRequest) {
        return http.post<{ ok: boolean }>('/store/carts/customer', data)
    },

    // 使用优惠码
    applyDiscount(data: ApplyDiscountRequest) {
        return http.post<ApplyDiscountResponse>('/store/carts/discount', data)
    },

    // 移除优惠
    removeDiscount(cartId: string, discountId: string) {
        return http.delete<{ ok: boolean }>('/store/carts/discount', { cart_id: cartId, discount_id: discountId })
    },

    // 使用礼品卡
    applyGiftCard(data: ApplyGiftCardRequest) {
        return http.post<ApplyGiftCardResponse>('/store/carts/gift-card', data)
    },

    // 移除礼品卡
    removeGiftCard(cartId: string, giftCardId: string) {
        return http.delete<{ ok: boolean }>('/store/carts/gift-card', { cart_id: cartId, gift_card_id: giftCardId })
    }
}

// ==================== 结算/订单 API ====================

export const orderApi = {
    // 结算下单
    checkout(data: CheckoutRequest) {
        return http.post<CheckoutResponse>('/store/checkout', data)
    },

    // 获取我的订单列表
    getMyOrders() {
        return http.get<Order[]>('/store/customers/me/orders')
    },

    // 获取我的预订单列表
    getMyPreorders() {
        return http.get<Order[]>('/store/customers/me/preorders')
    },

    // 凭 receipt_token 查单
    lookupOrder(receiptToken: string) {
        return http.get<Order>(`/store/orders/lookup?receipt_token=${receiptToken}`)
    },

    // 凭取货码查单（门店端核销用）
    lookupOrderByPickupCode(pickupCode: string) {
        return http.get<Order>(`/store/orders/lookup-by-pickup-code?pickup_code=${pickupCode}`)
    },

    // 发起支付
    createPayment(orderId: string, provider?: string) {
        return http.post<{ payment_id: string; jsapi?: any }>('/store/payments/create', { order_id: orderId, provider })
    },

    // 查询支付状态
    queryPayment(paymentId: string) {
        return http.get<{ payment_id: string; status: string }>(`/store/payments/${paymentId}`)
    },

    // 获取订单补差记录（称重商品多退少补）
    getOrderAdjustments(orderId: string) {
        return http.get<OrderAdjustment[]>(`/store/orders/${orderId}/adjustments`)
    },

    // 提交售后申请（支持多图、原因、描述、行项级）
    submitAftersalesClaim(data: AftersalesClaimRequest) {
        return http.post<{ claim_id: string; message: string }>('/store/customers/me/aftersales-claim', data)
    }
}

// ==================== 地址 API ====================

export const addressApi = {
    // 获取我的地址列表
    getAddresses() {
        return http.get<CustomerAddress[]>('/store/customers/me/addresses')
    },

    // 新增地址
    addAddress(data: AddAddressRequest) {
        return http.post<CustomerAddress[]>('/store/customers/me/addresses', data)
    },

    // 修改地址
    updateAddress(addressId: string, data: UpdateAddressRequest) {
        return http.put<CustomerAddress[]>(`/store/customers/me/addresses/${addressId}`, data)
    },

    // 删除地址
    deleteAddress(addressId: string) {
        return http.delete<{ ok: boolean }>(`/store/customers/me/addresses/${addressId}`)
    }
}

// ==================== 会员/积分/储值 API ====================

export const membershipApi = {
    // 获取会员摘要
    getMembershipSummary() {
        return http.get<MembershipSummary>('/store/customers/me/membership')
    },

    // 获取积分明细
    getPoints(limit = 50, offset = 0) {
        return http.get<{ entries: PointsEntry[] }>(`/store/points?limit=${limit}&offset=${offset}`)
    },

    // 获取储值明细
    getStoredValue(limit = 50, offset = 0) {
        return http.get<{ entries: StoredValueEntry[] }>(`/store/stored-value?limit=${limit}&offset=${offset}`)
    }
}

// ==================== 配送方式 API ====================

export const shippingApi = {
    // 获取可用配送方式
    getShippingMethods() {
        return http.get<ShippingMethod[]>('/store/shipping-methods')
    }
}

// ==================== 客服 API ====================

export const customerServiceApi = {
    // 获取客服配置
    getConfig() {
        return http.get<CustomerServiceConfig[]>('/store/customer-service')
    }
}

// ==================== 团购（拼单）API ====================

export const pintuanApi = {
    // 获取团购列表
    getGroups(status?: string, limit = 20, offset = 0) {
        const params = new URLSearchParams()
        if (status) params.set('status', status)
        params.set('limit', String(limit))
        params.set('offset', String(offset))
        return http.get<PintuanGroupListResponse>(`/store/pintuan/groups?${params.toString()}`)
    },

    // 获取团购详情
    getGroup(groupId: string) {
        return http.get<PintuanGroup>(`/store/pintuan/groups/${groupId}`)
    },

    // 发起拼单
    createGroup(data: CreatePintuanGroupRequest) {
        return http.post<PintuanGroup>('/store/pintuan/groups', data)
    },

    // 参团
    joinGroup(groupId: string, data: JoinPintuanGroupRequest) {
        return http.post<PintuanGroup>(`/store/pintuan/groups/${groupId}/join`, data)
    }
}

// ==================== 自提点 API ====================

export const pickupApi = {
    // 获取自提点及可用时段
    getPickupSettings() {
        return http.get<PickupSettings>('/store/pickup/settings')
    },

    // 获取自提点列表
    getPickupPoints() {
        return http.get<PickupPoint[]>('/store/pickup/points')
    },

    // 获取某自提点的可用时段
    getPickupTimeSlots(pointId: string, date?: string) {
        const params = new URLSearchParams()
        if (date) params.set('date', date)
        return http.get<PickupTimeSlot[]>(`/store/pickup/points/${pointId}/slots?${params.toString()}`)
    }
}

// ==================== 配送时段 API ====================

export const deliveryApi = {
    // 获取配送选项（含时段、运费、范围校验）
    getDeliveryOptions(lat?: number, lon?: number, subtotalCents = 0, slotStart?: string) {
        const params = new URLSearchParams()
        if (lat !== undefined) params.set('lat', String(lat))
        if (lon !== undefined) params.set('lon', String(lon))
        if (subtotalCents > 0) params.set('subtotal_cents', String(subtotalCents))
        if (slotStart) params.set('slot_start', slotStart)
        return http.get<DeliveryOptions>(`/store/delivery/options?${params.toString()}`)
    }
}

// ==================== 个性化推荐 API ====================

export const personalizationApi = {
    // 获取上一单（用于再来一单预览）
    getLastOrder() {
        return http.get<{ order: ReorderLastOrder }>('/store/personalization/reorder/last-order')
    },

    // 将上一单加入购物车
    applyReorder(data: ReorderApplyRequest) {
        return http.post<ReorderApplyResponse>('/store/personalization/reorder/apply', data)
    },

    // 获取常买清单
    getFrequent() {
        return http.get<{ items: FrequentItem[] }>('/store/personalization/frequent')
    },

    // 一键将常买商品加入购物车
    addFrequentToCart(variantIds: string[]) {
        return http.post<{ cart_id: string; added: number }>('/store/personalization/frequent/add-to-cart', { variant_ids: variantIds })
    },

    // 获取共购推荐
    getCopurchase(productId: string, limit = 10) {
        return http.get<{ items: CopurchaseItem[] }>(`/store/personalization/copurchase/${productId}?limit=${limit}`)
    },

    // 获取首页个性化推荐
    getHomepage() {
        return http.get<{ items: HomepageRecommendation[] }>('/store/personalization/homepage')
    },

    // 获取菜谱列表
    getRecipes(limit = 20, offset = 0) {
        return http.get<{ items: Recipe[]; total: number }>(`/store/personalization/recipes?limit=${limit}&offset=${offset}`)
    },

    // 获取菜谱详情
    getRecipe(recipeId: string) {
        return http.get<Recipe>(`/store/personalization/recipes/${recipeId}`)
    },

    // 一键将菜谱食材加入购物车
    addRecipeToCart(recipeId: string, servings = 1) {
        return http.post<{ cart_id: string; added: number }>(`/store/personalization/recipes/${recipeId}/add-to-cart`, { servings })
    }
}

// ==================== 轮播图 API ====================

export const bannerApi = {
    // 获取轮播图列表（顾客端）
    getBanners() {
        return http.get<{ items: BannerItem[] }>('/store/banner/list')
    }
}

// ==================== 后台管理 API ====================

export const adminApi = {
    // 获取轮播图配置（后台）
    getBannerSettings() {
        return http.get<{ config: { banners: BannerItem[]; note?: string } }>('/admin/banner/settings')
    },

    // 保存轮播图配置（后台）
    saveBannerSettings(config: { banners: BannerItem[]; note?: string }) {
        return http.put<{ ok: boolean; config: any }>('/admin/banner/settings', config)
    },

    // 新增轮播图
    createBanner(banner: BannerItem) {
        return http.post<{ ok: boolean; banner: BannerItem; config: any }>('/admin/banner/banners', banner)
    },

    // 更新轮播图
    updateBanner(bannerId: string, banner: BannerItem) {
        return http.put<{ ok: boolean; banner: BannerItem; config: any }>(`/admin/banner/banners/${bannerId}`, banner)
    },

    // 删除轮播图
    deleteBanner(bannerId: string) {
        return http.delete<{ ok: boolean; config: any }>(`/admin/banner/banners/${bannerId}`)
    },

    // 批量重排序
    reorderBanners(items: { id: string; sort_order: number }[]) {
        return http.post<{ ok: boolean; config: any }>('/admin/banner/banners/reorder', items)
    },

    // 获取自提点配置（后台）
    getPickupSettings() {
        return http.get<{ config: any }>('/admin/pickup/settings')
    },

    // 保存自提点配置（后台）
    savePickupSettings(config: any) {
        return http.put<{ ok: boolean; config: any }>('/admin/pickup/settings', config)
    },

    // 获取团购列表（后台）
    getPintuanGroups(status?: string, limit = 50, offset = 0) {
        const params = new URLSearchParams()
        if (status) params.set('status', status)
        params.set('limit', String(limit))
        params.set('offset', String(offset))
        return http.get<{ items: any[]; limit: number; offset: number }>(`/admin/pintuan/groups?${params.toString()}`)
    },

    // 获取团购详情（后台）
    getPintuanGroup(groupId: string) {
        return http.get<any>(`/admin/pintuan/groups/${groupId}`)
    },

    // 批量过期团购
    sweepExpiredPintuanGroups() {
        return http.post<{ expired_count: number }>('/admin/pintuan/groups/sweep-expired')
    },

    // 获取拣货单
    getPintuanPickList(groupId: string) {
        return http.get<any>(`/admin/pintuan/groups/${groupId}/pick-list`)
    },

    // 自提核销（门店端）
    verifyPickup(orderId: string) {
        return http.post<{ ok: boolean }>(`/admin/store-ops/orders/${orderId}/verify-pickup`)
    }
}
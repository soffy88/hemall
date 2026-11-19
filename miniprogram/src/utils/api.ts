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
    ProductVariant
} from '../types'

// ==================== 认证 API ====================

export const authApi = {
    // 微信登录
    wechatLogin(code: string) {
        return http.post<WeChatLoginResponse>('/store/customers/wechat-login', { code })
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
        return http.get<Order>('/store/orders/lookup', { receipt_token: receiptToken })
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
<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { cartApi, orderApi, addressApi, shippingApi, pickupApi, deliveryApi, membershipApi } from '@/utils/api'
import { formatPriceWithSymbol, formatTimeRemaining } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { Cart, CustomerAddress, ShippingMethod, CheckoutRequest, CheckoutResponse, CartAddress, PickupPoint, PickupTimeSlot, DeliveryOptions, DeliverySlot, MembershipSummary, CartDiscount } from '@/types'

// Helper functions
function formatPrice(cents: number): string {
    return (cents / 100).toFixed(2)
}

function getProductImage(item: any): string {
    // 简化：使用占位图，实际应从商品详情获取
    return '/static/images/placeholder.png'
}

function formatOptionText(optionValues: Record<string, any>): string {
    if (!optionValues) return ''
    return Object.values(optionValues).filter(v => v !== null && v !== undefined && v !== '').map(v => String(v)).join(' / ')
}

const cart = ref<Cart | null>(null)
const addresses = ref<CustomerAddress[]>([])
const shippingMethods = ref<ShippingMethod[]>([])
const loading = ref(false)
const submitting = ref(false)
const selectedAddress = ref<CustomerAddress | null>(null)
const selectedShippingMethod = ref<ShippingMethod | null>(null)
const remark = ref('')
const checkoutResult = ref<CheckoutResponse | null>(null)
const showPaymentModal = ref(false)
const paymentProvider = ref('wechat')

// 自提相关
const pickupPoints = ref<PickupPoint[]>([])
const selectedPickupPoint = ref<PickupPoint | null>(null)
const pickupTimeSlots = ref<PickupTimeSlot[]>([])
const selectedPickupSlot = ref<PickupTimeSlot | null>(null)
const pickupDate = ref<string>('') // YYYY-MM-DD
const showPickupSelector = ref(false)

// 配送时段相关
const deliveryOptions = ref<DeliveryOptions | null>(null)
const selectedDeliverySlot = ref<DeliverySlot | null>(null)
const showDeliverySlotSelector = ref(false)

// 优惠/会员相关
const membership = ref<MembershipSummary | null>(null)
const availableCoupons = ref<CartDiscount[]>([])
const showCouponSelector = ref(false)
const couponCode = ref('')
const showCouponInput = ref(false)

async function fetchData() {
    loading.value = true
    try {
        const cartId = uni.getStorageSync('cart_id') as string | null
        if (!cartId) {
            uni.showToast({ title: '购物车为空', icon: 'none' })
            setTimeout(() => uni.switchTab({ url: '/pages/cart/cart' }), 1500)
            return
        }

        const [cartRes, addrRes, shipRes, membershipRes] = await Promise.all([
            cartApi.getCart(cartId),
            addressApi.getAddresses(),
            shippingApi.getShippingMethods().catch(() => []),
            membershipApi.getMembershipSummary().catch(() => null)
        ])

        cart.value = cartRes
        addresses.value = addrRes
        shippingMethods.value = shipRes
        membership.value = membershipRes

        // 获取购物车中已应用的优惠券
        availableCoupons.value = cartRes.discounts || []

        // 默认选择默认地址
        const defaultAddr = addrRes.find(a => a.is_default)
        if (defaultAddr) {
            selectedAddress.value = defaultAddr
        } else if (addrRes.length > 0) {
            selectedAddress.value = addrRes[0]
        }

        // 默认选择配送方式（优先自提）
        const pickupMethod = shipRes.find(m => m.type === 'pickup' && m.is_active)
        const deliveryMethod = shipRes.find(m => m.type === 'delivery' && m.is_active)
        selectedShippingMethod.value = pickupMethod || deliveryMethod || shipRes[0] || null

        // 如果购物车已有地址，同步
        if (cartRes.shipping_address) {
            selectedAddress.value = cartRes.shipping_address as unknown as CustomerAddress
        }
        if (cartRes.billing_address) {
            // 账单地址通常同收货地址
        }

        // 如果默认是自提，加载自提点
        if (selectedShippingMethod.value?.type === 'pickup') {
            await loadPickupPoints()
        }
    } catch (e) {
        console.error('获取结算数据失败:', e)
        uni.showToast({ title: '加载失败', icon: 'none' })
    } finally {
        loading.value = false
    }
}

async function loadPickupPoints() {
    try {
        const res = await pickupApi.getPickupPoints()
        pickupPoints.value = res.points || []
        // 默认选择第一个自提点
        if (pickupPoints.value.length > 0 && !selectedPickupPoint.value) {
            selectedPickupPoint.value = pickupPoints.value[0]
            // 设置默认日期为今天
            const today = new Date()
            pickupDate.value = today.toISOString().split('T')[0]
            await loadPickupTimeSlots()
        }
    } catch (e) {
        console.error('加载自提点失败:', e)
    }
}

async function loadPickupTimeSlots() {
    if (!selectedPickupPoint.value || !pickupDate.value) return
    try {
        const res = await pickupApi.getPickupTimeSlots(selectedPickupPoint.value.id, pickupDate.value)
        pickupTimeSlots.value = res.slots || []
        // 默认选择第一个可用时段
        const available = pickupTimeSlots.value.find(s => s.is_available)
        selectedPickupSlot.value = available || null
    } catch (e) {
        console.error('加载自提时段失败:', e)
    }
}

async function onPickupPointChange(point: PickupPoint) {
    selectedPickupPoint.value = point
    selectedPickupSlot.value = null
    await loadPickupTimeSlots()
}

async function onPickupDateChange() {
    if (!pickupDate.value) return
    await loadPickupTimeSlots()
}

async function onPickupSlotChange(slot: PickupTimeSlot) {
    selectedPickupSlot.value = slot
}

// 配送时段相关
async function loadDeliveryOptions() {
    if (!selectedAddress.value) return
    try {
        const lat = 0 // TODO: 从地址获取经纬度
        const lon = 0
        const subtotal = cart.value?.subtotal_cents || 0
        const res = await deliveryApi.getDeliveryOptions(lat, lon, subtotal)
        deliveryOptions.value = res
        // 默认选择第一个可用时段
        if (res.slots && res.slots.length > 0) {
            const available = res.slots.find(s => s.remaining > 0)
            selectedDeliverySlot.value = available || null
        }
    } catch (e) {
        console.error('加载配送选项失败:', e)
    }
}

async function onDeliverySlotChange(slot: DeliverySlot) {
    selectedDeliverySlot.value = slot
}

// 优惠券相关
async function applyCouponCode() {
    if (!couponCode.value.trim()) return
    try {
        await cartApi.applyDiscount({ cart_id: cart.value!.id, code: couponCode.value.trim() })
        uni.showToast({ title: '优惠码已应用', icon: 'success' })
        couponCode.value = ''
        showCouponInput.value = false
        // 刷新购物车
        const cartRes = await cartApi.getCart(cart.value!.id)
        cart.value = cartRes
        availableCoupons.value = cartRes.discounts || []
    } catch (e: any) {
        uni.showToast({ title: e.message || '优惠码无效', icon: 'none' })
    }
}

async function removeCoupon(discountId: string) {
    try {
        await cartApi.removeDiscount(cart.value!.id, discountId)
        uni.showToast({ title: '已移除优惠', icon: 'success' })
        // 刷新购物车
        const cartRes = await cartApi.getCart(cart.value!.id)
        cart.value = cartRes
        availableCoupons.value = cartRes.discounts || []
    } catch (e: any) {
        uni.showToast({ title: e.message || '移除失败', icon: 'none' })
    }
}

function getMemberDiscount(): number {
    // 计算会员折扣（如果有会员价）
    // 这里简化：通过会员等级给出折扣比例
    if (!membership.value) return 0
    const tier = membership.value.tier
    // 假设：silver 5%, gold 8%, platinum 10%, diamond 12%
    const tierDiscounts: Record<string, number> = {
        silver: 0.05,
        gold: 0.08,
        platinum: 0.10,
        diamond: 0.12
    }
    const rate = tierDiscounts[tier] || 0
    if (rate === 0) return 0
    // 会员折扣基于商品小计计算，但不超过总金额
    return Math.floor(subtotal.value * rate)
}

function getTotalDiscount(): number {
    const couponDiscount = discount.value
    const memberDiscount = getMemberDiscount()
    return couponDiscount + memberDiscount
}

async function chooseAddress() {
    uni.navigateTo({ url: '/pages/address/list?select=true' })
}

async function addNewAddress() {
    uni.navigateTo({ url: '/pages/address/edit' })
}

function formatAddress(addr: CustomerAddress): string {
    return `${addr.recipient_name} ${addr.phone} ${addr.city}${addr.address_line1}${addr.address_line2}`
}

async function submitCheckout() {
    if (!cart.value || cart.value.line_items.length === 0) {
        uni.showToast({ title: '购物车为空', icon: 'none' })
        return
    }

    if (!selectedAddress.value) {
        uni.showToast({ title: '请选择收货地址', icon: 'none' })
        return
    }

    if (!selectedShippingMethod.value) {
        uni.showToast({ title: '请选择配送方式', icon: 'none' })
        return
    }

    // 自提方式需要选择自提点和时段
    if (selectedShippingMethod.value.type === 'pickup') {
        if (!selectedPickupPoint.value) {
            uni.showToast({ title: '请选择自提点', icon: 'none' })
            return
        }
        if (!selectedPickupSlot.value) {
            uni.showToast({ title: '请选择自提时段', icon: 'none' })
            return
        }
        if (!pickupDate.value) {
            uni.showToast({ title: '请选择自提日期', icon: 'none' })
            return
        }
    }

    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login?redirect=checkout' })
        return
    }

    submitting.value = true
    try {
        const request: CheckoutRequest = {
            cart_id: cart.value.id,
            shipping_address: {
                recipient_name: selectedAddress.value.recipient_name,
                phone: selectedAddress.value.phone,
                address_line1: selectedAddress.value.address_line1,
                address_line2: selectedAddress.value.address_line2,
                city: selectedAddress.value.city,
                region_code: selectedAddress.value.region_code,
                postal_code: selectedAddress.value.postal_code
            },
            billing_address: {
                recipient_name: selectedAddress.value.recipient_name,
                phone: selectedAddress.value.phone,
                address_line1: selectedAddress.value.address_line1,
                address_line2: selectedAddress.value.address_line2,
                city: selectedAddress.value.city,
                region_code: selectedAddress.value.region_code,
                postal_code: selectedAddress.value.postal_code
            },
            provider: paymentProvider.value,
            // 自提相关字段
            pickup_point_id: selectedShippingMethod.value.type === 'pickup' ? selectedPickupPoint.value?.id : undefined,
            pickup_time_slot_id: selectedShippingMethod.value.type === 'pickup' ? selectedPickupSlot.value?.id : undefined,
            // 配送时段
            delivery_slot_id: selectedShippingMethod.value.type === 'delivery' ? selectedDeliverySlot.value?.id : undefined,
        }

        const res = await orderApi.checkout(request)
        checkoutResult.value = res

        // 如果有 JSAPI 参数，调起微信支付
        if (res.payment.jsapi) {
            await callWeChatPay(res.payment.jsapi)
        } else if (res.payment.code_url) {
            // 显示二维码支付（Native）
            showPaymentModal.value = true
        } else {
            // 无在线支付配置，提示到店付款
            uni.showModal({
                title: '下单成功',
                content: '暂未开通在线支付，请到店付款。订单号：' + res.order_id.slice(0, 8),
                showCancel: false,
                success: () => {
                    uni.redirectTo({ url: `/pages/order/detail?id=${res.order_id}` })
                }
            })
        }
    } catch (e: any) {
        uni.showToast({ title: e.message || '下单失败', icon: 'none' })
    } finally {
        submitting.value = false
    }
}

async function callWeChatPay(jsapiParams: any) {
    return new Promise<void>((resolve, reject) => {
        uni.requestPayment({
            provider: 'wxpay',
            timeStamp: jsapiParams.timeStamp,
            nonceStr: jsapiParams.nonceStr,
            package: jsapiParams.package,
            signType: jsapiParams.signType,
            paySign: jsapiParams.paySign,
            success: () => {
                uni.showToast({ title: '支付成功', icon: 'success' })
                if (checkoutResult.value) {
                    uni.redirectTo({ url: `/pages/order/detail?id=${checkoutResult.value.order_id}` })
                }
                resolve()
            },
            fail: (err) => {
                if (err.errMsg?.includes('cancel')) {
                    uni.showToast({ title: '已取消支付', icon: 'none' })
                } else {
                    uni.showToast({ title: '支付失败：' + err.errMsg, icon: 'none' })
                }
                reject(err)
            }
        })
    })
}

function goOrders() {
    uni.switchTab({ url: '/pages/order/list' })
}

const subtotal = computed(() => cart.value?.subtotal_cents || 0)
const discount = computed(() => cart.value?.discount_cents || 0)
const memberDiscount = computed(() => getMemberDiscount())
const totalDiscount = computed(() => discount.value + memberDiscount.value)
const shipping = computed(() => {
    if (!selectedShippingMethod.value) return 0
    if (selectedShippingMethod.value.type === 'pickup') return 0
    if (subtotal.value >= selectedShippingMethod.value.min_order_cents) return 0
    return selectedShippingMethod.value.fee_cents
})
const grandTotal = computed(() => subtotal.value - totalDiscount.value + shipping.value)

// 监听配送方式变化，切换到自提时加载自提点
watch(selectedShippingMethod, async (newVal, oldVal) => {
    if (newVal?.type === 'pickup' && oldVal?.type !== 'pickup') {
        selectedPickupPoint.value = null
        selectedPickupSlot.value = null
        pickupTimeSlots.value = []
        pickupDate.value = ''
        await loadPickupPoints()
    } else if (newVal?.type === 'delivery' && oldVal?.type !== 'delivery') {
        // 切换到配送时加载配送选项
        selectedDeliverySlot.value = null
        deliveryOptions.value = null
        await loadDeliveryOptions()
    } else if (newVal?.type !== 'pickup' && newVal?.type !== 'delivery') {
        // 切换到其他方式时清空相关选择
        selectedPickupPoint.value = null
        selectedPickupSlot.value = null
        pickupTimeSlots.value = []
        pickupDate.value = ''
        selectedDeliverySlot.value = null
        deliveryOptions.value = null
    }
})

onMounted(() => {
    fetchData()
})

// 监听从地址列表返回的选择
onShow(() => {
    const selectedAddr = uni.getStorageSync('selected_checkout_address') as CustomerAddress | null
    if (selectedAddr) {
        selectedAddress.value = selectedAddr
        uni.removeStorageSync('selected_checkout_address')
    }
})
</script>

<template>
    <view class="page">
        <scroll-view class="checkout-scroll" scroll-y :style="{ height: 'calc(100vh - 98px)' }" v-if="cart && cart.line_items.length > 0">
            <!-- 收货地址 -->
            <view class="section">
                <view class="section-title">收货地址</view>
                <view class="address-card" v-if="selectedAddress" @tap="chooseAddress">
                    <view class="address-info">
                        <view class="address-name-phone">
                            <text class="address-name">{{ selectedAddress.recipient_name }}</text>
                            <text class="address-phone">{{ selectedAddress.phone }}</text>
                            <view class="address-tag" v-if="selectedShippingMethod?.type === 'pickup'">自提</view>
                            <view class="address-tag delivery" v-else>配送</view>
                        </view>
                        <text class="address-detail">{{ formatAddress(selectedAddress) }}</text>
                    </view>
                    <text class="arrow">></text>
                </view>
                <view class="address-empty" v-else @tap="addNewAddress">
                    <text class="empty-text">暂无收货地址，去添加</text>
                    <text class="arrow">></text>
                </view>
            </view>

            <!-- 配送方式 -->
            <view class="section" v-if="shippingMethods.length > 0">
                <view class="section-title">配送方式</view>
                <view class="shipping-options">
                    <view
                        class="shipping-option"
                        :class="{ selected: selectedShippingMethod?.id === method.id }"
                        v-for="method in shippingMethods"
                        :key="method.id"
                        @tap="selectedShippingMethod = method"
                    >
                        <view class="option-main">
                            <view class="option-icon" :class="{ pickup: method.type === 'pickup', delivery: method.type === 'delivery' }">
                                <text v-if="method.type === 'pickup'">🏪</text>
                                <text v-else>🚚</text>
                            </view>
                            <view class="option-info">
                                <text class="option-name">{{ method.name }}</text>
                                <text class="option-desc" v-if="method.type === 'delivery'">
                                    起送 ¥{{ formatPrice(method.min_order_cents) }}，配送费 ¥{{ formatPrice(method.fee_cents) }}
                                </text>
                                <text class="option-desc" v-else>到店自提，无配送费</text>
                            </view>
                        </view>
                        <view class="option-price" v-if="method.type === 'delivery' && subtotal < method.min_order_cents">
                            +¥{{ formatPrice(method.fee_cents) }}
                        </view>
                        <view class="option-price free" v-if="method.type === 'delivery' && subtotal >= method.min_order_cents">
                            免配送费
                        </view>
                        <view class="option-price free" v-if="method.type === 'pickup'">
                            免费
                        </view>
                        <radio class="option-radio" :checked="selectedShippingMethod?.id === method.id" />
                    </view>
                </view>
                <view class="shipping-hint" v-if="selectedShippingMethod?.type === 'delivery' && subtotal < selectedShippingMethod.min_order_cents">
                    再买 ¥{{ formatPrice(selectedShippingMethod.min_order_cents - subtotal) }} 免配送费
                </view>
            </view>

            <!-- 自提点与时段选择（仅自提方式显示） -->
            <view class="section" v-if="selectedShippingMethod?.type === 'pickup'">
                <view class="section-title">自提信息</view>

                <!-- 自提点选择 -->
                <view class="pickup-field" @tap="showPickupSelector = true">
                    <view class="field-label">自提门店</view>
                    <view class="field-value">
                        <text v-if="selectedPickupPoint">{{ selectedPickupPoint.name }}</text>
                        <text v-else class="placeholder">请选择自提门店</text>
                        <text class="arrow">></text>
                    </view>
                </view>
                <view class="pickup-point-detail" v-if="selectedPickupPoint">
                    <text class="detail-text">{{ selectedPickupPoint.address }}</text>
                    <text class="detail-text">{{ selectedPickupPoint.phone }}</text>
                    <text class="detail-text">营业时间：{{ selectedPickupPoint.business_hours?.start }} - {{ selectedPickupPoint.business_hours?.end }}</text>
                </view>

                <!-- 自提日期选择 -->
                <view class="pickup-field">
                    <view class="field-label">自提日期</view>
                    <view class="field-value">
                        <picker mode="date" :value="pickupDate" @change="onPickupDateChange" v-if="pickupPoints.length > 0">
                            <view class="picker-display">
                                <text v-if="pickupDate">{{ pickupDate }}</text>
                                <text v-else class="placeholder">请选择日期</text>
                                <text class="arrow">></text>
                            </view>
                        </picker>
                    </view>
                </view>

                <!-- 自提时段选择 -->
                <view class="pickup-field">
                    <view class="field-label">自提时段</view>
                    <view class="field-value">
                        <text v-if="selectedPickupSlot">{{ selectedPickupSlot.start_time }} - {{ selectedPickupSlot.end_time }} (剩余 {{ selectedPickupSlot.remaining }} 份)</text>
                        <text v-else class="placeholder">请选择时段</text>
                    </view>
                </view>
                <view class="time-slot-list" v-if="pickupTimeSlots.length > 0">
                    <view
                        class="time-slot-item"
                        :class="{ selected: selectedPickupSlot?.id === slot.id, unavailable: !slot.is_available }"
                        v-for="slot in pickupTimeSlots"
                        :key="slot.id"
                        @tap="slot.is_available && (selectedPickupSlot = slot)"
                    >
                        <text class="slot-time">{{ slot.start_time }} - {{ slot.end_time }}</text>
                        <text class="slot-capacity" :class="{ full: !slot.is_available }">
                            {{ slot.is_available ? ('剩余 ' + slot.remaining + ' 份') : '已约满' }}
                        </text>
                        <radio class="slot-radio" :checked="selectedPickupSlot?.id === slot.id" />
                    </view>
                </view>
                <view class="no-slots" v-if="pickupTimeSlots.length === 0 && pickupDate">
                    该日期暂无可用时段，请选择其他日期
                </view>
            </view>

            <!-- 配送时段选择（仅配送方式显示） -->
            <view class="section" v-if="selectedShippingMethod?.type === 'delivery'">
                <view class="section-title">配送时段</view>

                <!-- 配送状态提示 -->
                <view class="delivery-status" v-if="deliveryOptions">
                    <view class="status-item" :class="{ error: !deliveryOptions.ok }">
                        <text class="status-icon" v-if="deliveryOptions.ok">✓</text>
                        <text class="status-icon" v-else>✗</text>
                        <text class="status-text">{{ deliveryOptions.reason || '配送可用' }}</text>
                    </view>
                    <view class="status-item" v-if="deliveryOptions.distance_km !== null">
                        <text class="status-icon">📍</text>
                        <text class="status-text">距离门店 {{ deliveryOptions.distance_km }} 公里</text>
                    </view>
                    <view class="status-item" v-if="!deliveryOptions.ok && deliveryOptions.reason">
                        <text class="status-icon">⚠</text>
                        <text class="status-text">{{ deliveryOptions.reason }}</text>
                    </view>
                </view>

                <!-- 配送时段选择 -->
                <view class="delivery-field" v-if="deliveryOptions?.slots && deliveryOptions.slots.length > 0">
                    <view class="field-label">选择时段</view>
                    <view class="field-value">
                        <text v-if="selectedDeliverySlot">{{ selectedDeliverySlot.start }} - {{ selectedDeliverySlot.end }} (剩余 {{ selectedDeliverySlot.remaining }} 份)</text>
                        <text v-else class="placeholder">请选择配送时段</text>
                    </view>
                </view>
                <view class="time-slot-list" v-if="deliveryOptions?.slots && deliveryOptions.slots.length > 0">
                    <view
                        class="time-slot-item"
                        :class="{ selected: selectedDeliverySlot?.id === slot.id, unavailable: slot.remaining <= 0 }"
                        v-for="slot in deliveryOptions.slots"
                        :key="slot.id"
                        @tap="slot.remaining > 0 && (selectedDeliverySlot = slot)"
                    >
                        <text class="slot-time">{{ slot.start }} - {{ slot.end }}</text>
                        <text class="slot-capacity" :class="{ full: slot.remaining <= 0 }">
                            {{ slot.remaining > 0 ? ('剩余 ' + slot.remaining + ' 份') : '已约满' }}
                        </text>
                        <text class="slot-eta">{{ slot.eta_text }}</text>
                        <radio class="slot-radio" :checked="selectedDeliverySlot?.id === slot.id" />
                    </view>
                </view>
                <view class="no-slots" v-if="deliveryOptions?.slots && deliveryOptions.slots.length === 0">
                    今日暂无可用配送时段
                </view>
                <view class="delivery-disabled" v-if="deliveryOptions && !deliveryOptions.enabled">
                    <text class="disabled-text">自营配送暂未开启，仅支持到店自提</text>
                </view>
            </view>

            <!-- 商品清单 -->
            <view class="section">
                <view class="section-title">商品清单 ({{ cart?.line_items.length || 0 }})</view>
                <view class="product-list">
                    <view class="product-item" v-for="item in cart?.line_items" :key="item.id">
                        <image class="product-image" :src="getProductImage(item)" mode="aspectFill" />
                        <view class="product-info">
                            <text class="product-title" style="-webkit-line-clamp: 1; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ item.product_title }}</text>
                            <text class="product-spec" v-if="item.option_values && Object.keys(item.option_values).length > 0">{{ formatOptionText(item.option_values) }}</text>
                            <view class="product-price-qty">
                                <text class="product-price">{{ formatPriceWithSymbol(item.unit_price_cents) }}</text>
                                <text class="product-qty">x {{ item.quantity }}</text>
                            </view>
                        </view>
                    </view>
                </view>
            </view>

            <!-- 价格明细 -->
            <view class="section price-detail">
                <view class="price-row">
                    <text>商品小计</text>
                    <text>{{ formatPriceWithSymbol(subtotal) }}</text>
                </view>

                <!-- 优惠券 -->
                <view class="price-row" v-if="availableCoupons.length > 0">
                    <view class="coupon-row">
                        <text>优惠券 ({{ availableCoupons.length }})</text>
                        <text class="discount">-{{ formatPriceWithSymbol(discount) }}</text>
                    </view>
                    <view class="coupon-detail" v-for="coupon in availableCoupons" :key="coupon.id">
                        <view class="coupon-tag">{{ coupon.code }}</view>
                        <text class="coupon-amount">-{{ formatPriceWithSymbol(coupon.applied_amount_cents) }}</text>
                        <button class="btn-remove-coupon" @tap.stop="removeCoupon(coupon.discount_id)" size="mini">移除</button>
                    </view>
                </view>

                <!-- 会员折扣 -->
                <view class="price-row member-discount" v-if="memberDiscount > 0">
                    <text>会员折扣 ({{ membership?.tier || '会员' }})</text>
                    <text class="discount">-{{ formatPriceWithSymbol(memberDiscount) }}</text>
                </view>

                <!-- 输入优惠码 -->
                <view class="coupon-input-row" v-if="showCouponInput">
                    <input class="coupon-input" v-model="couponCode" placeholder="请输入优惠码" />
                    <button class="btn-apply-coupon" @tap="applyCouponCode">使用</button>
                </view>
                <view class="coupon-input-trigger" v-if="!showCouponInput && !availableCoupons.length" @tap="showCouponInput = true">
                    <text class="coupon-input-text">+ 使用优惠码</text>
                </view>

                <view class="price-row" v-if="shipping > 0">
                    <text>配送费</text>
                    <text>{{ formatPriceWithSymbol(shipping) }}</text>
                </view>
                <view class="price-row" v-if="selectedShippingMethod?.type === 'delivery' && subtotal >= selectedShippingMethod.min_order_cents">
                    <text>配送费</text>
                    <text class="free">免费</text>
                </view>
                <view class="price-row total">
                    <text>实付款</text>
                    <text>{{ formatPriceWithSymbol(grandTotal) }}</text>
                </view>
            </view>

            <!-- 备注 -->
            <view class="section">
                <view class="section-title">备注 (选填)</view>
                <input class="remark-input" v-model="remark" placeholder="给商家留言，如：放门口/电话联系..." maxlength="100" />
            </view>

            <view class="bottom-placeholder" />
        </scroll-view>

        <!-- 底部提交栏 -->
        <view class="submit-bar" v-if="cart && cart.line_items.length > 0">
            <button class="btn-submit" @tap="submitCheckout" :disabled="submitting || !selectedAddress || !selectedShippingMethod">
                <text v-if="!submitting">提交订单 ({{ formatPriceWithSymbol(grandTotal) }})</text>
                <text v-else>提交中...</text>
            </button>
        </view>

        <!-- 支付二维码弹窗 -->
        <view class="modal-mask" v-if="showPaymentModal" @tap="showPaymentModal = false">
            <view class="modal payment-modal" @tap.stop>
                <text class="modal-title">扫码支付</text>
                <text class="modal-desc">使用微信扫一扫完成支付</text>
                <image class="qrcode" :src="checkoutResult?.payment?.code_url" mode="aspectFit" v-if="checkoutResult?.payment?.code_url" />
                <text class="modal-desc" v-else>生成二维码中...</text>
                <button class="modal-btn" @tap="showPaymentModal = false">已完成支付</button>
                <button class="modal-btn secondary" @tap="showPaymentModal = false">取消</button>
            </view>
        </view>

        <!-- 自提点选择弹窗 -->
        <view class="modal-mask" v-if="showPickupSelector" @tap="showPickupSelector = false">
            <view class="modal pickup-modal" @tap.stop>
                <view class="modal-header">
                    <text class="modal-title">选择自提门店</text>
                    <text class="modal-close" @tap="showPickupSelector = false">×</text>
                </view>
                <view class="pickup-list">
                    <view
                        class="pickup-item"
                        :class="{ selected: selectedPickupPoint?.id === point.id }"
                        v-for="point in pickupPoints"
                        :key="point.id"
                        @tap="onPickupPointChange(point); showPickupSelector = false"
                    >
                        <view class="pickup-info">
                            <text class="pickup-name">{{ point.name }}</text>
                            <text class="pickup-address">{{ point.address }}</text>
                        </view>
                        <radio class="pickup-radio" :checked="selectedPickupPoint?.id === point.id" />
                    </view>
                </view>
                <view class="pickup-empty" v-if="pickupPoints.length === 0">
                    暂无可用自提点
                </view>
            </view>
        </view>
    </view>
</template>

<style scoped>
.page {
    background: #F5F5F5;
    height: 100vh;
    display: flex;
    flex-direction: column;
}

.checkout-scroll {
    flex: 1;
}

.section {
    background: #FFFFFF;
    margin-bottom: 8px;
    padding: 16px;
}

.section-title {
    font-size: 15px;
    font-weight: 600;
    color: #1A1A1A;
    margin-bottom: 12px;
}

.address-card {
    display: flex;
    align-items: flex-start;
    justify-content: space-between;
    padding: 12px;
    border: 1px solid #EEEEEE;
    border-radius: 8px;
}

.address-info {
    flex: 1;
}

.address-name-phone {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
}

.address-name {
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
}

.address-phone {
    font-size: 15px;
    color: #616161;
}

.address-tag {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
}

.address-tag:not(.delivery) {
    background: #E8F5E9;
    color: #2E7D32;
}

.address-tag.delivery {
    background: #E3F2FD;
    color: #1565C0;
}

.address-detail {
    font-size: 13px;
    color: #616161;
    line-height: 1.5;
}

.address-empty {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px 12px;
    border: 1px dashed #E0E0E0;
    border-radius: 8px;
    color: #9E9E9E;
}

.arrow {
    font-size: 16px;
    color: #9E9E9E;
}

.shipping-options {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.shipping-option {
    display: flex;
    align-items: center;
    padding: 12px;
    border: 1px solid #EEEEEE;
    border-radius: 8px;
    background: #FAFAFA;
}

.shipping-option.selected {
    border-color: #2E7D32;
    background: #E8F5E9;
}

.option-main {
    display: flex;
    align-items: center;
    gap: 12px;
    flex: 1;
}

.option-icon {
    width: 36px;
    height: 36px;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 16px;
}

.option-icon.pickup {
    background: #E8F5E9;
}

.option-icon.delivery {
    background: #E3F2FD;
}

.option-info {
    flex: 1;
    min-width: 0;
}

.option-name {
    display: block;
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 2px;
}

.option-desc {
    font-size: 12px;
    color: #9E9E9E;
}

.option-price {
    font-size: 14px;
    font-weight: 500;
    color: #F44336;
    margin-right: 12px;
}

.option-price.free {
    color: #4CAF50;
}

.option-radio {
    width: 20px;
    height: 20px;
}

.shipping-hint {
    margin-top: 12px;
    padding: 8px 12px;
    background: #FFF3E0;
    border-radius: 8px;
    font-size: 13px;
    color: #E65100;
}

.product-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.product-item {
    display: flex;
    gap: 12px;
    padding: 8px 0;
}

.product-image {
    width: 60px;
    height: 60px;
    border-radius: 8px;
    object-fit: cover;
}

.product-info {
    flex: 1;
    min-width: 0;
}

.product-title {
    font-size: 14px;
    color: #1A1A1A;
    margin-bottom: 4px;
}

.product-spec {
    font-size: 12px;
    color: #9E9E9E;
    margin-bottom: 4px;
}

.product-price-qty {
    display: flex;
    align-items: center;
    gap: 12px;
}

.product-price {
    font-size: 14px;
    font-weight: 500;
    color: #2E7D32;
}

.product-qty {
    font-size: 13px;
    color: #9E9E9E;
}

.price-detail {
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
}

.price-row {
    display: flex;
    justify-content: space-between;
    font-size: 14px;
    color: #616161;
    margin-bottom: 8px;
}

.price-row.total {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
}

.price-row .discount {
    color: #F44336;
}

.price-row .free {
    color: #4CAF50;
}

.coupon-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.coupon-detail {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-top: 8px;
    padding-left: 8px;
}

.coupon-tag {
    padding: 2px 8px;
    background: #E3F2FD;
    color: #1565C0;
    border-radius: 4px;
    font-size: 12px;
    font-weight: 500;
}

.coupon-amount {
    font-size: 13px;
    color: #F44336;
    font-weight: 500;
}

.btn-remove-coupon {
    margin-left: auto;
    font-size: 11px;
    color: #9E9E9E;
    background: transparent;
    border: none;
}

.member-discount {
    background: linear-gradient(90deg, #FFF3E0, #FFF8E1);
    border-radius: 8px;
    padding: 8px 12px;
    margin: 8px -12px;
}

.coupon-input-row {
    display: flex;
    gap: 8px;
    margin-top: 8px;
}

.coupon-input {
    flex: 1;
    padding: 10px 12px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 14px;
}

.btn-apply-coupon {
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 10px 16px;
    font-size: 14px;
    font-weight: 500;
}

.coupon-input-trigger {
    margin-top: 8px;
    padding: 8px;
    text-align: center;
}

.coupon-input-text {
    font-size: 13px;
    color: #2E7D32;
    text-decoration: underline;
}

.remark-input {
    width: 100%;
    padding: 12px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 14px;
    color: #1A1A1A;
    background: #FAFAFA;
    box-sizing: border-box;
}

.bottom-placeholder {
    height: 98px;
}

.submit-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    padding: 12px 16px;
    background: #FFFFFF;
    border-top: 1px solid #EEEEEE;
    z-index: 50;
    padding-bottom: calc(12px + env(safe-area-inset-bottom, 34px));
}

.btn-submit {
    width: 100%;
    height: 52px;
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 12px;
    font-size: 17px;
    font-weight: 600;
}

.btn-submit:disabled {
    background: #A5D6A7;
}

/* 支付弹窗 */
.modal-mask {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
    padding: 24px;
}

.payment-modal {
    background: #FFFFFF;
    border-radius: 16px;
    padding: 24px;
    width: 100%;
    max-width: 320px;
    text-align: center;
}

.modal-title {
    display: block;
    font-size: 18px;
    font-weight: 600;
    color: #1A1A1A;
    margin-bottom: 8px;
}

.modal-desc {
    display: block;
    font-size: 14px;
    color: #9E9E9E;
    margin-bottom: 16px;
}

.qrcode {
    width: 200px;
    height: 200px;
    margin: 16px auto;
}

.modal-btn {
    display: block;
    width: 100%;
    padding: 14px;
    border-radius: 12px;
    font-size: 16px;
    font-weight: 500;
    margin-bottom: 12px;
    border: none;
}

.modal-btn:not(.secondary) {
    background: #2E7D32;
    color: #FFFFFF;
}

.modal-btn.secondary {
    background: #F5F5F5;
    color: #424242;
}

/* 自提相关样式 */
.pickup-field {
    display: flex;
    align-items: center;
    padding: 12px 0;
    border-bottom: 1px solid #F5F5F5;
}

.pickup-field:last-child {
    border-bottom: none;
}

.field-label {
    width: 80px;
    font-size: 14px;
    color: #616161;
    flex-shrink: 0;
}

.field-value {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: space-between;
    font-size: 14px;
    color: #1A1A1A;
}

.pickup-point-detail {
    padding: 8px 0 0 80px;
}

.detail-text {
    display: block;
    font-size: 12px;
    color: #9E9E9E;
    margin-bottom: 2px;
}

.time-slot-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-top: 8px;
    padding-left: 80px;
}

.time-slot-item {
    display: flex;
    align-items: center;
    padding: 12px;
    border: 1px solid #EEEEEE;
    border-radius: 8px;
    background: #FAFAFA;
}

.time-slot-item.selected {
    border-color: #2E7D32;
    background: #E8F5E9;
}

.time-slot-item.unavailable {
    opacity: 0.5;
}

.slot-time {
    flex: 1;
    font-size: 14px;
    font-weight: 500;
    color: #1A1A1A;
}

.slot-capacity {
    font-size: 12px;
    color: #2E7D32;
    margin-right: 12px;
}

.slot-capacity.full {
    color: #9E9E9E;
}

.slot-radio {
    width: 20px;
    height: 20px;
}

.no-slots {
    text-align: center;
    padding: 16px;
    color: #9E9E9E;
    font-size: 13px;
}

/* 自提点选择弹窗 */
.pickup-modal {
    background: #FFFFFF;
    border-radius: 16px 16px 0 0;
    padding: 0;
    width: 100%;
    max-width: 100%;
    max-height: 70vh;
    display: flex;
    flex-direction: column;
    margin: auto 0 0 0;
}

.modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px;
    border-bottom: 1px solid #EEEEEE;
}

.modal-close {
    font-size: 24px;
    color: #9E9E9E;
}

.pickup-list {
    flex: 1;
    overflow-y: auto;
    padding: 8px 16px;
}

.pickup-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 14px 12px;
    border-radius: 8px;
}

.pickup-item.selected {
    background: #E8F5E9;
}

.pickup-info {
    flex: 1;
    min-width: 0;
}

.pickup-name {
    display: block;
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 4px;
}

.pickup-address {
    font-size: 12px;
    color: #9E9E9E;
}

.pickup-radio {
    width: 20px;
    height: 20px;
}

.pickup-empty {
    text-align: center;
    padding: 32px;
    color: #9E9E9E;
}

/* 配送时段样式 */
.delivery-status {
    display: flex;
    flex-direction: column;
    gap: 8px;
    margin-bottom: 12px;
    padding: 12px;
    background: #FAFAFA;
    border-radius: 8px;
}

.status-item {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: 13px;
    color: #616161;
}

.status-item.error {
    color: #F44336;
}

.status-icon {
    font-size: 14px;
}

.status-text {
    flex: 1;
}

.delivery-field {
    display: flex;
    align-items: center;
    padding: 12px 0;
    border-bottom: 1px solid #F5F5F5;
}

.slot-eta {
    font-size: 12px;
    color: #9E9E9E;
    margin-right: 12px;
    white-space: nowrap;
}

.delivery-disabled {
    text-align: center;
    padding: 24px;
}

.disabled-text {
    color: #9E9E9E;
    font-size: 14px;
}
</style>
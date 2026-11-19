<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { cartApi, orderApi, addressApi, shippingApi } from '@/utils/api'
import { formatPriceWithSymbol } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { Cart, CustomerAddress, ShippingMethod, CheckoutRequest, CheckoutResponse, CartAddress } from '@/types'

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

async function fetchData() {
    loading.value = true
    try {
        const cartId = uni.getStorageSync('cart_id') as string | null
        if (!cartId) {
            uni.showToast({ title: '购物车为空', icon: 'none' })
            setTimeout(() => uni.switchTab({ url: '/pages/cart/cart' }), 1500)
            return
        }

        const [cartRes, addrRes, shipRes] = await Promise.all([
            cartApi.getCart(cartId),
            addressApi.getAddresses(),
            shippingApi.getShippingMethods().catch(() => [])
        ])

        cart.value = cartRes
        addresses.value = addrRes
        shippingMethods.value = shipRes

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
    } catch (e) {
        console.error('获取结算数据失败:', e)
        uni.showToast({ title: '加载失败', icon: 'none' })
    } finally {
        loading.value = false
    }
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
            provider: paymentProvider.value
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
const shipping = computed(() => {
    if (!selectedShippingMethod.value) return 0
    if (selectedShippingMethod.value.type === 'pickup') return 0
    if (subtotal.value >= selectedShippingMethod.value.min_order_cents) return 0
    return selectedShippingMethod.value.fee_cents
})
const grandTotal = computed(() => subtotal.value - discount.value + shipping.value)

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
                <view class="price-row" v-if="discount > 0">
                    <text>优惠</text>
                    <text class="discount">-{{ formatPriceWithSymbol(discount) }}</text>
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
</style>
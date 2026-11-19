<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { cartApi, productApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage, getStockStatus, getStockStatusLabel, getStockStatusColor } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { Cart, CartLineItem, Product } from '@/types'

// Helper functions
function formatOptionText(optionValues: Record<string, any>): string {
    if (!optionValues) return ''
    return Object.values(optionValues).filter(v => v !== null && v !== undefined && v !== '').map(v => String(v)).join(' / ')
}

function getStockStatusClass(availableQty: number): string {
    if (availableQty <= 0) return 'out'
    if (availableQty <= 10) return 'low'
    return 'in'
}

const cart = ref<Cart | null>(null)
const loading = ref(false)
const updating = ref<string | null>(null)
const checkingOut = ref(false)

async function fetchCart() {
    loading.value = true
    try {
        const cartId = uni.getStorageSync('cart_id') as string | null
        if (!cartId) {
            cart.value = null
            return
        }
        const res = await cartApi.getCart(cartId)
        cart.value = res
    } catch (e) {
        console.error('获取购物车失败:', e)
        cart.value = null
    } finally {
        loading.value = false
    }
}

async function updateQuantity(item: CartLineItem, newQuantity: number) {
    if (newQuantity < 1) return
    if (!cart.value) return

    updating.value = item.id
    try {
        await cartApi.updateLineItem({
            cart_id: cart.value.id,
            line_item_id: item.id,
            quantity: newQuantity
        })
        // 乐观更新
        item.quantity = newQuantity
        item.line_total_cents = item.unit_price_cents * newQuantity
        recalculateCart()
    } catch (e: any) {
        uni.showToast({ title: e.message || '修改失败', icon: 'none' })
        await fetchCart() // 回滚
    } finally {
        updating.value = null
    }
}

async function removeItem(item: CartLineItem) {
    if (!cart.value) return

    try {
        await cartApi.deleteLineItem({
            cart_id: cart.value.id,
            line_item_id: item.id
        })
        uni.showToast({ title: '已删除', icon: 'success' })
        await fetchCart()
    } catch (e: any) {
        uni.showToast({ title: e.message || '删除失败', icon: 'none' })
    }
}

async function selectAllItems() {
    // 购物车全选逻辑（如果需要可以添加 selected 字段）
}

function recalculateCart() {
    if (!cart.value) return
    cart.value.subtotal_cents = cart.value.line_items.reduce((sum, item) => sum + item.line_total_cents, 0)
    cart.value.grand_total_cents = cart.value.subtotal_cents - cart.value.discount_cents + cart.value.tax_cents + cart.value.shipping_cents
}

async function goCheckout() {
    if (!cart.value || cart.value.line_items.length === 0) {
        uni.showToast({ title: '购物车为空', icon: 'none' })
        return
    }

    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login?redirect=checkout' })
        return
    }

    checkingOut.value = true
    try {
        uni.navigateTo({ url: '/pages/checkout/checkout' })
    } finally {
        checkingOut.value = false
    }
}

function onProductClick(productId: string) {
    uni.navigateTo({ url: `/pages/product/detail?id=${productId}` })
}

function getItemStockStatus(item: CartLineItem) {
    return getStockStatus(item.available_qty, item.preorder_qty_preview)
}

const selectedItemsCount = computed(() => {
    if (!cart.value) return 0
    return cart.value.line_items.reduce((sum, item) => sum + item.quantity, 0)
})

const selectedItemsTotal = computed(() => {
    if (!cart.value) return 0
    return cart.value.line_items.reduce((sum, item) => sum + item.line_total_cents, 0)
})

onMounted(() => {
    fetchCart()
})

onShow(() => {
    fetchCart()
})
</script>

<template>
    <view class="page">
        <scroll-view class="cart-scroll" scroll-y :style="{ height: 'calc(100vh - 98px)' }" refresher-enabled :refresher-triggered="loading" @refresherrefresh="fetchCart" v-if="cart && cart.line_items.length > 0">
            <view class="cart-list">
                <view class="cart-item" v-for="item in cart.line_items" :key="item.id">
                    <view class="item-main">
                        <image class="item-image" :src="getProductMainImage({ variants: [{ batches: item.batch_id ? [{ id: item.batch_id, shelf_image_url: item.shelf_image_url }] : [] }] })" mode="aspectFill" @tap="onProductClick(item.variant_id)" />
                        <view class="item-info">
                            <text class="item-title" @tap="onProductClick(item.variant_id)" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ item.product_title }}</text>
                            <text class="item-spec" v-if="item.option_values && Object.keys(item.option_values).length > 0">{{ formatOptionText(item.option_values) }}</text>
                            <view class="item-price-stock">
                                <text class="item-price">{{ formatPriceWithSymbol(item.unit_price_cents) }}</text>
                                <text class="item-unit">/ 份</text>
                                <view class="item-stock" v-if="item.available_qty > 0 && item.available_qty <= 10">
                                    <text :class="['stock-tag', getStockStatusClass(item.available_qty)]">{{ getStockStatusLabel(getItemStockStatus(item)) }}</text>
                                </view>
                            </view>
                            <view class="item-quantity">
                                <view class="qty-selector">
                                    <view class="qty-btn" @tap="updateQuantity(item, item.quantity - 1)" :class="{ disabled: item.quantity <= 1, updating: updating === item.id }">−</view>
                                    <text class="qty-value">{{ item.quantity }}</text>
                                    <view class="qty-btn" @tap="updateQuantity(item, item.quantity + 1)" :class="{ disabled: item.available_qty > 0 && item.quantity >= item.available_qty, updating: updating === item.id }">+</view>
                                </view>
                            </view>
                        </view>
                    </view>
                    <view class="item-subtotal">
                        <text class="subtotal-label">小计</text>
                        <text class="subtotal-price">{{ formatPriceWithSymbol(item.line_total_cents) }}</text>
                    </view>
                    <view class="item-delete" @tap="removeItem(item)">删除</view>
                </view>
            </view>

            <!-- 优惠/礼品卡区域 -->
            <view class="promo-section" v-if="cart && (cart.discounts.length > 0 || cart.gift_cards.length > 0)">
                <view class="promo-item" v-for="d in cart.discounts" :key="d.id">
                    <text class="promo-label">{{ d.code }} (优惠)</text>
                    <text class="promo-amount">-{{ formatPriceWithSymbol(d.applied_amount_cents) }}</text>
                </view>
                <view class="promo-item" v-for="g in cart.gift_cards" :key="g.id">
                    <text class="promo-label">{{ g.code }} (礼品卡)</text>
                    <text class="promo-amount">-{{ formatPriceWithSymbol(g.applied_amount_cents) }}</text>
                </view>
            </view>

            <view class="bottom-placeholder" />
        </scroll-view>

        <!-- 空购物车 -->
        <view class="empty-cart" v-if="!loading && (!cart || cart.line_items.length === 0)">
            <image class="empty-icon" src="/static/images/empty-cart.png" mode="aspectFit" />
            <text class="empty-title">购物车是空的</text>
            <text class="empty-desc">快去挑选心仪的生鲜好物吧</text>
            <button class="btn-go-shop" @tap="uni.switchTab({ url: '/pages/index/index' })">逛逛首页</button>
        </view>

        <!-- 底部结算栏 -->
        <view class="checkout-bar" v-if="cart && cart.line_items.length > 0">
            <view class="checkout-summary">
                <view class="summary-row">
                    <text class="summary-label">共 {{ selectedItemsCount }} 件商品</text>
                </view>
                <view class="summary-row total">
                    <text class="summary-label">合计</text>
                    <text class="summary-price">{{ formatPriceWithSymbol(selectedItemsTotal) }}</text>
                </view>
                <view class="summary-row" v-if="cart.discount_cents > 0">
                    <text class="summary-label">优惠</text>
                    <text class="summary-discount">-{{ formatPriceWithSymbol(cart.discount_cents) }}</text>
                </view>
                <view class="summary-row" v-if="cart.shipping_cents > 0">
                    <text class="summary-label">配送费</text>
                    <text class="summary-price">{{ formatPriceWithSymbol(cart.shipping_cents) }}</text>
                </view>
            </view>
            <button class="btn-checkout" @tap="goCheckout" :disabled="checkingOut">
                <text v-if="!checkingOut">去结算 ({{ formatPriceWithSymbol(cart.grand_total_cents) }})</text>
                <text v-else>提交中...</text>
            </button>
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

.cart-scroll {
    flex: 1;
}

.cart-list {
    padding: 8px 16px 16px;
}

.cart-item {
    background: #FFFFFF;
    border-radius: 12px;
    margin-bottom: 8px;
    padding: 12px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
}

.item-main {
    display: flex;
    gap: 12px;
}

.item-image {
    width: 80px;
    height: 80px;
    border-radius: 8px;
    object-fit: cover;
    flex-shrink: 0;
}

.item-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
}

.item-title {
    font-size: 14px;
    color: #1A1A1A;
    line-height: 1.4;
    margin-bottom: 4px;
}

.item-spec {
    font-size: 12px;
    color: #9E9E9E;
    margin-bottom: 8px;
}

.item-price-stock {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 8px;
}

.item-price {
    font-size: 16px;
    font-weight: 600;
    color: #2E7D32;
}

.item-unit {
    font-size: 12px;
    color: #9E9E9E;
}

.stock-tag {
    padding: 1px 6px;
    border-radius: 3px;
    font-size: 10px;
    font-weight: 500;
}

.stock-tag.in {
    background: #E8F5E9;
    color: #2E7D32;
}

.stock-tag.low {
    background: #FFF3E0;
    color: #E65100;
}

.stock-tag.out {
    background: #FBE9E7;
    color: #C62828;
}

.item-quantity {
    display: flex;
    justify-content: flex-end;
}

.qty-selector {
    display: flex;
    align-items: center;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    overflow: hidden;
}

.qty-btn {
    width: 32px;
    height: 32px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #F5F5F5;
    font-size: 16px;
    color: #1A1A1A;
}

.qty-btn.disabled {
    color: #BDBDBD;
}

.qty-btn.updating {
    opacity: 0.6;
}

.qty-value {
    width: 40px;
    text-align: center;
    font-size: 14px;
    color: #1A1A1A;
    border-left: 1px solid #E0E0E0;
    border-right: 1px solid #E0E0E0;
}

.item-subtotal {
    display: flex;
    flex-direction: column;
    align-items: flex-end;
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
}

.subtotal-label {
    font-size: 11px;
    color: #9E9E9E;
    margin-bottom: 2px;
}

.subtotal-price {
    font-size: 15px;
    font-weight: 600;
    color: #1A1A1A;
}

.item-delete {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
    text-align: right;
    font-size: 13px;
    color: #9E9E9E;
}

.promo-section {
    background: #FFFFFF;
    border-radius: 12px;
    padding: 12px 16px;
    margin: 0 16px 16px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
}

.promo-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 8px 0;
    border-bottom: 1px solid #F5F5F5;
}

.promo-item:last-child {
    border-bottom: none;
}

.promo-label {
    font-size: 13px;
    color: #616161;
}

.promo-amount {
    font-size: 14px;
    font-weight: 500;
    color: #F44336;
}

.empty-cart {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 32px;
}

.empty-icon {
    width: 120px;
    height: 120px;
    margin-bottom: 24px;
    opacity: 0.5;
}

.empty-title {
    font-size: 18px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 8px;
}

.empty-desc {
    font-size: 14px;
    color: #9E9E9E;
    margin-bottom: 24px;
}

.btn-go-shop {
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 12px 40px;
    font-size: 16px;
    font-weight: 500;
}

.checkout-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    display: flex;
    align-items: center;
    background: #FFFFFF;
    border-top: 1px solid #EEEEEE;
    padding: 12px 16px;
    z-index: 50;
    padding-bottom: calc(12px + env(safe-area-inset-bottom, 34px));
}

.checkout-summary {
    flex: 1;
    padding-right: 16px;
}

.summary-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
    font-size: 13px;
    color: #616161;
    margin-bottom: 4px;
}

.summary-row.total {
    margin-top: 4px;
    padding-top: 4px;
    border-top: 1px solid #F5F5F5;
}

.summary-label {
    color: #616161;
}

.summary-price {
    font-size: 18px;
    font-weight: 700;
    color: #2E7D32;
}

.summary-discount {
    font-size: 14px;
    font-weight: 500;
    color: #F44336;
}

.btn-checkout {
    width: 160px;
    height: 52px;
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 12px;
    font-size: 16px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
}

.btn-checkout:disabled {
    background: #A5D6A7;
}
</style>
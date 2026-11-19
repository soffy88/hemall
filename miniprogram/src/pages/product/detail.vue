<script setup lang="ts">
import { ref, onMounted, computed, watch } from 'vue'
import { productApi, cartApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage, getVariantMainImage, parseOptionValues, isWeightProduct, extractWeightBasis } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { Product, ProductVariant, ProductBatch, AddLineItemRequest } from '@/types'

const props = defineProps<{ id: string }>()

// Helper functions
function getStockStatusClass(availableQty: number): string {
    if (availableQty <= 0) return 'out'
    if (availableQty <= 10) return 'low'
    return 'in'
}

function getAllOptionKeys(): Record<string, string[]> {
    if (!product.value) return {}
    const keys: Record<string, Set<string>> = {}
    product.value.variants.forEach(v => {
        if (v.option_values) {
            Object.keys(v.option_values).forEach(key => {
                if (!keys[key]) keys[key] = new Set()
                keys[key].add(String(v.option_values![key]))
            })
        }
    })
    const result: Record<string, string[]> = {}
    Object.entries(keys).forEach(([key, set]) => {
        result[key] = Array.from(set)
    })
    return result
}

function validateQuantity() {
    if (quantity.value < 1) quantity.value = 1
    if (selectedBatch.value && quantity.value > selectedBatch.value.available_qty) {
        quantity.value = selectedBatch.value.available_qty
    }
}

const product = ref<Product | null>(null)
const loading = ref(true)
const selectedVariant = ref<ProductVariant | null>(null)
const selectedBatch = ref<ProductBatch | null>(null)
const quantity = ref(1)
const showSkuModal = ref(false)
const addingToCart = ref(false)
const cartCount = ref(0)

// 规格选择状态
const selectedOptions = ref<Record<string, string>>({})

async function fetchProduct() {
    loading.value = true
    try {
        const res = await productApi.getProduct(props.id)
        product.value = res
        // 默认选中第一个变体
        if (res.variants.length > 0) {
            selectedVariant.value = res.variants[0]
            selectDefaultOptions(res.variants[0])
        }
    } catch (e) {
        console.error('获取商品详情失败:', e)
        uni.showToast({ title: '商品不存在', icon: 'none' })
        setTimeout(() => uni.navigateBack(), 1500)
    } finally {
        loading.value = false
    }
}

function selectDefaultOptions(variant: ProductVariant) {
    selectedOptions.value = {}
    if (variant.option_values) {
        Object.entries(variant.option_values).forEach(([key, value]) => {
            selectedOptions.value[key] = String(value)
        })
    }
    // 默认选中有库存的第一个批次
    const availableBatch = variant.batches.find(b => b.available_qty > 0)
    selectedBatch.value = availableBatch || variant.batches[0] || null
}

// 根据选中的规格匹配变体
function matchVariant() {
    if (!product.value) return
    const matched = product.value.variants.find(v => {
        if (!v.option_values) return false
        return Object.entries(selectedOptions.value).every(([key, value]) => v.option_values[key] === value)
    })
    if (matched) {
        selectedVariant.value = matched
        // 重新选择默认批次
        const availableBatch = matched.batches.find(b => b.available_qty > 0)
        selectedBatch.value = availableBatch || matched.batches[0] || null
    }
}

// 规格项点击
function onOptionClick(optionKey: string, optionValue: string) {
    selectedOptions.value[optionKey] = optionValue
    matchVariant()
}

// 获取某规格下的可选值
function getOptionValues(optionKey: string): string[] {
    if (!product.value) return []
    const values = new Set<string>()
    product.value.variants.forEach(v => {
        if (v.option_values && v.option_values[optionKey] !== undefined) {
            values.add(String(v.option_values[optionKey]))
        }
    })
    return Array.from(values)
}

// 检查规格值是否可选（有库存）
function isOptionAvailable(optionKey: string, optionValue: string): boolean {
    if (!product.value) return false
    return product.value.variants.some(v =>
        v.option_values && v.option_values[optionKey] === optionValue &&
        v.batches.some(b => b.available_qty > 0)
    )
}

// 增加/减少数量
function increaseQuantity() {
    const maxStock = selectedBatch.value?.available_qty || 999
    if (quantity.value < maxStock) {
        quantity.value++
    } else {
        uni.showToast({ title: '库存不足', icon: 'none' })
    }
}

function decreaseQuantity() {
    if (quantity.value > 1) {
        quantity.value--
    }
}

// 加入购物车
async function addToCart() {
    if (!selectedVariant.value) {
        uni.showToast({ title: '请选择规格', icon: 'none' })
        return
    }

    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login' })
        return
    }

    addingToCart.value = true
    try {
        // 确保有购物车
        let cartId = uni.getStorageSync('cart_id') as string | null
        if (!cartId) {
            const cartRes = await cartApi.createCart({ region_code: 'cn-east', currency: 'CNY' })
            cartId = cartRes.cart_id
            uni.setStorageSync('cart_id', cartId)
            if (cartRes.cart_token) {
                uni.setStorageSync('cart_token', cartRes.cart_token)
            }
        }

        const request: AddLineItemRequest = {
            cart_id: cartId!,
            variant_id: selectedVariant.value.id,
            quantity: quantity.value
        }

        // 如果有选中的批次且有库存，优先用批次
        if (selectedBatch.value && selectedBatch.value.available_qty > 0) {
            request.batch_id = selectedBatch.value.id
        }

        await cartApi.addLineItem(request)
        uni.showToast({ title: '已加入购物车', icon: 'success' })
        showSkuModal.value = false
        updateCartCount()
    } catch (e: any) {
        uni.showToast({ title: e.message || '加入失败', icon: 'none' })
    } finally {
        addingToCart.value = false
    }
}

// 立即购买
async function buyNow() {
    if (!selectedVariant.value) {
        uni.showToast({ title: '请选择规格', icon: 'none' })
        return
    }

    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login' })
        return
    }

    // 先加入购物车，再跳转结算
    await addToCart()
    if (!addingToCart.value) {
        uni.switchTab({ url: '/pages/cart/cart' })
    }
}

async function updateCartCount() {
    try {
        const cartId = uni.getStorageSync('cart_id') as string | null
        if (cartId) {
            const cart = await cartApi.getCart(cartId)
            cartCount.value = cart.line_items.reduce((sum, item) => sum + item.quantity, 0)
        }
    } catch (e) {
        cartCount.value = 0
    }
}

function goCart() {
    uni.switchTab({ url: '/pages/cart/cart' })
}

function formatOptionText(optionValues: Record<string, any>): string {
    return parseOptionValues(optionValues).join(' / ')
}

function getWeightTip(): string | null {
    if (!selectedVariant.value) return null
    if (isWeightProduct(selectedVariant.value.option_values)) {
        const basis = extractWeightBasis(selectedVariant.value.option_values)
        if (basis) {
            return `按实际重量多退少补（计价基准：${basis}g/份）`
        }
        return '按实际重量多退少补'
    }
    return null
}

onMounted(() => {
    fetchProduct()
    updateCartCount()
})

watch(() => props.id, () => {
    fetchProduct()
})
</script>

<template>
    <view class="page">
        <scroll-view class="detail-scroll" scroll-y :style="{ height: 'calc(100vh - 98px)' }" refresher-enabled :refresher-triggered="loading" @refresherrefresh="fetchProduct">
            <!-- 轮播图 -->
            <view class="swiper-container">
                <swiper class="swiper" :indicator-dots="true" :autoplay="false" :circular="true">
                    <block v-if="product">
                        <swiper-item v-for="variant in product.variants" :key="variant.id">
                            <block v-for="batch in variant.batches" :key="batch.id">
                                <image class="swiper-image" :src="batch.shelf_image_url || getVariantMainImage(variant)" mode="aspectFill" v-if="batch.shelf_image_url || variant.batches[0]?.shelf_image_url" />
                            </block>
                            <image class="swiper-image" :src="getVariantMainImage(variant)" mode="aspectFill" v-if="!variant.batches.some(b => b.shelf_image_url)" />
                        </swiper-item>
                    </block>
                    <swiper-item v-else>
                        <image class="swiper-image" src="/static/images/placeholder.png" mode="aspectFill" />
                    </swiper-item>
                </swiper>
            </view>

            <!-- 商品信息 -->
            <view class="info-section" v-if="product">
                <text class="product-title">{{ product.title }}</text>
                <view class="price-row">
                    <text class="price">{{ formatPriceWithSymbol(selectedVariant?.min_price_cents || product.min_price_cents) }}</text>
                    <text class="price-unit" v-if="selectedVariant?.min_price_cents !== null || product.min_price_cents !== null">/ 份起</text>
                    <text class="original-price" v-if="selectedVariant && selectedVariant.reference_price_cents > (selectedVariant.min_price_cents || 0)">
                        {{ formatPriceWithSymbol(selectedVariant.reference_price_cents) }}
                    </text>
                </view>

                <!-- 称重商品提示 -->
                <view class="weight-tip" v-if="getWeightTip()">
                    <text class="weight-icon">⚖️</text>
                    <text class="weight-text">{{ getWeightTip() }}</text>
                </view>

                <!-- 库存状态 -->
                <view class="stock-row" v-if="selectedBatch">
                    <text :class="['stock-status', getStockStatusClass(selectedBatch.available_qty)]">
                        {{ getStockStatusLabel(getStockStatus(selectedBatch.available_qty)) }}
                    </text>
                    <text class="stock-count" v-if="selectedBatch.available_qty > 0">库存 {{ selectedBatch.available_qty }} 份</text>
                </view>

                <!-- 规格选择 -->
                <view class="spec-section" v-if="product.variants.length > 1 || (product.variants[0]?.option_values && Object.keys(product.variants[0].option_values).length > 0)">
                    <text class="spec-title">规格</text>
                    <view class="spec-options" v-for="(values, key) in getAllOptionKeys()" :key="key">
                        <text class="spec-label">{{ key }}</text>
                        <view class="spec-values">
                            <view
                                class="spec-value"
                                :class="{ selected: selectedOptions[key] === value, disabled: !isOptionAvailable(key, value) }"
                                v-for="value in values"
                                :key="value"
                                @tap="isOptionAvailable(key, value) && onOptionClick(key, value)"
                            >
                                {{ value }}
                            </view>
                        </view>
                    </view>
                </view>

                <!-- 数量选择 -->
                <view class="quantity-section">
                    <text class="spec-label">数量</text>
                    <view class="quantity-selector">
                        <view class="qty-btn" @tap="decreaseQuantity" :class="{ disabled: quantity <= 1 }">−</view>
                        <input class="qty-input" type="number" v-model.number="quantity" @blur="validateQuantity" />
                        <view class="qty-btn" @tap="increaseQuantity" :class="{ disabled: selectedBatch && quantity >= selectedBatch.available_qty }">+</view>
                    </view>
                </view>

                <!-- 商品详情 -->
                <view class="detail-section">
                    <text class="detail-title">商品详情</text>
                    <rich-text :nodes="product.description || '<p>暂无详情</p>'" />
                </view>
            </view>

            <!-- 底部占位 -->
            <view class="bottom-placeholder" />
        </scroll-view>

        <!-- 底部固定栏 -->
        <view class="bottom-bar" v-if="product">
            <view class="cart-entry" @tap="goCart">
                <view class="cart-icon">🛒</view>
                <view class="cart-badge" v-if="cartCount > 0">{{ cartCount > 99 ? '99+' : cartCount }}</view>
            </view>
            <view class="btn-group">
                <button class="btn-add-cart" @tap="showSkuModal = true" :disabled="addingToCart">
                    <text v-if="!addingToCart">加入购物车</text>
                    <text v-else>添加中...</text>
                </button>
                <button class="btn-buy-now" @tap="buyNow" :disabled="addingToCart">立即购买</button>
            </view>
        </view>

        <!-- 规格选择弹窗 -->
        <view class="sku-modal-mask" v-if="showSkuModal" @tap="showSkuModal = false">
            <view class="sku-modal" @tap.stop>
                <view class="sku-header">
                    <text class="sku-title">选择规格</text>
                    <view class="sku-close" @tap="showSkuModal = false">✕</view>
                </view>
                <view class="sku-content">
                    <view class="sku-product">
                        <image class="sku-image" :src="selectedVariant ? getVariantMainImage(selectedVariant) : getProductMainImage(product!)" mode="aspectFill" />
                        <view class="sku-product-info">
                            <text class="sku-product-title">{{ product?.title }}</text>
                            <view class="sku-price">
                                <text class="price">{{ formatPriceWithSymbol(selectedVariant?.min_price_cents || product?.min_price_cents) }}</text>
                            </view>
                            <view class="sku-selected-spec" v-if="selectedVariant">
                                {{ formatOptionText(selectedVariant.option_values) }}
                            </view>
                        </view>
                    </view>

                    <view class="spec-section" v-if="product">
                        <text class="spec-title">规格</text>
                        <view class="spec-options" v-for="(values, key) in getAllOptionKeys()" :key="key">
                            <text class="spec-label">{{ key }}</text>
                            <view class="spec-values">
                                <view
                                    class="spec-value"
                                    :class="{ selected: selectedOptions[key] === value, disabled: !isOptionAvailable(key, value) }"
                                    v-for="value in values"
                                    :key="value"
                                    @tap="isOptionAvailable(key, value) && onOptionClick(key, value)"
                                >
                                    {{ value }}
                                </view>
                            </view>
                        </view>
                    </view>

                    <view class="quantity-section">
                        <text class="spec-label">数量</text>
                        <view class="quantity-selector">
                            <view class="qty-btn" @tap="decreaseQuantity" :class="{ disabled: quantity <= 1 }">−</view>
                            <input class="qty-input" type="number" v-model.number="quantity" @blur="validateQuantity" />
                            <view class="qty-btn" @tap="increaseQuantity" :class="{ disabled: selectedBatch && quantity >= selectedBatch.available_qty }">+</view>
                        </view>
                        <text class="stock-hint" v-if="selectedBatch">库存 {{ selectedBatch.available_qty }} 份</text>
                    </view>
                </view>
                <view class="sku-footer">
                    <button class="sku-btn-cancel" @tap="showSkuModal = false">取消</button>
                    <button class="sku-btn-confirm" @tap="addToCart; showSkuModal = false" :disabled="addingToCart">确定</button>
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

.detail-scroll {
    flex: 1;
}

.swiper-container {
    width: 100%;
    height: 400px;
    background: #FFFFFF;
}

.swiper {
    width: 100%;
    height: 100%;
}

.swiper-image {
    width: 100%;
    height: 100%;
}

.info-section {
    background: #FFFFFF;
    padding: 16px;
    margin-top: 8px;
}

.product-title {
    font-size: 17px;
    font-weight: 600;
    color: #1A1A1A;
    line-height: 1.4;
    margin-bottom: 12px;
}

.price-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-bottom: 8px;
}

.price {
    font-size: 24px;
    font-weight: 700;
    color: #2E7D32;
}

.price-unit {
    font-size: 13px;
    color: #9E9E9E;
}

.original-price {
    font-size: 14px;
    color: #9E9E9E;
    text-decoration: line-through;
}

.weight-tip {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 10px 12px;
    background: #E8F5E9;
    border-radius: 8px;
    margin-bottom: 12px;
}

.weight-icon {
    font-size: 14px;
}

.weight-text {
    font-size: 13px;
    color: #2E7D32;
    font-weight: 500;
}

.stock-row {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 16px;
}

.stock-status {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
}

.stock-status.in {
    background: #E8F5E9;
    color: #2E7D32;
}

.stock-status.low {
    background: #FFF3E0;
    color: #E65100;
}

.stock-status.out {
    background: #FBE9E7;
    color: #C62828;
}

.stock-count {
    font-size: 13px;
    color: #9E9E9E;
}

.spec-section {
    margin-bottom: 16px;
}

.spec-title {
    font-size: 14px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 10px;
}

.spec-options {
    margin-bottom: 12px;
}

.spec-label {
    display: block;
    font-size: 13px;
    color: #616161;
    margin-bottom: 8px;
}

.spec-values {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.spec-value {
    padding: 8px 16px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 13px;
    color: #1A1A1A;
    background: #FFFFFF;
    min-width: 60px;
    text-align: center;
}

.spec-value.selected {
    border-color: #2E7D32;
    background: #E8F5E9;
    color: #2E7D32;
}

.spec-value.disabled {
    border-color: #EEEEEE;
    color: #BDBDBD;
    background: #FAFAFA;
}

.quantity-section {
    padding-top: 16px;
    border-top: 1px solid #EEEEEE;
}

.quantity-selector {
    display: flex;
    align-items: center;
    gap: 0;
    width: fit-content;
}

.qty-btn {
    width: 40px;
    height: 40px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #F5F5F5;
    border: 1px solid #E0E0E0;
    font-size: 18px;
    color: #1A1A1A;
}

.qty-btn:first-child {
    border-radius: 8px 0 0 8px;
    border-right: none;
}

.qty-btn:last-child {
    border-radius: 0 8px 8px 0;
    border-left: none;
}

.qty-btn.disabled {
    color: #BDBDBD;
    background: #FAFAFA;
}

.qty-input {
    width: 60px;
    height: 40px;
    text-align: center;
    border: 1px solid #E0E0E0;
    border-left: none;
    border-right: none;
    font-size: 16px;
    color: #1A1A1A;
}

.stock-hint {
    margin-left: 12px;
    font-size: 13px;
    color: #9E9E9E;
}

.detail-section {
    margin-top: 24px;
    padding-top: 16px;
    border-top: 1px solid #EEEEEE;
}

.detail-title {
    font-size: 14px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 12px;
}

.bottom-placeholder {
    height: 98px;
}

.bottom-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    display: flex;
    align-items: center;
    padding: 12px 16px;
    background: #FFFFFF;
    border-top: 1px solid #EEEEEE;
    z-index: 50;
    padding-bottom: calc(12px + env(safe-area-inset-bottom, 34px));
}

.cart-entry {
    position: relative;
    width: 56px;
    height: 56px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #F5F5F5;
    border-radius: 12px;
    margin-right: 12px;
}

.cart-icon {
    font-size: 24px;
}

.cart-badge {
    position: absolute;
    top: -4px;
    right: -4px;
    min-width: 18px;
    height: 18px;
    background: #F44336;
    color: #FFFFFF;
    font-size: 10px;
    border-radius: 9px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 0 4px;
}

.btn-group {
    flex: 1;
    display: flex;
    gap: 12px;
}

.btn-add-cart,
.btn-buy-now {
    flex: 1;
    height: 56px;
    border-radius: 12px;
    font-size: 16px;
    font-weight: 600;
    border: none;
    display: flex;
    align-items: center;
    justify-content: center;
}

.btn-add-cart {
    background: #FFFFFF;
    color: #2E7D32;
    border: 1px solid #2E7D32;
}

.btn-add-cart:disabled {
    background: #F5F5F5;
    color: #BDBDBD;
    border-color: #E0E0E0;
}

.btn-buy-now {
    background: #2E7D32;
    color: #FFFFFF;
}

.btn-buy-now:disabled {
    background: #A5D6A7;
}

/* SKU 弹窗 */
.sku-modal-mask {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: rgba(0, 0, 0, 0.5);
    display: flex;
    align-items: flex-end;
    justify-content: center;
    z-index: 100;
    animation: fadeIn 0.2s ease;
}

@keyframes fadeIn {
    from { opacity: 0; }
    to { opacity: 1; }
}

.sku-modal {
    width: 100%;
    max-height: 80vh;
    background: #FFFFFF;
    border-radius: 16px 16px 0 0;
    display: flex;
    flex-direction: column;
    animation: slideUp 0.3s ease;
}

@keyframes slideUp {
    from { transform: translateY(100%); }
    to { transform: translateY(0); }
}

.sku-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px;
    border-bottom: 1px solid #EEEEEE;
    position: sticky;
    top: 0;
    background: #FFFFFF;
    z-index: 10;
}

.sku-title {
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
}

.sku-close {
    font-size: 20px;
    color: #9E9E9E;
}

.sku-content {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
}

.sku-product {
    display: flex;
    gap: 12px;
    margin-bottom: 20px;
    padding-bottom: 16px;
    border-bottom: 1px solid #EEEEEE;
}

.sku-image {
    width: 80px;
    height: 80px;
    border-radius: 8px;
    object-fit: cover;
}

.sku-product-info {
    flex: 1;
    min-width: 0;
}

.sku-product-title {
    font-size: 14px;
    color: #1A1A1A;
    line-height: 1.4;
    margin-bottom: 8px;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    overflow: hidden;
}

.sku-price {
    margin-bottom: 8px;
}

.sku-selected-spec {
    font-size: 13px;
    color: #9E9E9E;
}

.sku-footer {
    display: flex;
    gap: 12px;
    padding: 16px;
    border-top: 1px solid #EEEEEE;
    background: #FFFFFF;
    position: sticky;
    bottom: 0;
    padding-bottom: calc(16px + env(safe-area-inset-bottom, 34px));
}

.sku-btn-cancel,
.sku-btn-confirm {
    flex: 1;
    height: 52px;
    border-radius: 12px;
    font-size: 16px;
    font-weight: 600;
    border: none;
}

.sku-btn-cancel {
    background: #F5F5F5;
    color: #424242;
}

.sku-btn-confirm {
    background: #2E7D32;
    color: #FFFFFF;
}

.sku-btn-confirm:disabled {
    background: #A5D6A7;
}
</style>
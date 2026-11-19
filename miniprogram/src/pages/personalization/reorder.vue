<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { personalizationApi, cartApi } from '@/utils/api'
import { formatPriceWithSymbol, formatDateTime, getProductMainImage } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { ReorderLastOrder, ReorderLineItem } from '@/types'

const lastOrder = ref<ReorderLastOrder | null>(null)
const loading = ref(false)
const applying = ref(false)
const selectedItems = ref<Set<string>>(new Set()) // variant_id set

const totalSelectedQty = computed(() => {
    if (!lastOrder.value) return 0
    return lastOrder.value.line_items
        .filter(item => selectedItems.value.has(item.variant_id))
        .reduce((sum, item) => sum + item.quantity, 0)
})

const totalSelectedPrice = computed(() => {
    if (!lastOrder.value) return 0
    return lastOrder.value.line_items
        .filter(item => selectedItems.value.has(item.variant_id))
        .reduce((sum, item) => sum + item.unit_price_cents * item.quantity, 0)
})

async function fetchLastOrder() {
    loading.value = true
    try {
        const res = await personalizationApi.getLastOrder()
        lastOrder.value = res.order
        // 默认全选
        if (res.order?.line_items) {
            selectedItems.value = new Set(res.order.line_items.map(item => item.variant_id))
        }
    } catch (e: any) {
        if (e.message?.includes('404') || e.message?.includes('no eligible')) {
            lastOrder.value = null
        } else {
            uni.showToast({ title: e.message || '加载失败', icon: 'none' })
        }
    } finally {
        loading.value = false
    }
}

function toggleItem(variantId: string) {
    if (selectedItems.value.has(variantId)) {
        selectedItems.value.delete(variantId)
    } else {
        selectedItems.value.add(variantId)
    }
}

function selectAll() {
    if (!lastOrder.value) return
    if (selectedItems.value.size === lastOrder.value.line_items.length) {
        selectedItems.value.clear()
    } else {
        selectedItems.value = new Set(lastOrder.value.line_items.map(item => item.variant_id))
    }
}

async function applyReorder() {
    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login?redirect=personalization_reorder' })
        return
    }

    if (!lastOrder.value || selectedItems.value.size === 0) {
        uni.showToast({ title: '请至少选择一个商品', icon: 'none' })
        return
    }

    const variantQtyOverrides: Record<string, number> = {}
    const skipVariantIds: string[] = []

    lastOrder.value.line_items.forEach(item => {
        if (!selectedItems.value.has(item.variant_id)) {
            skipVariantIds.push(item.variant_id)
        }
    })

    applying.value = true
    try {
        const res = await personalizationApi.applyReorder({
            order_id: lastOrder.value.id,
            variant_qty_overrides: variantQtyOverrides,
            skip_variant_ids: skipVariantIds
        })
        uni.showToast({ title: res.message || '已加入购物车', icon: 'success' })
        setTimeout(() => {
            uni.switchTab({ url: '/pages/cart/cart' })
        }, 1500)
    } catch (e: any) {
        uni.showToast({ title: e.message || '加入失败', icon: 'none' })
    } finally {
        applying.value = false
    }
}

function goProduct(variantId: string) {
    uni.navigateTo({ url: `/pages/product/detail?variant_id=${variantId}` })
}

function formatOptionText(optionValues: Record<string, any>): string {
    if (!optionValues) return ''
    return Object.values(optionValues).filter(v => v !== null && v !== undefined && v !== '').map(v => String(v)).join(' / ')
}

onMounted(() => {
    fetchLastOrder()
})

onShow(() => {
    fetchLastOrder()
})
</script>

<template>
    <view class="page">
        <scroll-view
            class="reorder-scroll"
            scroll-y
            :style="{ height: 'calc(100vh - 98px)' }"
            refresher-enabled
            :refresher-triggered="loading"
            @refresherrefresh="fetchLastOrder"
            v-if="lastOrder"
        >
            <!-- 订单信息头部 -->
            <view class="order-header">
                <view class="order-info">
                    <text class="order-label">上次下单</text>
                    <text class="order-time">{{ formatDateTime(lastOrder.created_at) }}</text>
                    <text class="order-status" :class="lastOrder.status">{{ lastOrder.status }}</text>
                </view>
                <view class="order-summary">
                    <text class="summary-text">共 {{ lastOrder.line_items.length }} 种商品</text>
                </view>
            </view>

            <!-- 全选/反选 -->
            <view class="select-all-bar" @tap="selectAll">
                <text class="select-all-text">
                    {{ selectedItems.size === lastOrder?.line_items?.length ? '取消全选' : '全选' }}
                </text>
                <text class="select-all-count">已选 {{ selectedItems.size }}/{{ lastOrder?.line_items?.length || 0 }}</text>
            </view>

            <!-- 商品列表 -->
            <view class="product-list">
                <view
                    class="product-item"
                    v-for="item in lastOrder.line_items"
                    :key="item.variant_id"
                    :class="{ selected: selectedItems.has(item.variant_id) }"
                    @tap="toggleItem(item.variant_id)"
                >
                    <view class="item-checkbox">
                        <checkbox :checked="selectedItems.has(item.variant_id)" />
                    </view>
                    <view class="item-main">
                        <image
                            class="item-image"
                            :src="getProductMainImage({ variants: [{ batches: item.batch_id ? [{ id: item.batch_id }] : [] }] })"
                            mode="aspectFill"
                        />
                        <view class="item-info">
                            <text class="item-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ item.title }}</text>
                            <text class="item-spec" v-if="item.weight_option_label">{{ item.weight_option_label }}</text>
                            <view class="item-price-qty">
                                <text class="item-price">{{ formatPriceWithSymbol(item.unit_price_cents) }}</text>
                                <text class="item-qty">x {{ item.quantity }}</text>
                            </view>
                        </view>
                    </view>
                    <view class="item-subtotal">
                        {{ formatPriceWithSymbol(item.unit_price_cents * item.quantity) }}
                    </view>
                </view>
            </view>

            <!-- 底部操作栏 -->
            <view class="bottom-bar">
                <view class="bottom-summary">
                    <text class="summary-label">已选 {{ totalSelectedQty }} 件商品</text>
                    <text class="summary-price">{{ formatPriceWithSymbol(totalSelectedPrice) }}</text>
                </view>
                <button class="btn-apply" @tap="applyReorder" :disabled="applying || selectedItems.size === 0">
                    <text v-if="!applying">加入购物车 ({{ formatPriceWithSymbol(totalSelectedPrice) }})</text>
                    <text v-else>加入中...</text>
                </button>
            </view>

            <view class="bottom-spacer" />
        </scroll-view>

        <!-- 空状态 -->
        <view class="empty-state" v-if="!loading && !lastOrder">
            <image class="empty-icon" src="/static/images/empty-order.png" mode="aspectFit" />
            <text class="empty-title">暂无历史订单</text>
            <text class="empty-desc">去首页挑选心仪好物，下次就能"再来一单"啦</text>
            <button class="btn-go-shop" @tap="uni.switchTab({ url: '/pages/index/index' })">逛逛首页</button>
        </view>

        <view class="loading-overlay" v-if="loading && !lastOrder">
            <view class="spinner" />
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

.reorder-scroll {
    flex: 1;
}

.order-header {
    background: #FFFFFF;
    padding: 16px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.order-info {
    display: flex;
    flex-direction: column;
    gap: 4px;
}

.order-label {
    font-size: 13px;
    color: #9E9E9E;
}

.order-time {
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
}

.order-status {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 500;
    width: fit-content;
}

.order-status.completed,
.order-status.delivered {
    background: #E8F5E9;
    color: #2E7D32;
}

.order-status.paid,
.order-status.confirmed {
    background: #E3F2FD;
    color: #1565C0;
}

.order-summary {
    text-align: right;
}

.summary-text {
    font-size: 13px;
    color: #616161;
}

.select-all-bar {
    background: #FFFFFF;
    padding: 12px 16px;
    margin-bottom: 8px;
    display: flex;
    justify-content: space-between;
    align-items: center;
    border-bottom: 1px solid #F5F5F5;
}

.select-all-text {
    font-size: 14px;
    color: #2E7D32;
    font-weight: 500;
}

.select-all-count {
    font-size: 13px;
    color: #9E9E9E;
}

.product-list {
    background: #FFFFFF;
}

.product-item {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    border-bottom: 1px solid #F5F5F5;
    gap: 12px;
}

.product-item:last-child {
    border-bottom: none;
}

.product-item.selected {
    background: #E8F5E9;
}

.item-checkbox {
    width: 24px;
    display: flex;
    align-items: center;
    justify-content: center;
}

.item-main {
    flex: 1;
    min-width: 0;
    display: flex;
    gap: 12px;
}

.item-image {
    width: 70px;
    height: 70px;
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

.item-price-qty {
    display: flex;
    align-items: center;
    gap: 8px;
}

.item-price {
    font-size: 15px;
    font-weight: 600;
    color: #2E7D32;
}

.item-qty {
    font-size: 13px;
    color: #9E9E9E;
}

.item-subtotal {
    font-size: 15px;
    font-weight: 600;
    color: #1A1A1A;
    text-align: right;
    min-width: 80px;
}

.bottom-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    background: #FFFFFF;
    border-top: 1px solid #EEEEEE;
    padding: 12px 16px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    z-index: 50;
    padding-bottom: calc(12px + env(safe-area-inset-bottom, 34px));
}

.bottom-summary {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.summary-label {
    font-size: 13px;
    color: #616161;
}

.summary-price {
    font-size: 18px;
    font-weight: 700;
    color: #2E7D32;
}

.btn-apply {
    width: 160px;
    height: 48px;
    background: linear-gradient(135deg, #2E7D32, #4CAF50);
    color: #FFFFFF;
    border: none;
    border-radius: 24px;
    font-size: 15px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 4px 12px rgba(46, 125, 50, 0.3);
}

.btn-apply:disabled {
    background: #A5D6A7;
    box-shadow: none;
}

.empty-state {
    flex: 1;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 48px 24px;
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
    text-align: center;
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

.loading-overlay {
    position: fixed;
    top: 0;
    left: 0;
    right: 0;
    bottom: 0;
    background: #F5F5F5;
    display: flex;
    align-items: center;
    justify-content: center;
    z-index: 100;
}

.spinner {
    width: 32px;
    height: 32px;
    border: 3px solid #E0E0E0;
    border-top-color: #2E7D32;
    border-radius: 50%;
    animation: spin 1s linear infinite;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}

.bottom-spacer {
    height: 100px;
}
</style>
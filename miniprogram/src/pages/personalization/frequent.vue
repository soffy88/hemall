<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { personalizationApi, cartApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { FrequentItem } from '@/types'

const items = ref<FrequentItem[]>([])
const loading = ref(false)
const addingAll = ref(false)
const selectedItems = ref<Set<string>>(new Set())

const totalSelectedQty = computed(() => {
    return items.value
        .filter(item => selectedItems.value.has(item.variant_id))
        .reduce((sum, item) => sum + 1, 0)
})

const totalSelectedPrice = computed(() => {
    return items.value
        .filter(item => selectedItems.value.has(item.variant_id))
        .reduce((sum, item) => sum + (item.min_price_cents || 0), 0)
})

async function fetchFrequent() {
    loading.value = true
    try {
        const res = await personalizationApi.getFrequent()
        items.value = res.items || []
        // 默认全选
        selectedItems.value = new Set(items.value.map(item => item.variant_id))
    } catch (e: any) {
        uni.showToast({ title: e.message || '加载失败', icon: 'none' })
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
    if (selectedItems.value.size === items.value.length) {
        selectedItems.value.clear()
    } else {
        selectedItems.value = new Set(items.value.map(item => item.variant_id))
    }
}

async function addSelectedToCart() {
    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login?redirect=personalization_frequent' })
        return
    }

    if (selectedItems.value.size === 0) {
        uni.showToast({ title: '请至少选择一个商品', icon: 'none' })
        return
    }

    const variantIds = Array.from(selectedItems.value)
    addingAll.value = true
    try {
        const res = await personalizationApi.addFrequentToCart(variantIds)
        uni.showToast({ title: res.message || `已加入 ${res.added} 件商品`, icon: 'success' })
        setTimeout(() => {
            uni.switchTab({ url: '/pages/cart/cart' })
        }, 1500)
    } catch (e: any) {
        uni.showToast({ title: e.message || '加入失败', icon: 'none' })
    } finally {
        addingAll.value = false
    }
}

function goProduct(variantId: string) {
    uni.navigateTo({ url: `/pages/product/detail?variant_id=${variantId}` })
}

function formatFrequency(count: number): string {
    return `${count} 次`
}

function formatAvgPrice(cents: number): string {
    return formatPriceWithSymbol(cents)
}

onMounted(() => {
    fetchFrequent()
})

onShow(() => {
    fetchFrequent()
})
</script>

<template>
    <view class="page">
        <scroll-view
            class="frequent-scroll"
            scroll-y
            :style="{ height: 'calc(100vh - 98px)' }"
            refresher-enabled
            :refresher-triggered="loading"
            @refresherrefresh="fetchFrequent"
        >
            <!-- 头部说明 -->
            <view class="header-info">
                <text class="info-text">基于您近期购买记录生成，共 {{ items.length }} 种常买商品</text>
            </view>

            <!-- 全选/反选 -->
            <view class="select-all-bar" @tap="selectAll">
                <text class="select-all-text">
                    {{ selectedItems.size === items.length ? '取消全选' : '全选' }}
                </text>
                <text class="select-all-count">已选 {{ selectedItems.size }}/{{ items.length }}</text>
            </view>

            <!-- 商品列表 -->
            <view class="product-list" v-if="items.length > 0">
                <view
                    class="product-item"
                    v-for="item in items"
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
                            :src="item.image_url || '/static/images/placeholder-product.png'"
                            mode="aspectFill"
                        />
                        <view class="item-info">
                            <text class="item-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ item.product_name }}</text>
                            <view class="item-meta">
                                <text class="meta-tag freq-tag">
                                    <text class="icon">🔁</text>
                                    {{ formatFrequency(item.purchase_count) }}
                                </text>
                                <text class="meta-tag avg-price-tag">
                                    均价 {{ formatAvgPrice(item.avg_price_cents) }}
                                </text>
                            </view>
                            <view class="item-price-row">
                                <text class="item-price">{{ formatPriceWithSymbol(item.min_price_cents || 0) }}</text>
                            </view>
                        </view>
                    </view>
                    <view class="item-qty">
                        共买 {{ item.total_qty }} 件
                    </view>
                </view>
            </view>

            <!-- 空状态 -->
            <view class="empty-state" v-if="!loading && items.length === 0">
                <image class="empty-icon" src="/static/images/empty-frequent.png" mode="aspectFit" />
                <text class="empty-title">暂无常买清单</text>
                <text class="empty-desc">多买几次，系统就会为您生成专属常买清单</text>
                <button class="btn-go-shop" @tap="uni.switchTab({ url: '/pages/index/index' })">去逛逛</button>
            </view>

            <!-- 底部操作栏 -->
            <view class="bottom-bar" v-if="items.length > 0">
                <view class="bottom-summary">
                    <text class="summary-label">已选 {{ totalSelectedQty }} 件商品</text>
                    <text class="summary-price">{{ formatPriceWithSymbol(totalSelectedPrice) }}</text>
                </view>
                <button class="btn-apply" @tap="addSelectedToCart" :disabled="addingAll || selectedItems.size === 0">
                    <text v-if="!addingAll">一键加购 ({{ formatPriceWithSymbol(totalSelectedPrice) }})</text>
                    <text v-else>加入中...</text>
                </button>
            </view>

            <view class="bottom-spacer" v-if="items.length > 0" />
        </scroll-view>

        <view class="loading-overlay" v-if="loading && items.length === 0">
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

.frequent-scroll {
    flex: 1;
}

.header-info {
    padding: 12px 16px;
    background: #FFFFFF;
    margin-bottom: 8px;
}

.info-text {
    font-size: 13px;
    color: #616161;
    line-height: 1.5;
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
    margin-bottom: 6px;
}

.item-meta {
    display: flex;
    gap: 8px;
    margin-bottom: 8px;
    flex-wrap: wrap;
}

.meta-tag {
    font-size: 11px;
    padding: 2px 6px;
    border-radius: 4px;
    display: flex;
    align-items: center;
    gap: 3px;
}

.freq-tag {
    background: #E3F2FD;
    color: #1565C0;
}

.avg-price-tag {
    background: #FFF3E0;
    color: #E65100;
}

.icon {
    font-size: 10px;
}

.item-price-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
}

.item-price {
    font-size: 15px;
    font-weight: 600;
    color: #2E7D32;
}

.item-qty {
    font-size: 12px;
    color: #9E9E9E;
    text-align: right;
    min-width: 70px;
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
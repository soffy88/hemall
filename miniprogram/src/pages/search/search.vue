<script setup lang="ts">
import { ref, onMounted, watch, computed } from 'vue'
import { productApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage, debounce } from '@/utils/helpers'
import type { Product } from '@/types'

const searchKeyword = ref('')
const products = ref<Product[]>([])
const loading = false
const hasMore = ref(true)
const page = ref(0)
const pageSize = 20
const showHistory = ref(true)
const searchHistory = ref<string[]>([])
const showClearHistory = ref(false)

// 从存储加载搜索历史
function loadSearchHistory() {
    const history = uni.getStorageSync('search_history') as string[] || []
    searchHistory.value = history.slice(0, 10)
}

// 保存搜索历史
function saveSearchHistory(keyword: string) {
    if (!keyword.trim()) return
    const history = [...searchHistory.value]
    const index = history.indexOf(keyword)
    if (index > -1) history.splice(index, 1)
    history.unshift(keyword)
    searchHistory.value = history.slice(0, 10)
    uni.setStorageSync('search_history', searchHistory.value)
}

// 清空搜索历史
function clearSearchHistory() {
    searchHistory.value = []
    uni.removeStorageSync('search_history')
}

// 处理清空历史确认
function handleClearHistory() {
    clearSearchHistory()
    showClearHistory.value = false
}

// 搜索商品（防抖）
const debouncedSearch = debounce(async (keyword: string) => {
    if (!keyword.trim()) {
        products.value = []
        showHistory.value = true
        hasMore.value = true
        page.value = 0
        return
    }

    showHistory.value = false
    page.value = 0
    products.value = []
    hasMore.value = true
    await fetchProducts(keyword)
}, 300)

const hotWords = ['有机苹果', '草莓', '西兰花', '三文鱼', '牛奶', '鸡蛋', '西瓜', '葡萄']

function onProductClick(product: any) {
    uni.navigateTo({ url: `/pages/product/detail?id=${product.id}` })
}

async function fetchProducts(keyword: string, reset = false) {
    if (reset) {
        page.value = 0
        products.value = []
        hasMore.value = true
    }
    if (!hasMore.value) return

    try {
        const res = await productApi.getProducts({
            search: keyword,
            limit: pageSize,
            offset: page.value * pageSize
        })
        if (res.length < pageSize) {
            hasMore.value = false
        }
        products.value = [...products.value, ...res]
        page.value++
    } catch (e) {
        console.error('搜索失败:', e)
        hasMore.value = false
    }
}

async function onSearchConfirm() {
    const keyword = searchKeyword.value.trim()
    if (!keyword) return
    saveSearchHistory(keyword)
    await fetchProducts(keyword, true)
}

function onKeywordChange(value: string) {
    searchKeyword.value = value
    debouncedSearch(value)
}

function onHistoryClick(keyword: string) {
    searchKeyword.value = keyword
    onSearchConfirm()
}

function onClearKeyword() {
    searchKeyword.value = ''
    products.value = []
    showHistory.value = true
    hasMore.value = true
    page.value = 0
}

async function onLoadMore() {
    if (!loading && hasMore.value && searchKeyword.value.trim()) {
        await fetchProducts(searchKeyword.value.trim())
    }
}

onMounted(() => {
    loadSearchHistory()
})
</script>

<template>
    <view class="page">
        <!-- 搜索栏 -->
        <view class="search-header">
            <view class="search-input-wrapper">
                <text class="search-icon">🔍</text>
                <input
                    class="search-input"
                    type="text"
                    v-model="searchKeyword"
                    @input="onKeywordChange($event.detail.value)"
                    @confirm="onSearchConfirm"
                    placeholder="搜索水果、蔬菜、生鲜..."
                    confirm-type="search"
                />
                <view class="search-clear" v-if="searchKeyword" @tap="onClearKeyword">✕</view>
            </view>
            <text class="search-cancel" @tap="onClearKeyword">取消</text>
        </view>

        <!-- 搜索历史 -->
        <view class="history-section" v-if="showHistory && searchHistory.length">
            <view class="history-header">
                <text class="history-title">搜索历史</text>
                <text class="history-clear" @tap="showClearHistory = true">清空</text>
            </view>
            <view class="history-tags">
                <view
                    class="history-tag"
                    v-for="(keyword, index) in searchHistory"
                    :key="index"
                    @tap="onHistoryClick(keyword)"
                >
                    {{ keyword }}
                </view>
            </view>
        </view>

        <!-- 热门搜索占位 -->
        <view class="history-section" v-if="showHistory">
            <view class="history-header">
                <text class="history-title">热门搜索</text>
            </view>
            <view class="history-tags">
                <view class="history-tag" v-for="(word, index) in hotWords" :key="index" @tap="onHistoryClick(word)">
                    {{ word }}
                </view>
            </view>
        </view>

        <!-- 搜索结果 -->
        <view class="result-section" v-if="!showHistory">
            <scroll-view
                class="result-scroll"
                scroll-y
                :style="{ height: 'calc(100vh - 100px)' }"
                @scrolltolower="onLoadMore"
            >
                <view class="product-grid" v-if="products.length">
                    <view class="product-card" v-for="product in products" :key="product.id" @tap="onProductClick(product)">
                        <image class="product-image" :src="getProductMainImage(product)" mode="aspectFill" />
                        <view class="product-info">
                            <text class="product-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ product.title }}</text>
                            <view class="product-price-row">
                                <text class="price">{{ formatPriceWithSymbol(product.min_price_cents) }}</text>
                                <text class="price-unit" v-if="product.min_price_cents !== null">/ 份起</text>
                            </view>
                        </view>
                    </view>
                </view>
                <view class="empty-state" v-else-if="searchKeyword.trim() && !loading">
                    <image class="empty-icon" src="/static/images/empty-search.png" mode="aspectFit" />
                    <text class="empty-text">未找到相关商品</text>
                    <text class="empty-subtext">试试其他关键词</text>
                </view>

                <view class="load-more" v-if="!hasMore && products.length">
                    <text class="load-more-text">已加载全部商品</text>
                </view>
                <view class="load-more" v-if="loading && products.length">
                    <uni-loading-icon />
                    <text class="load-more-text">加载中...</text>
                </view>
            </scroll-view>
        </view>

        <!-- 清空历史确认弹窗 -->
        <view class="modal-mask" v-if="showClearHistory" @tap="showClearHistory = false">
            <view class="modal" @tap.stop>
                <text class="modal-title">确定清空搜索历史吗？</text>
                <view class="modal-actions">
                    <text class="modal-btn cancel" @tap="showClearHistory = false">取消</text>
                    <text class="modal-btn confirm" @tap="handleClearHistory">清空</text>
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

.search-header {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    background: #FFFFFF;
    position: sticky;
    top: 0;
    z-index: 10;
}

.search-input-wrapper {
    flex: 1;
    display: flex;
    align-items: center;
    background: #F5F5F5;
    border-radius: 24px;
    padding: 0 16px;
    height: 40px;
}

.search-icon {
    font-size: 16px;
    margin-right: 8px;
    color: #9E9E9E;
}

.search-input {
    flex: 1;
    border: none;
    background: transparent;
    font-size: 15px;
    color: #1A1A1A;
    height: 100%;
}

.search-input::placeholder {
    color: #9E9E9E;
}

.search-clear {
    font-size: 14px;
    color: #9E9E9E;
    padding: 4px;
}

.search-cancel {
    margin-left: 12px;
    font-size: 15px;
    color: #2E7D32;
    font-weight: 500;
}

.history-section {
    background: #FFFFFF;
    padding: 16px;
    margin-bottom: 8px;
}

.history-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
}

.history-title {
    font-size: 15px;
    font-weight: 600;
    color: #1A1A1A;
}

.history-clear {
    font-size: 13px;
    color: #2E7D32;
}

.history-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.history-tag {
    padding: 8px 16px;
    background: #F5F5F5;
    border-radius: 20px;
    font-size: 13px;
    color: #424242;
}

.result-section {
    flex: 1;
    overflow: hidden;
}

.result-scroll {
    height: 100%;
}

.product-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    padding: 8px 16px 24px;
}

.product-card {
    background: #FFFFFF;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.product-image {
    width: 100%;
    aspect-ratio: 1 / 1;
    object-fit: cover;
}

.product-info {
    padding: 12px;
}

.product-title {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    overflow: hidden;
    font-size: 13px;
    color: #1A1A1A;
    line-height: 1.4;
    margin-bottom: 8px;
}

.product-price-row {
    display: flex;
    align-items: baseline;
}

.price {
    font-size: 16px;
    font-weight: 600;
    color: #2E7D32;
}

.price-unit {
    font-size: 11px;
    color: #9E9E9E;
}

.empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 60px 24px;
    color: #9E9E9E;
}

.empty-icon {
    width: 100px;
    height: 100px;
    margin-bottom: 16px;
    opacity: 0.5;
}

.empty-text {
    font-size: 16px;
    font-weight: 500;
    margin-bottom: 4px;
}

.empty-subtext {
    font-size: 13px;
}

.load-more {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 24px;
    color: #9E9E9E;
    font-size: 13px;
}

.load-more-text {
    margin-top: 8px;
}

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
}

.modal {
    background: #FFFFFF;
    border-radius: 12px;
    padding: 24px;
    width: 80%;
    max-width: 300px;
    text-align: center;
}

.modal-title {
    font-size: 16px;
    color: #1A1A1A;
    margin-bottom: 20px;
}

.modal-actions {
    display: flex;
    gap: 12px;
}

.modal-btn {
    flex: 1;
    padding: 12px;
    border-radius: 8px;
    font-size: 15px;
    font-weight: 500;
}

.modal-btn.cancel {
    background: #F5F5F5;
    color: #424242;
}

.modal-btn.confirm {
    background: #2E7D32;
    color: #FFFFFF;
}
</style>
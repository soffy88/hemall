<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { categoryApi, productApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage } from '@/utils/helpers'
import type { Category, Product } from '@/types'

// 响应式数据
const categories = ref<Category[]>([])
const recommendProducts = ref<Product[]>([])
const nearExpiryProducts = ref<Product[]>([])
const loading = ref(false)
const categoriesLoading = ref(false)
const activeCategoryId = ref<string | null>(null)

// 获取分类
async function fetchCategories() {
    categoriesLoading.value = true
    try {
        const res = await categoryApi.getCategories()
        categories.value = res
        if (res.length > 0 && !activeCategoryId.value) {
            activeCategoryId.value = res[0].id
        }
    } catch (e) {
        console.error('获取分类失败:', e)
    } finally {
        categoriesLoading.value = false
    }
}

// 获取推荐商品（首页展示前 8 个）
async function fetchRecommendProducts() {
    try {
        const res = await productApi.getProducts({ limit: 8 })
        recommendProducts.value = res
    } catch (e) {
        console.error('获取推荐商品失败:', e)
    }
}

// 获取临期特价（按价格升序取前 4 个，近似临期）
async function fetchNearExpiryProducts() {
    try {
        const res = await productApi.getProducts({ limit: 4, min_price: 0, max_price: 5000 }) // 50元以下近似临期
        nearExpiryProducts.value = res
    } catch (e) {
        console.error('获取临期特价失败:', e)
    }
}

// 下拉刷新
async function onRefresh() {
    await Promise.all([fetchCategories(), fetchRecommendProducts(), fetchNearExpiryProducts()])
    uni.stopPullDownRefresh()
}

// 分类点击
function onCategoryClick(cat: Category) {
    activeCategoryId.value = cat.id
    uni.switchTab({ url: '/pages/category/category' })
    // 可通过 storage 传递选中分类
    uni.setStorageSync('selected_category_id', cat.id)
}

// 商品点击
function onProductClick(product: Product) {
    uni.navigateTo({ url: `/pages/product/detail?id=${product.id}` })
}

// 去分类页查看更多
function goCategory() {
    uni.switchTab({ url: '/pages/category/category' })
}

// 去搜索页
function goSearch() {
    uni.navigateTo({ url: '/pages/search/search' })
}

onMounted(() => {
    onRefresh()
})

onPullDownRefresh(() => {
    onRefresh()
})
</script>

<template>
    <scroll-view class="page" scroll-y :style="{ height: '100vh' }" refresher-enabled :refresher-triggered="loading">
        <!-- 搜索栏 -->
        <view class="search-bar" @tap="goSearch">
            <text class="search-icon">🔍</text>
            <text class="search-placeholder">搜索水果、蔬菜、生鲜...</text>
        </view>

        <!-- 轮播图占位 -->
        <view class="banner">
            <image class="banner-image" src="/static/images/banner-placeholder.png" mode="aspectFill" />
        </view>

        <!-- 分类导航 -->
        <view class="section">
            <view class="section-header">
                <text class="section-title">分类</text>
                <text class="section-more" @tap="goCategory">更多 ></text>
            </view>
            <view class="category-grid" v-if="!categoriesLoading">
                <view
                    class="category-item"
                    :class="{ active: activeCategoryId === cat.id }"
                    v-for="cat in categories"
                    :key="cat.id"
                    @tap="onCategoryClick(cat)"
                >
                    <text class="category-name">{{ cat.name }}</text>
                    <text class="category-count">{{ cat.count }} 个商品</text>
                </view>
            </view>
            <view class="loading-placeholder" v-else>
                <view class="category-skeleton" v-for="i in 6" :key="i" />
            </view>
        </view>

        <!-- 推荐商品 -->
        <view class="section">
            <view class="section-header">
                <text class="section-title">为你推荐</text>
                <text class="section-more" @tap="goCategory">查看更多 ></text>
            </view>
            <view class="product-grid" v-if="recommendProducts.length">
                <view class="product-card" v-for="product in recommendProducts" :key="product.id" @tap="onProductClick(product)">
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
            <view class="product-skeleton-grid" v-else>
                <view class="product-skeleton" v-for="i in 4" :key="i" />
            </view>
        </view>

        <!-- 临期特价 -->
        <view class="section" v-if="nearExpiryProducts.length">
            <view class="section-header">
                <view class="section-title-wrapper">
                    <view class="tag tag-danger">临期特价</view>
                    <text class="section-title">今日特惠</text>
                </view>
            </view>
            <view class="product-list">
                <view class="product-list-item" v-for="product in nearExpiryProducts" :key="product.id" @tap="onProductClick(product)">
                    <image class="product-list-image" :src="getProductMainImage(product)" mode="aspectFill" />
                    <view class="product-list-info">
                        <text class="product-title" style="-webkit-line-clamp: 1; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ product.title }}</text>
                        <view class="product-price-row">
                            <text class="price">{{ formatPriceWithSymbol(product.min_price_cents) }}</text>
                            <text class="price-unit">/ 份</text>
                        </view>
                    </view>
                </view>
            </view>
        </view>

        <!-- 底部安全区 -->
        <view class="safe-area-bottom" />
    </scroll-view>
</template>

<style scoped>
.page {
    background: #F5F5F5;
}

.search-bar {
    display: flex;
    align-items: center;
    padding: 12px 16px;
    background: #FFFFFF;
    position: sticky;
    top: 0;
    z-index: 10;
}

.search-icon {
    font-size: 18px;
    margin-right: 8px;
}

.search-placeholder {
    flex: 1;
    color: #9E9E9E;
    font-size: 15px;
}

.banner {
    height: 160px;
    margin: 8px 16px;
    border-radius: 12px;
    overflow: hidden;
}

.banner-image {
    width: 100%;
    height: 100%;
}

.section {
    background: #FFFFFF;
    margin-bottom: 8px;
}

.section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px;
}

.section-title-wrapper {
    display: flex;
    align-items: center;
    gap: 8px;
}

.section-title {
    font-size: 17px;
    font-weight: 600;
    color: #1A1A1A;
}

.section-more {
    font-size: 13px;
    color: #9E9E9E;
}

/* 分类网格 */
.category-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 8px;
    padding: 0 16px 16px;
}

.category-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 12px 8px;
    border-radius: 8px;
    background: #F5F5F5;
}

.category-item.active {
    background: #E8F5E9;
}

.category-name {
    font-size: 13px;
    font-weight: 500;
    color: #1A1A1A;
}

.category-count {
    font-size: 11px;
    color: #9E9E9E;
    margin-top: 4px;
}

.category-skeleton {
    height: 60px;
    background: linear-gradient(90deg, #F5F5F5 25%, #EEEEEE 50%, #F5F5F5 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
    border-radius: 8px;
}

@keyframes shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
}

/* 商品网格 */
.product-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    padding: 0 16px 16px;
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

.product-skeleton-grid {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    padding: 0 16px 16px;
}

.product-skeleton {
    background: #FFFFFF;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.product-skeleton::before {
    content: '';
    display: block;
    width: 100%;
    padding-top: 100%;
    background: linear-gradient(90deg, #F5F5F5 25%, #EEEEEE 50%, #F5F5F5 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
}

.product-skeleton::after {
    content: '';
    display: block;
    padding: 12px;
    height: 40px;
    background: linear-gradient(90deg, #F5F5F5 25%, #EEEEEE 50%, #F5F5F5 75%);
    background-size: 200% 100%;
    animation: shimmer 1.5s infinite;
}

/* 临期特价列表 */
.product-list {
    padding: 0 16px 16px;
}

.product-list-item {
    display: flex;
    align-items: center;
    padding: 12px;
    background: #FFFFFF;
    border-radius: 12px;
    margin-bottom: 8px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
}

.product-list-image {
    width: 80px;
    height: 80px;
    border-radius: 8px;
    object-fit: cover;
    margin-right: 12px;
}

.product-list-info {
    flex: 1;
    min-width: 0;
}

.tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
}

.tag-danger {
    background: #FBE9E7;
    color: #C62828;
}

.safe-area-bottom {
    height: 34px;
}
</style>
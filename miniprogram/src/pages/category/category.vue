<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { categoryApi, productApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage } from '@/utils/helpers'
import type { Category, Product } from '@/types'

const categories = ref<Category[]>([])
const products = ref<Product[]>([])
const activeCategoryId = ref<string | null>(null)
const loading = ref(false)
const categoriesLoading = ref(false)
const hasMore = ref(true)
const page = ref(0)
const pageSize = 20
const selectedCategoryIdFromStorage = uni.getStorageSync('selected_category_id') as string | null

async function fetchCategories() {
    categoriesLoading.value = true
    try {
        const res = await categoryApi.getCategories()
        categories.value = res
        if (res.length > 0) {
            const targetId = selectedCategoryIdFromStorage || res[0].id
            if (res.some(c => c.id === targetId)) {
                activeCategoryId.value = targetId
            } else {
                activeCategoryId.value = res[0].id
            }
        }
    } catch (e) {
        console.error('获取分类失败:', e)
    } finally {
        categoriesLoading.value = false
    }
}

async function fetchProducts(categoryId: string, reset = false) {
    if (loading.value) return
    if (reset) {
        page.value = 0
        products.value = []
        hasMore.value = true
    }
    if (!hasMore.value) return

    loading.value = true
    try {
        const res = await productApi.getProducts({
            category_id: categoryId,
            limit: pageSize,
            offset: page.value * pageSize
        })
        if (res.length < pageSize) {
            hasMore.value = false
        }
        products.value = [...products.value, ...res]
        page.value++
    } catch (e) {
        console.error('获取商品失败:', e)
        hasMore.value = false
    } finally {
        loading.value = false
    }
}

function onCategoryClick(cat: Category) {
    activeCategoryId.value = cat.id
    uni.setStorageSync('selected_category_id', cat.id)
    fetchProducts(cat.id, true)
}

async function onRefresh() {
    await fetchCategories()
    if (activeCategoryId.value) {
        await fetchProducts(activeCategoryId.value, true)
    }
    uni.stopPullDownRefresh()
}

async function onLoadMore() {
    if (activeCategoryId.value && !loading.value && hasMore.value) {
        await fetchProducts(activeCategoryId.value, false)
    }
}

onMounted(async () => {
    await fetchCategories()
    if (activeCategoryId.value) {
        await fetchProducts(activeCategoryId.value, true)
    }
})

onShow(() => {
    // 从首页跳转过来时可能有新的选中分类
    const stored = uni.getStorageSync('selected_category_id') as string | null
    if (stored && stored !== activeCategoryId.value && categories.value.some(c => c.id === stored)) {
        activeCategoryId.value = stored
        fetchProducts(stored, true)
    }
})

function onProductClick(product: any) {
    uni.navigateTo({ url: `/pages/product/detail?id=${product.id}` })
}
</script>

<template>
    <view class="page">
        <!-- 左侧分类列表 -->
        <view class="category-sidebar" :class="{ loading: categoriesLoading }">
            <view
                class="category-item"
                :class="{ active: activeCategoryId === cat.id }"
                v-for="cat in categories"
                :key="cat.id"
                @tap="onCategoryClick(cat)"
            >
                <text class="category-name">{{ cat.name }}</text>
                <text class="category-count">{{ cat.count }}</text>
            </view>
            <view class="category-skeleton" v-for="i in 10" :key="i" v-if="categoriesLoading" />
        </view>

        <!-- 右侧商品列表 -->
        <view class="product-list-wrapper">
            <scroll-view
                class="product-list-scroll"
                scroll-y
                :style="{ height: '100vh' }"
                refresher-enabled
                :refresher-triggered="loading"
                @refresherrefresh="onRefresh"
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
                <view class="empty-state" v-else-if="!loading && activeCategoryId">
                    <image class="empty-icon" src="/static/images/empty-category.png" mode="aspectFit" />
                    <text class="empty-text">该分类暂无商品</text>
                </view>
                <view class="loading-container" v-else-if="loading && !products.length">
                    <view class="product-skeleton" v-for="i in 6" :key="i" />
                </view>

                <!-- 加载更多提示 -->
                <view class="load-more" v-if="!hasMore && products.length">
                    <text class="load-more-text">已加载全部商品</text>
                </view>
                <view class="load-more" v-if="loading && products.length">
                    <uni-loading-icon />
                    <text class="load-more-text">加载中...</text>
                </view>
            </scroll-view>
        </view>
    </view>
</template>

<style scoped>
.page {
    display: flex;
    height: 100vh;
    background: #F5F5F5;
}

.category-sidebar {
    width: 100px;
    min-width: 100px;
    background: #FFFFFF;
    border-right: 1px solid #EEEEEE;
    overflow-y: auto;
}

.category-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 16px 8px;
    border-bottom: 1px solid #F5F5F5;
}

.category-item.active {
    background: #E8F5E9;
    border-left: 3px solid #2E7D32;
}

.category-name {
    font-size: 14px;
    font-weight: 500;
    color: #1A1A1A;
    text-align: center;
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
    border-bottom: 1px solid #F5F5F5;
}

@keyframes shimmer {
    0% { background-position: 200% 0; }
    100% { background-position: -200% 0; }
}

.product-list-wrapper {
    flex: 1;
    overflow: hidden;
}

.product-list-scroll {
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
    font-size: 15px;
}

.loading-container {
    display: grid;
    grid-template-columns: repeat(2, 1fr);
    gap: 8px;
    padding: 8px 16px;
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
</style>
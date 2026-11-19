<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { productApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage } from '@/utils/helpers'
import type { Product } from '@/types'

const products = ref<Product[]>([])
const loading = ref(false)
const hasMore = ref(true)
const page = ref(0)
const pageSize = 20
const showFilter = ref(false)

const filterOptions = {
  sortBy: 'discount' as 'discount' | 'time' | 'price',
  minDiscount: 0,
  maxPrice: 0,
}

async function fetchProducts(reset = false) {
  if (reset) {
    page.value = 0
    products.value = []
    hasMore.value = true
  }
  if (!hasMore.value) return

  loading.value = true
  try {
    const res = await productApi.getProducts({
      limit: pageSize,
      offset: page.value * pageSize,
    })
    // 筛选有 discount_label 的商品（临期特价）
    const expiryProducts = res.filter(p => p.min_price_cents !== null && (p as any).discount_label)
    if (expiryProducts.length < pageSize) {
      hasMore.value = false
    }
    products.value = [...products.value, ...expiryProducts]
    page.value++
  } catch (e) {
    console.error('获取临期特价失败:', e)
    hasMore.value = false
  } finally {
    loading.value = false
  }
}

function onProductClick(product: Product) {
  if (product.variants && product.variants.length > 0) {
    const variant = product.variants[0]
    uni.navigateTo({ url: `/pages/product/detail?variant_id=${variant.id}` })
  } else if (product.id) {
    uni.navigateTo({ url: `/pages/product/detail?id=${product.id}` })
  }
}

function formatDiscountLabel(product: Product): string {
  return (product as any).discount_label || ''
}

function getDiscountPercent(product: Product): number {
  const original = (product as any).list_price_cents
  const current = product.min_price_cents
  if (original && current && original > current) {
    return Math.round((1 - current / original) * 100)
  }
  return 0
}

onMounted(() => {
  fetchProducts(true)
})
</script>

<template>
  <view class="page">
    <!-- 顶部筛选栏 -->
    <view class="filter-bar">
      <view class="filter-item" @tap="showFilter = !showFilter">
        <text class="filter-icon">⚙️</text>
        <text class="filter-text">{{ showFilter ? '收起筛选' : '筛选' }}</text>
      </view>
      <view class="filter-item" v-if="!showFilter">
        <text class="filter-icon">🔥</text>
        <text class="filter-text">{{ products.length }} 个临期特价</text>
      </view>
    </view>

    <!-- 筛选面板 -->
    <view class="filter-panel" v-if="showFilter">
      <view class="filter-section">
        <text class="filter-section-title">排序方式</text>
        <view class="filter-options">
          <view
            class="filter-option"
            :class="{ active: filterOptions.sortBy === 'discount' }"
            @tap="filterOptions.sortBy = 'discount'; fetchProducts(true)"
          >
            折扣力度
          </view>
          <view
            class="filter-option"
            :class="{ active: filterOptions.sortBy === 'time' }"
            @tap="filterOptions.sortBy = 'time'; fetchProducts(true)"
          >
            剩余时间
          </view>
          <view
            class="filter-option"
            :class="{ active: filterOptions.sortBy === 'price' }"
            @tap="filterOptions.sortBy = 'price'; fetchProducts(true)"
          >
            价格从低到高
          </view>
        </view>
      </view>
    </view>

    <!-- 商品列表 -->
    <scroll-view
      class="product-scroll"
      scroll-y
      :style="{ height: showFilter ? 'calc(100vh - 160px)' : 'calc(100vh - 100px)' }"
      @scrolltolower="fetchProducts"
    >
      <view class="product-grid" v-if="products.length > 0">
        <view
          class="product-card"
          v-for="product in products"
          :key="product.id"
          @tap="onProductClick(product)"
        >
          <view class="product-image-wrapper">
            <image class="product-image" :src="getProductMainImage(product)" mode="aspectFill" />
            <view class="discount-badge" v-if="getDiscountPercent(product) > 0">
              {{ getDiscountPercent(product) }}折
            </view>
            <view class="expiry-tag" v-if="formatDiscountLabel(product)">
              {{ formatDiscountLabel(product) }}
            </view>
          </view>
          <view class="product-info">
            <text class="product-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ product.title }}</text>
            <view class="product-price-row">
              <text class="price">{{ formatPriceWithSymbol(product.min_price_cents) }}</text>
              <text class="original-price" v-if="(product as any).list_price_cents">
                {{ formatPriceWithSymbol((product as any).list_price_cents) }}
              </text>
            </view>
            <view class="product-stock" v-if="product.total_stock > 0">
              剩余 {{ product.total_stock }} 份
            </view>
          </view>
        </view>
      </view>

      <view class="empty-state" v-else-if="!loading && products.length === 0">
        <image class="empty-icon" src="/static/images/empty-near-expiry.png" mode="aspectFit" />
        <text class="empty-text">暂无临期特价商品</text>
        <text class="empty-subtext">每天20:00后更新晚间清仓</text>
      </view>

      <view class="load-more" v-if="!hasMore && products.length > 0">
        <text class="load-more-text">已加载全部商品</text>
      </view>
      <view class="load-more" v-if="loading && products.length > 0">
        <uni-loading-icon />
        <text class="load-more-text">加载中...</text>
      </view>
    </scroll-view>

    <view class="loading-container" v-if="loading && products.length === 0">
      <uni-loading-icon />
      <text>加载中...</text>
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

.filter-bar {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: #FFFFFF;
  border-bottom: 1px solid #EEEEEE;
}

.filter-item {
  display: flex;
  align-items: center;
  gap: 6px;
}

.filter-icon {
  font-size: 16px;
}

.filter-text {
  font-size: 14px;
  color: #2E7D32;
  font-weight: 500;
}

.filter-panel {
  background: #FFFFFF;
  border-bottom: 1px solid #EEEEEE;
  padding: 12px 16px;
  animation: slideDown 0.3s ease;
}

@keyframes slideDown {
  from { opacity: 0; transform: translateY(-10px); }
  to { opacity: 1; transform: translateY(0); }
}

.filter-section {
  margin-bottom: 12px;
}

.filter-section-title {
  display: block;
  font-size: 13px;
  color: #9E9E9E;
  margin-bottom: 8px;
}

.filter-options {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.filter-option {
  padding: 6px 12px;
  background: #F5F5F5;
  border-radius: 16px;
  font-size: 12px;
  color: #616161;
}

.filter-option.active {
  background: #2E7D32;
  color: #FFFFFF;
}

.product-scroll {
  flex: 1;
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

.product-image-wrapper {
  position: relative;
  aspect-ratio: 1 / 1;
  overflow: hidden;
}

.product-image {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.discount-badge {
  position: absolute;
  top: 8px;
  left: 8px;
  background: linear-gradient(135deg, #F44336, #E53935);
  color: #FFFFFF;
  padding: 2px 8px;
  border-radius: 12px;
  font-size: 11px;
  font-weight: 600;
}

.expiry-tag {
  position: absolute;
  bottom: 8px;
  left: 8px;
  background: rgba(0, 0, 0, 0.7);
  color: #FFFFFF;
  padding: 2px 8px;
  border-radius: 4px;
  font-size: 10px;
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
  gap: 8px;
  margin-bottom: 4px;
}

.price {
  font-size: 16px;
  font-weight: 600;
  color: #2E7D32;
}

.original-price {
  font-size: 12px;
  color: #9E9E9E;
  text-decoration: line-through;
}

.product-stock {
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

.loading-container {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100vh;
  gap: 16px;
  color: #9E9E9E;
}
</style>
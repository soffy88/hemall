<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { onShow, onReachBottom } from '@dcloudio/uni-app'
import { personalizationApi } from '@/utils/api'
import { formatPriceWithSymbol, formatDuration } from '@/utils/helpers'
import type { Recipe } from '@/types'

const recipes = ref<Recipe[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const hasMore = ref(true)
const limit = 20
let offset = 0

const total = ref(0)

async function fetchRecipes(append = false) {
    if (append) {
        loadingMore.value = true
    } else {
        loading.value = true
    }
    try {
        const res = await personalizationApi.getRecipes(limit, append ? offset : 0)
        if (append) {
            recipes.value = [...recipes.value, ...res.items]
        } else {
            recipes.value = res.items
            offset = 0
        }
        total.value = res.total
        hasMore.value = recipes.value.length < res.total
        if (append) {
            offset += limit
        } else {
            offset = limit
        }
    } catch (e: any) {
        uni.showToast({ title: e.message || '加载失败', icon: 'none' })
    } finally {
        loading.value = false
        loadingMore.value = false
    }
}

function goDetail(recipeId: string) {
    uni.navigateTo({ url: `/pages/personalization/recipe-detail?recipe_id=${recipeId}` })
}

function formatDifficulty(difficulty: string): { text: string; class: string } {
    switch (difficulty) {
        case 'easy':
            return { text: '简单', class: 'diff-easy' }
        case 'medium':
            return { text: '中等', class: 'diff-medium' }
        case 'hard':
            return { text: '困难', class: 'diff-hard' }
        default:
            return { text: difficulty, class: '' }
    }
}

function formatTags(tags: string[]): string {
    return tags.slice(0, 3).join(' · ')
}

onMounted(() => {
    fetchRecipes()
})

onShow(() => {
    // 不自动刷新，保持列表位置
})

onReachBottom(() => {
    if (hasMore.value && !loadingMore.value) {
        fetchRecipes(true)
    }
})
</script>

<template>
    <view class="page">
        <scroll-view
            class="recipe-scroll"
            scroll-y
            :style="{ height: 'calc(100vh - 98px)' }"
            refresher-enabled
            :refresher-triggered="loading"
            @refresherrefresh="fetchRecipes"
        >
            <!-- 列表 -->
            <view class="recipe-list" v-if="recipes.length > 0">
                <view
                    class="recipe-card"
                    v-for="recipe in recipes"
                    :key="recipe.id"
                    @tap="goDetail(recipe.id)"
                >
                    <view class="card-image-wrapper">
                        <image
                            class="card-image"
                            :src="recipe.cover_image_url || '/static/images/placeholder-recipe.png'"
                            mode="aspectFill"
                        />
                        <view class="card-servings">{{ recipe.servings }} 人份</view>
                    </view>
                    <view class="card-content">
                        <text class="card-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ recipe.title }}</text>
                        <text class="card-desc" v-if="recipe.description" style="-webkit-line-clamp: 1; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ recipe.description }}</text>
                        <view class="card-meta">
                            <view class="meta-item">
                                <text class="meta-icon">⏱️</text>
                                <text class="meta-text">{{ formatDuration(recipe.prep_time_minutes + recipe.cook_time_minutes) }}</text>
                            </view>
                            <view class="meta-item">
                                <text class="meta-icon">👨‍🍳</text>
                                <text class="meta-text" :class="formatDifficulty(recipe.difficulty).class">{{ formatDifficulty(recipe.difficulty).text }}</text>
                            </view>
                        </view>
                        <view class="card-tags" v-if="recipe.tags && recipe.tags.length > 0">
                            <text class="tag" v-for="tag in recipe.tags.slice(0, 3)" :key="tag">{{ tag }}</text>
                        </view>
                    </view>
                </view>
            </view>

            <!-- 空状态 -->
            <view class="empty-state" v-if="!loading && recipes.length === 0">
                <image class="empty-icon" src="/static/images/empty-recipe.png" mode="aspectFit" />
                <text class="empty-title">暂无菜谱</text>
                <text class="empty-desc">敬请期待更多美味菜谱推荐</text>
            </view>

            <!-- 加载更多 -->
            <view class="load-more" v-if="hasMore && recipes.length > 0">
                <view class="spinner" v-if="loadingMore" />
                <text v-else class="load-more-text">上拉加载更多</text>
            </view>

            <!-- 已全部加载 -->
            <view class="load-end" v-if="!hasMore && recipes.length > 0">
                <text>已全部加载完毕</text>
            </view>

            <view class="bottom-spacer" />
        </scroll-view>

        <view class="loading-overlay" v-if="loading && recipes.length === 0">
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

.recipe-scroll {
    flex: 1;
}

.recipe-list {
    padding: 8px 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.recipe-card {
    background: #FFFFFF;
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.card-image-wrapper {
    position: relative;
    width: 100%;
    height: 160px;
    overflow: hidden;
}

.card-image {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

.card-servings {
    position: absolute;
    bottom: 8px;
    right: 8px;
    background: rgba(0, 0, 0, 0.6);
    color: #FFFFFF;
    font-size: 11px;
    padding: 2px 8px;
    border-radius: 10px;
}

.card-content {
    padding: 12px;
}

.card-title {
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
    line-height: 1.3;
    margin-bottom: 6px;
    display: block;
}

.card-desc {
    font-size: 13px;
    color: #9E9E9E;
    line-height: 1.4;
    margin-bottom: 8px;
    display: block;
}

.card-meta {
    display: flex;
    gap: 16px;
    margin-bottom: 8px;
}

.meta-item {
    display: flex;
    align-items: center;
    gap: 4px;
}

.meta-icon {
    font-size: 12px;
}

.meta-text {
    font-size: 12px;
    color: #616161;
}

.meta-text.diff-easy {
    color: #2E7D32;
}

.meta-text.diff-medium {
    color: #F57C00;
}

.meta-text.diff-hard {
    color: #C62828;
}

.card-tags {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}

.tag {
    font-size: 11px;
    background: #F5F5F5;
    color: #616161;
    padding: 2px 8px;
    border-radius: 10px;
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
}

.load-more {
    padding: 24px 16px;
    text-align: center;
}

.load-more-text {
    font-size: 13px;
    color: #9E9E9E;
}

.spinner {
    width: 24px;
    height: 24px;
    border: 2px solid #E0E0E0;
    border-top-color: #2E7D32;
    border-radius: 50%;
    animation: spin 1s linear infinite;
    margin: 0 auto;
}

.load-end {
    padding: 24px 16px;
    text-align: center;
    color: #9E9E9E;
    font-size: 13px;
    border-top: 1px solid #EEEEEE;
    margin-top: 12px;
}

.bottom-spacer {
    height: 40px;
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

@keyframes spin {
    to { transform: rotate(360deg); }
}
</style>
<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { personalizationApi, cartApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { Recipe, RecipeIngredient, RecipeStep } from '@/types'

const recipe = ref<Recipe | null>(null)
const loading = ref(false)
const addingToCart = ref(false)
const servings = ref(1)
const maxServings = 20
const minServings = 1

async function fetchRecipe() {
    const pages = getCurrentPages()
    const currentPage = pages[pages.length - 1]
    const recipeId = currentPage.options?.recipe_id
    if (!recipeId) {
        uni.showToast({ title: '参数错误', icon: 'none' })
        uni.navigateBack()
        return
    }
    loading.value = true
    try {
        const res = await personalizationApi.getRecipe(recipeId)
        recipe.value = res
    } catch (e: any) {
        uni.showToast({ title: e.message || '加载失败', icon: 'none' })
        setTimeout(() => uni.navigateBack(), 1500)
    } finally {
        loading.value = false
    }
}

function increaseServings() {
    if (servings.value < maxServings) {
        servings.value++
    }
}

function decreaseServings() {
    if (servings.value > minServings) {
        servings.value--
    }
}

async function addRecipeToCart() {
    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login?redirect=personalization_recipe_detail' })
        return
    }

    if (!recipe.value) return

    addingToCart.value = true
    try {
        const res = await personalizationApi.addRecipeToCart(recipe.value.id, servings.value)
        uni.showToast({ title: res.message || `已加入 ${res.added} 件食材`, icon: 'success' })
        setTimeout(() => {
            uni.switchTab({ url: '/pages/cart/cart' })
        }, 1500)
    } catch (e: any) {
        uni.showToast({ title: e.message || '加入失败', icon: 'none' })
    } finally {
        addingToCart.value = false
    }
}

function goProduct(variantId: string) {
    uni.navigateTo({ url: `/pages/product/detail?variant_id=${variantId}` })
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

function formatDuration(totalMinutes: number): string {
    if (totalMinutes < 60) {
        return `${totalMinutes} 分钟`
    }
    const hours = Math.floor(totalMinutes / 60)
    const minutes = totalMinutes % 60
    return minutes > 0 ? `${hours} 小时 ${minutes} 分钟` : `${hours} 小时`
}

onMounted(() => {
    fetchRecipe()
})
</script>

<template>
    <view class="page">
        <scroll-view
            class="detail-scroll"
            scroll-y
            :style="{ height: 'calc(100vh - 98px)' }"
            v-if="recipe"
        >
            <!-- 封面图 -->
            <view class="cover-section">
                <image
                    class="cover-image"
                    :src="recipe.cover_image_url || '/static/images/placeholder-recipe.png'"
                    mode="aspectFill"
                />
                <view class="cover-overlay">
                    <view class="cover-info">
                        <text class="cover-title">{{ recipe.title }}</text>
                        <view class="cover-meta">
                            <view class="meta-item">
                                <text class="meta-icon">👥</text>
                                <text class="meta-text">{{ recipe.servings }} 人份</text>
                            </view>
                            <view class="meta-item">
                                <text class="meta-icon">⏱️</text>
                                <text class="meta-text">{{ formatDuration(recipe.prep_time_minutes + recipe.cook_time_minutes) }}</text>
                            </view>
                            <view class="meta-item">
                                <text class="meta-icon">👨‍🍳</text>
                                <text class="meta-text" :class="formatDifficulty(recipe.difficulty).class">{{ formatDifficulty(recipe.difficulty).text }}</text>
                            </view>
                        </view>
                    </view>
                </view>
            </view>

            <!-- 描述 -->
            <view class="section" v-if="recipe.description">
                <text class="section-title">菜品介绍</text>
                <text class="description-text">{{ recipe.description }}</text>
            </view>

            <!-- 标签 -->
            <view class="section" v-if="recipe.tags && recipe.tags.length > 0">
                <text class="section-title">标签</text>
                <view class="tags-wrap">
                    <text class="tag" v-for="tag in recipe.tags" :key="tag">{{ tag }}</text>
                </view>
            </view>

            <!-- 食材清单 -->
            <view class="section">
                <view class="section-header">
                    <text class="section-title">食材清单</text>
                    <text class="ingredients-count">共 {{ recipe.ingredients.length }} 种食材</text>
                </view>
                <view class="ingredients-list">
                    <view
                        class="ingredient-item"
                        v-for="ingredient in recipe.ingredients"
                        :key="ingredient.variant_id"
                        @tap="goProduct(ingredient.variant_id)"
                    >
                        <view class="ingredient-main">
                            <image
                                class="ingredient-image"
                                :src="getProductMainImage({ variants: [{ batches: [{ id: '' }] }] })"
                                mode="aspectFill"
                            />
                            <view class="ingredient-info">
                                <text class="ingredient-name">{{ ingredient.product_title }}</text>
                                <view class="ingredient-qty">
                                    <text class="qty-value">{{ ingredient.quantity * servings }} {{ ingredient.unit }}</text>
                                    <text class="qty-base" v-if="servings > 1">(原量: {{ ingredient.quantity }} {{ ingredient.unit }})</text>
                                </view>
                            </view>
                        </view>
                        <view class="ingredient-note" v-if="ingredient.note">
                            <text class="note-icon">💡</text>
                            <text class="note-text">{{ ingredient.note }}</text>
                        </view>
                    </view>
                </view>
            </view>

            <!-- 制作步骤 -->
            <view class="section">
                <text class="section-title">制作步骤</text>
                <view class="steps-list">
                    <view
                        class="step-item"
                        v-for="step in recipe.steps"
                        :key="step.step_number"
                    >
                        <view class="step-number">{{ step.step_number }}</view>
                        <view class="step-content">
                            <text class="step-text">{{ step.instruction }}</text>
                            <image
                                class="step-image"
                                :src="step.image_url"
                                mode="aspectFill"
                                v-if="step.image_url"
                            />
                        </view>
                    </view>
                </view>
            </view>

            <!-- 底部加购栏 -->
            <view class="bottom-spacer" />
        </scroll-view>

        <!-- 底部固定操作栏 -->
        <view class="bottom-bar" v-if="recipe">
            <view class="servings-selector">
                <text class="servings-label">份数</text>
                <view class="servings-control">
                    <button class="btn-minus" @tap="decreaseServings" :disabled="servings <= minServings">-</button>
                    <text class="servings-value">{{ servings }}</text>
                    <button class="btn-plus" @tap="increaseServings" :disabled="servings >= maxServings">+</button>
                </view>
            </view>
            <button class="btn-add-cart" @tap="addRecipeToCart" :disabled="addingToCart">
                <text v-if="!addingToCart">按份数加购食材</text>
                <text v-else>加入中...</text>
            </button>
        </view>

        <view class="loading-overlay" v-if="loading && !recipe">
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

.detail-scroll {
    flex: 1;
}

.cover-section {
    position: relative;
    width: 100%;
    height: 220px;
    overflow: hidden;
}

.cover-image {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

.cover-overlay {
    position: absolute;
    bottom: 0;
    left: 0;
    right: 0;
    background: linear-gradient(transparent, rgba(0, 0, 0, 0.8));
    padding: 16px;
}

.cover-info {
    color: #FFFFFF;
}

.cover-title {
    font-size: 22px;
    font-weight: 600;
    margin-bottom: 12px;
    display: block;
}

.cover-meta {
    display: flex;
    gap: 20px;
}

.meta-item {
    display: flex;
    align-items: center;
    gap: 6px;
}

.meta-icon {
    font-size: 14px;
}

.meta-text {
    font-size: 13px;
}

.meta-text.diff-easy {
    color: #A5D6A7;
}

.meta-text.diff-medium {
    color: #FFB74D;
}

.meta-text.diff-hard {
    color: #EF9A9A;
}

.section {
    background: #FFFFFF;
    margin-top: 12px;
    padding: 16px;
}

.section-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
}

.section-title {
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
}

.ingredients-count {
    font-size: 13px;
    color: #9E9E9E;
}

.description-text {
    font-size: 14px;
    color: #616161;
    line-height: 1.6;
    display: block;
}

.tags-wrap {
    display: flex;
    flex-wrap: wrap;
    gap: 8px;
}

.tag {
    font-size: 12px;
    background: #F5F5F5;
    color: #616161;
    padding: 4px 12px;
    border-radius: 12px;
}

.ingredients-list {
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.ingredient-item {
    display: flex;
    flex-direction: column;
    gap: 8px;
    padding-bottom: 12px;
    border-bottom: 1px solid #F5F5F5;
}

.ingredient-item:last-child {
    border-bottom: none;
    padding-bottom: 0;
}

.ingredient-main {
    display: flex;
    gap: 12px;
}

.ingredient-image {
    width: 60px;
    height: 60px;
    border-radius: 8px;
    object-fit: cover;
    flex-shrink: 0;
    background: #F5F5F5;
}

.ingredient-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
}

.ingredient-name {
    font-size: 14px;
    color: #1A1A1A;
    line-height: 1.3;
}

.ingredient-qty {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.qty-value {
    font-size: 14px;
    font-weight: 600;
    color: #2E7D32;
}

.qty-base {
    font-size: 11px;
    color: #9E9E9E;
}

.ingredient-note {
    display: flex;
    align-items: flex-start;
    gap: 6px;
    padding-left: 72px;
}

.note-icon {
    font-size: 11px;
    margin-top: 1px;
}

.note-text {
    font-size: 12px;
    color: #9E9E9E;
    line-height: 1.4;
}

.steps-list {
    display: flex;
    flex-direction: column;
    gap: 16px;
}

.step-item {
    display: flex;
    gap: 12px;
}

.step-number {
    width: 28px;
    height: 28px;
    background: #2E7D32;
    color: #FFFFFF;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 13px;
    font-weight: 600;
    flex-shrink: 0;
    margin-top: 2px;
}

.step-content {
    flex: 1;
    min-width: 0;
}

.step-text {
    font-size: 14px;
    color: #1A1A1A;
    line-height: 1.6;
    display: block;
    margin-bottom: 8px;
}

.step-image {
    width: 100%;
    border-radius: 8px;
    object-fit: cover;
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
    gap: 12px;
    z-index: 50;
    padding-bottom: calc(12px + env(safe-area-inset-bottom, 34px));
}

.servings-selector {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 0 12px;
}

.servings-label {
    font-size: 14px;
    color: #616161;
}

.servings-control {
    display: flex;
    align-items: center;
    gap: 0;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    overflow: hidden;
}

.btn-minus,
.btn-plus {
    width: 36px;
    height: 36px;
    background: #F5F5F5;
    border: none;
    font-size: 18px;
    color: #1A1A1A;
    display: flex;
    align-items: center;
    justify-content: center;
}

.btn-minus:disabled,
.btn-plus:disabled {
    color: #BDBDBD;
    background: #FAFAFA;
}

.servings-value {
    min-width: 40px;
    text-align: center;
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
}

.btn-add-cart {
    flex: 1;
    height: 48px;
    background: linear-gradient(135deg, #2E7D32, #4CAF50);
    color: #FFFFFF;
    border: none;
    border-radius: 24px;
    font-size: 16px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 4px 12px rgba(46, 125, 50, 0.3);
}

.btn-add-cart:disabled {
    background: #A5D6A7;
    box-shadow: none;
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
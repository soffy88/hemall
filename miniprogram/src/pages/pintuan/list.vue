<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { pintuanApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage, formatTimeRemaining } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { PintuanGroup } from '@/types'

const groups = ref<PintuanGroup[]>([])
const loading = ref(false)
const hasMore = ref(true)
const page = ref(0)
const pageSize = 20
const activeTab = ref<'all' | 'open' | 'formed'>('all')

const statusLabels: Record<string, string> = {
    open: '拼单中',
    formed: '已成团',
    expired: '已过期'
}

const statusColors: Record<string, string> = {
    open: '#2E7D32',
    formed: '#1976D2',
    expired: '#9E9E9E'
}

function getStatusLabel(status: string): string {
    return statusLabels[status] || status
}

function getStatusColor(status: string): string {
    return statusColors[status] || '#9E9E9E'
}

function formatProgress(current: number, target: number): string {
    return `${current}/${target}`
}

function getProgressPercent(current: number, target: number): number {
    return Math.min(100, Math.round((current / target) * 100))
}

async function fetchGroups(append = false) {
    if (loading.value || (!hasMore.value && !append)) return

    loading.value = true
    try {
        if (!append) {
            page.value = 0
            groups.value = []
        }
        const status = activeTab.value === 'all' ? undefined : activeTab.value
        const res = await pintuanApi.getGroups(status, pageSize, page.value * pageSize)
        const newGroups = res.items || []
        if (append) {
            groups.value = [...groups.value, ...newGroups]
        } else {
            groups.value = newGroups
        }
        hasMore.value = newGroups.length === pageSize
        page.value += 1
    } catch (e: any) {
        uni.showToast({ title: e.message || '加载失败', icon: 'none' })
    } finally {
        loading.value = false
    }
}

function onTabChange(tab: 'all' | 'open' | 'formed') {
    activeTab.value = tab
    fetchGroups()
}

async function onRefresh() {
    await fetchGroups()
}

async function onLoadMore() {
    if (!loading.value && hasMore.value) {
        await fetchGroups(true)
    }
}

function goDetail(group: PintuanGroup) {
    uni.navigateTo({ url: `/pages/pintuan/detail?group_id=${group.group_id}` })
}

function goCreate() {
    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login?redirect=pintuan_create' })
        return
    }
    uni.navigateTo({ url: '/pages/pintuan/create' })
}

function goProduct(group: PintuanGroup) {
    if (group.variant?.slug) {
        uni.navigateTo({ url: `/pages/product/detail?slug=${group.variant.slug}` })
    }
}

onMounted(() => {
    fetchGroups()
})

onShow(() => {
    // 从详情页返回时刷新
    fetchGroups()
})
</script>

<template>
    <view class="page">
        <!-- 顶部标签栏 -->
        <view class="tab-bar" role="tablist">
            <view
                class="tab-item"
                :class="{ active: activeTab === 'all' }"
                @tap="onTabChange('all')"
                role="tab"
                aria-selected="true"
            >
                全部
            </view>
            <view
                class="tab-item"
                :class="{ active: activeTab === 'open' }"
                @tap="onTabChange('open')"
                role="tab"
            >
                拼单中
            </view>
            <view
                class="tab-item"
                :class="{ active: activeTab === 'formed' }"
                @tap="onTabChange('formed')"
                role="tab"
            >
                已成团
            </view>
        </view>

        <!-- 列表 -->
        <scroll-view
            class="list-scroll"
            scroll-y
            :style="{ height: 'calc(100vh - 100px)' }"
            refresher-enabled
            :refresher-triggered="loading"
            @refresherrefresh="onRefresh"
            @scrolltolower="onLoadMore"
        >
            <view class="group-list" v-if="groups.length > 0">
                <view
                    class="group-card"
                    v-for="group in groups"
                    :key="group.group_id"
                    @tap="goDetail(group)"
                >
                    <view class="card-main">
                        <view class="card-left">
                            <image
                                class="card-image"
                                :src="getProductMainImage(group.variant || {})"
                                mode="aspectFill"
                            />
                        </view>
                        <view class="card-center">
                            <text class="card-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">
                                {{ group.variant?.product_title || group.variant?.title || '未知商品' }}
                            </text>
                            <text class="card-spec" v-if="group.variant?.option_values && Object.keys(group.variant.option_values).length > 0">
                                {{ Object.values(group.variant.option_values).join(' / ') }}
                            </text>

                            <!-- 进度条 -->
                            <view class="progress-section">
                                <view class="progress-header">
                                    <text class="progress-text">{{ formatProgress(group.participant_count, group.target_size) }} 人</text>
                                    <text class="progress-price">{{ formatPriceWithSymbol(group.pintuan_price_cents) }}</text>
                                </view>
                                <view class="progress-bar">
                                    <view
                                        class="progress-fill"
                                        :style="{ width: getProgressPercent(group.participant_count, group.target_size) + '%' }"
                                    />
                                </view>
                            </view>

                            <!-- 状态与倒计时 -->
                            <view class="card-footer">
                                <view class="status-tag" :style="{ backgroundColor: getStatusColor(group.status) + '20', color: getStatusColor(group.status) }">
                                    {{ getStatusLabel(group.status) }}
                                </view>
                                <view class="countdown" v-if="group.status === 'open'">
                                    <text class="countdown-label">剩余</text>
                                    <text class="countdown-time">{{ formatTimeRemaining(group.expires_at) }}</text>
                                </view>
                            </view>
                        </view>
                        <view class="card-right">
                            <text class="icon-right">›</text>
                        </view>
                    </view>
                </view>
            </view>

            <!-- 空状态 -->
            <view class="empty-state" v-if="!loading && groups.length === 0">
                <image class="empty-icon" src="/static/images/empty-pintuan.png" mode="aspectFit" />
                <text class="empty-title">暂无团购活动</text>
                <text class="empty-desc">敬请期待更多精彩团购</text>
            </view>

            <!-- 加载更多 -->
            <view class="load-more" v-if="hasMore && groups.length > 0">
                <view class="load-spinner" v-if="loading" />
                <text v-else>上拉加载更多</text>
            </view>
            <view class="load-end" v-if="!hasMore && groups.length > 0">
                没有更多了
            </view>
        </scroll-view>

        <!-- 发起团购入口（仅登录用户显示） -->
        <view class="create-entry" v-if="getToken()">
            <button class="btn-create" @tap="goCreate">
                <text class="icon-plus">+</text>
                <text>我要发起团购</text>
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

.tab-bar {
    display: flex;
    background: #FFFFFF;
    border-bottom: 1px solid #EEEEEE;
    position: sticky;
    top: 0;
    z-index: 10;
}

.tab-item {
    flex: 1;
    padding: 14px 0;
    text-align: center;
    font-size: 15px;
    color: #616161;
    position: relative;
}

.tab-item.active {
    color: #2E7D32;
    font-weight: 600;
}

.tab-item.active::after {
    content: '';
    position: absolute;
    bottom: -1px;
    left: 25%;
    right: 25%;
    height: 2px;
    background: #2E7D32;
    border-radius: 1px;
}

.list-scroll {
    flex: 1;
}

.group-list {
    padding: 12px 16px;
}

.group-card {
    background: #FFFFFF;
    border-radius: 12px;
    margin-bottom: 12px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
}

.card-main {
    display: flex;
    align-items: stretch;
    padding: 12px;
    gap: 12px;
}

.card-left {
    width: 80px;
    height: 80px;
    border-radius: 8px;
    overflow: hidden;
    flex-shrink: 0;
}

.card-image {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

.card-center {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
}

.card-title {
    font-size: 14px;
    color: #1A1A1A;
    line-height: 1.4;
    margin-bottom: 4px;
}

.card-spec {
    font-size: 12px;
    color: #9E9E9E;
    margin-bottom: 8px;
}

.progress-section {
    margin-bottom: 8px;
}

.progress-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 4px;
}

.progress-text {
    font-size: 12px;
    color: #616161;
}

.progress-price {
    font-size: 15px;
    font-weight: 600;
    color: #F44336;
}

.progress-bar {
    height: 6px;
    background: #E0E0E0;
    border-radius: 3px;
    overflow: hidden;
}

.progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #2E7D32, #4CAF50);
    border-radius: 3px;
    transition: width 0.3s ease;
}

.card-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
}

.status-tag {
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 11px;
    font-weight: 500;
}

.countdown {
    display: flex;
    align-items: center;
    gap: 4px;
}

.countdown-label {
    font-size: 11px;
    color: #9E9E9E;
}

.countdown-time {
    font-size: 12px;
    font-weight: 600;
    color: #F44336;
    font-variant-numeric: tabular-nums;
}

.card-right {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 24px;
    flex-shrink: 0;
}

.icon-right {
    font-size: 18px;
    color: #BDBDBD;
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
    margin-bottom: 16px;
    opacity: 0.5;
}

.empty-title {
    font-size: 16px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 8px;
}

.empty-desc {
    font-size: 13px;
    color: #9E9E9E;
}

.load-more {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 16px;
    color: #9E9E9E;
    font-size: 13px;
}

.load-spinner {
    width: 20px;
    height: 20px;
    border: 2px solid #E0E0E0;
    border-top-color: #2E7D32;
    border-radius: 50%;
    animation: spin 1s linear infinite;
}

@keyframes spin {
    to { transform: rotate(360deg); }
}

.load-end {
    text-align: center;
    padding: 16px;
    color: #9E9E9E;
    font-size: 13px;
}

.create-entry {
    padding: 16px;
    padding-bottom: calc(16px + env(safe-area-inset-bottom, 34px));
}

.btn-create {
    width: 100%;
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
    gap: 8px;
    box-shadow: 0 4px 12px rgba(46, 125, 50, 0.3);
}

.icon-plus {
    font-size: 20px;
    line-height: 1;
}
</style>
<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { onShow } from '@dcloudio/uni-app'
import { pintuanApi } from '@/utils/api'
import { formatPriceWithSymbol, getProductMainImage, formatTimeRemaining, formatDateTime, maskNickname } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { PintuanGroup, PintuanParticipant } from '@/types'

const props = defineProps<{
    group_id: string
}>()

const group = ref<PintuanGroup | null>(null)
const loading = ref(false)
const joining = ref(false)
const showShare = ref(false)

const isCreator = computed(() => {
    const token = getToken()
    if (!token || !group.value) return false
    // TODO: 从 token 解析 customer_id 对比
    return false
})

const canJoin = computed(() => {
    if (!group.value) return false
    return group.value.status === 'open'
})

const isJoined = computed(() => {
    if (!group.value) return false
    const token = getToken()
    if (!token) return false
    // TODO: 检查是否已参团
    return false
})

const progressPercent = computed(() => {
    if (!group.value) return 0
    return Math.min(100, Math.round((group.value.participant_count / group.value.target_size) * 100))
})

async function fetchGroup() {
    loading.value = true
    try {
        const res = await pintuanApi.getGroup(props.group_id)
        group.value = res
    } catch (e: any) {
        uni.showToast({ title: e.message || '加载失败', icon: 'none' })
        setTimeout(() => uni.navigateBack(), 1500)
    } finally {
        loading.value = false
    }
}

async function joinGroup() {
    const token = getToken()
    if (!token) {
        uni.navigateTo({ url: '/pages/login/login?redirect=pintuan_detail&group_id=' + props.group_id })
        return
    }

    joining.value = true
    try {
        const res = await pintuanApi.joinGroup(props.group_id, { quantity: 1 })
        group.value = res
        uni.showToast({ title: '参团成功', icon: 'success' })
    } catch (e: any) {
        uni.showToast({ title: e.message || '参团失败', icon: 'none' })
    } finally {
        joining.value = false
    }
}

function goProduct() {
    if (group.value?.variant?.slug) {
        uni.navigateTo({ url: `/pages/product/detail?slug=${group.value.variant.slug}` })
    }
}

function onShareAppMessage() {
    if (!group.value) return
    return {
        title: `${group.value.variant?.product_title || group.value.variant?.title || '团购商品'} - ${formatPriceWithSymbol(group.value.pintuan_price_cents)} 拼单`,
        path: `/pages/pintuan/detail?group_id=${group.value.group_id}`,
        imageUrl: group.value.variant?.image_url || ''
    }
}

function onShareTimeline() {
    if (!group.value) return
    return {
        title: `【团购】${group.value.variant?.product_title || group.value.variant?.title || '团购商品'} - ${formatPriceWithSymbol(group.value.pintuan_price_cents)}`,
        query: `group_id=${group.value.group_id}`,
        imageUrl: group.value.variant?.image_url || ''
    }
}

onMounted(() => {
    fetchGroup()
})

onShow(() => {
    // 从分享进入或参团后刷新
    fetchGroup()
})
</script>

<template>
    <view class="page">
        <scroll-view
            class="detail-scroll"
            scroll-y
            :style="{ height: 'calc(100vh - 98px)' }"
            refresher-enabled
            :refresher-triggered="loading"
            @refresherrefresh="fetchGroup"
            v-if="group"
        >
            <!-- 商品头部 -->
            <view class="product-header">
                <view class="product-main">
                    <image
                        class="product-image"
                        :src="getProductMainImage(group.variant || {})"
                        mode="aspectFill"
                    />
                    <view class="product-info">
                        <text class="product-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">
                            {{ group.variant?.product_title || group.variant?.title || '未知商品' }}
                        </text>
                        <text class="product-spec" v-if="group.variant?.option_values && Object.keys(group.variant.option_values).length > 0">
                            {{ Object.values(group.variant.option_values).join(' / ') }}
                        </text>
                        <view class="product-price-row">
                            <text class="pintuan-price">{{ formatPriceWithSymbol(group.pintuan_price_cents) }}</text>
                            <text class="price-unit">/ 份</text>
                        </view>
                    </view>
                </view>

                <!-- 进度条 -->
                <view class="progress-section">
                    <view class="progress-header">
                        <view class="progress-left">
                            <text class="progress-label">拼单进度</text>
                            <text class="progress-count">{{ group.participant_count }} / {{ group.target_size }} 人</text>
                        </view>
                        <view class="progress-right" v-if="group.status === 'open'">
                            <text class="countdown-time">{{ formatTimeRemaining(group.expires_at) }}</text>
                        </view>
                    </view>
                    <view class="progress-bar">
                        <view
                            class="progress-fill"
                            :style="{ width: progressPercent + '%' }"
                        />
                    </view>
                    <view class="progress-status">
                        <view
                            class="status-badge"
                            :class="group.status"
                        >
                            {{ group.status === 'open' ? '拼单中' : group.status === 'formed' ? '已成团' : '已过期' }}
                        </view>
                    </view>
                </view>
            </view>

            <!-- 参团名单 -->
            <view class="section">
                <view class="section-header">
                    <text class="section-title">接龙名单 ({{ group.participant_count }})</text>
                </view>
                <view class="participant-list" v-if="group.participants && group.participants.length > 0">
                    <view
                        class="participant-item"
                        v-for="(participant, index) in group.participants"
                        :key="participant.customer_id"
                    >
                        <view class="participant-rank">
                            <text :class="['rank-num', index < 3 ? 'top3' : '']">{{ index + 1 }}</text>
                        </view>
                        <view class="participant-info">
                            <text class="participant-name">
                                {{ participant.nickname_masked || maskNickname(participant.name) }}
                            </text>
                            <text class="participant-time">{{ formatDateTime(participant.created_at) }}</text>
                        </view>
                        <view class="participant-qty" v-if="participant.quantity > 1">
                            ×{{ participant.quantity }}
                        </view>
                        <view class="participant-tag" v-if="index === 0">团长</view>
                    </view>
                </view>
                <view class="no-participants" v-else>
                    暂无人参团，快叫朋友来拼单吧~
                </view>
            </view>

            <!-- 团购说明 -->
            <view class="section">
                <view class="section-header">
                    <text class="section-title">活动说明</text>
                </view>
                <view class="rules-list">
                    <view class="rule-item">
                        <text class="rule-num">1.</text>
                        <text>本活动为意向拼单，不收款、不占库存，成团后由商家联系下单</text>
                    </view>
                    <view class="rule-item">
                        <text class="rule-num">2.</text>
                        <text>成团条件：参与人数达到 {{ group.target_size }} 人（按人头计算）</text>
                    </view>
                    <view class="rule-item">
                        <text class="rule-num">3.</text>
                        <text>每人限参一份，一人一团仅能参与一次</text>
                    </view>
                    <view class="rule-item">
                        <text class="rule-num">4.</text>
                        <text>拼单价与会员折扣、优惠券不叠加，商家履约时按最优价执行</text>
                    </view>
                    <view class="rule-item">
                        <text class="rule-num">5.</text>
                        <text>过期未成团自动失效，不产生任何费用</text>
                    </view>
                </view>
            </view>

            <view class="bottom-spacer" />
        </scroll-view>

        <!-- 底部操作栏 -->
        <view class="bottom-bar" v-if="group && group.status === 'open'">
            <button
                class="btn-join"
                @tap="joinGroup"
                :disabled="joining"
                :class="{ 'btn-joined': isJoined }"
            >
                <text v-if="!isJoined && !joining">立即参团</text>
                <text v-else-if="joining">参团中...</text>
                <text v-else>已参团</text>
            </button>
        </view>
        <view class="bottom-bar-ended" v-if="group && group.status !== 'open'">
            <text class="ended-text">
                {{ group.status === 'formed' ? '本团已成团，等待商家联系下单' : '本团已过期' }}
            </text>
        </view>

        <view class="loading-overlay" v-if="loading && !group">
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

.product-header {
    background: #FFFFFF;
    padding: 16px;
    margin-bottom: 8px;
}

.product-main {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
}

.product-image {
    width: 90px;
    height: 90px;
    border-radius: 10px;
    object-fit: cover;
    flex-shrink: 0;
}

.product-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: space-between;
}

.product-title {
    font-size: 15px;
    color: #1A1A1A;
    line-height: 1.4;
    margin-bottom: 4px;
}

.product-spec {
    font-size: 12px;
    color: #9E9E9E;
    margin-bottom: 8px;
}

.product-price-row {
    display: flex;
    align-items: baseline;
    gap: 4px;
}

.pintuan-price {
    font-size: 22px;
    font-weight: 700;
    color: #F44336;
}

.price-unit {
    font-size: 12px;
    color: #9E9E9E;
}

.progress-section {
    padding-top: 12px;
    border-top: 1px solid #F5F5F5;
}

.progress-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.progress-left {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.progress-label {
    font-size: 13px;
    color: #616161;
}

.progress-count {
    font-size: 15px;
    font-weight: 600;
    color: #1A1A1A;
}

.countdown-time {
    font-size: 16px;
    font-weight: 700;
    color: #F44336;
    font-variant-numeric: tabular-nums;
}

.progress-bar {
    height: 8px;
    background: #E0E0E0;
    border-radius: 4px;
    overflow: hidden;
    margin-bottom: 8px;
}

.progress-fill {
    height: 100%;
    background: linear-gradient(90deg, #2E7D32, #4CAF50);
    border-radius: 4px;
    transition: width 0.3s ease;
}

.progress-status {
    display: flex;
    justify-content: flex-end;
}

.status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
}

.status-badge.open {
    background: #E8F5E9;
    color: #2E7D32;
}

.status-badge.formed {
    background: #E3F2FD;
    color: #1976D2;
}

.status-badge.expired {
    background: #F5F5F5;
    color: #9E9E9E;
}

.section {
    background: #FFFFFF;
    margin-bottom: 8px;
    padding: 0 16px;
}

.section-header {
    padding: 14px 0;
    border-bottom: 1px solid #F5F5F5;
}

.section-title {
    font-size: 15px;
    font-weight: 600;
    color: #1A1A1A;
}

.participant-list {
    padding: 8px 0;
}

.participant-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 10px 0;
    border-bottom: 1px solid #F5F5F5;
}

.participant-item:last-child {
    border-bottom: none;
}

.participant-rank {
    width: 28px;
    height: 28px;
    display: flex;
    align-items: center;
    justify-content: center;
    border-radius: 50%;
    background: #F5F5F5;
    flex-shrink: 0;
}

.rank-num {
    font-size: 12px;
    color: #9E9E9E;
    font-weight: 500;
}

.rank-num.top3 {
    background: linear-gradient(135deg, #FFD54F, #FFB300);
    color: #1A1A1A;
    font-weight: 700;
}

.participant-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.participant-name {
    font-size: 14px;
    color: #1A1A1A;
}

.participant-time {
    font-size: 11px;
    color: #9E9E9E;
}

.participant-qty {
    font-size: 13px;
    color: #616161;
    margin-right: 8px;
}

.participant-tag {
    font-size: 10px;
    color: #2E7D32;
    background: #E8F5E9;
    padding: 1px 6px;
    border-radius: 3px;
    font-weight: 500;
}

.no-participants {
    text-align: center;
    padding: 24px;
    color: #9E9E9E;
    font-size: 13px;
}

.rules-list {
    padding: 12px 0;
}

.rule-item {
    display: flex;
    gap: 8px;
    padding: 8px 0;
    color: #616161;
    font-size: 13px;
    line-height: 1.5;
}

.rule-num {
    color: #9E9E9E;
    flex-shrink: 0;
    margin-top: 1px;
}

.bottom-bar {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    padding: 12px 16px;
    background: #FFFFFF;
    border-top: 1px solid #EEEEEE;
    padding-bottom: calc(12px + env(safe-area-inset-bottom, 34px));
    z-index: 50;
}

.btn-join {
    width: 100%;
    height: 50px;
    background: linear-gradient(135deg, #2E7D32, #4CAF50);
    color: #FFFFFF;
    border: none;
    border-radius: 25px;
    font-size: 16px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
    box-shadow: 0 4px 12px rgba(46, 125, 50, 0.3);
}

.btn-join:disabled {
    background: #A5D6A7;
    box-shadow: none;
}

.btn-join.btn-joined {
    background: #E0E0E0;
    color: #9E9E9E;
    box-shadow: none;
}

.bottom-bar-ended {
    position: fixed;
    bottom: 0;
    left: 0;
    right: 0;
    padding: 14px 16px;
    background: #FFFFFF;
    border-top: 1px solid #EEEEEE;
    text-align: center;
    padding-bottom: calc(14px + env(safe-area-inset-bottom, 34px));
    z-index: 50;
}

.ended-text {
    font-size: 14px;
    color: #616161;
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
<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { adminApi, pintuanApi } from '@/utils/api'
import { formatDateTime, formatTimeRemaining, maskNickname } from '@/utils/helpers'

const groups = ref<any[]>([])
const loading = ref(false)
const loadingMore = ref(false)
const hasMore = ref(true)
const limit = 20
let offset = 0

const statusFilter = ref<string>('') // all, open, formed, expired
const selectedGroup = ref<any | null>(null)
const showGroupDetail = ref(false)
const sweepLoading = ref(false)

const statusTabs = [
    { value: '', label: '全部' },
    { value: 'open', label: '拼单中' },
    { value: 'formed', label: '已成团' },
    { value: 'expired', label: '已过期' },
]

async function fetchGroups(append = false) {
    if (append) {
        loadingMore.value = true
    } else {
        loading.value = true
    }
    try {
        const res = await adminApi.getPintuanGroups(statusFilter.value || undefined, limit, append ? offset : 0)
        if (append) {
            groups.value = [...groups.value, ...res.items]
        } else {
            groups.value = res.items
            offset = 0
        }
        hasMore.value = res.items.length === limit
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

async function sweepExpired() {
    const confirm = await uni.showModal({
        title: '确认过期',
        content: '将所有已过期的拼单团标记为过期，此操作不可撤销。',
        confirmColor: '#F44336',
    })
    if (!confirm.confirm) return

    sweepLoading.value = true
    try {
        const res = await adminApi.sweepExpiredPintuanGroups()
        uni.showToast({ title: `已过期 ${res.expired_count} 个团`, icon: 'success' })
        await fetchGroups()
    } catch (e: any) {
        uni.showToast({ title: e.message || '操作失败', icon: 'none' })
    } finally {
        sweepLoading.value = false
    }
}

async function openGroupDetail(group: any) {
    selectedGroup.value = group
    showGroupDetail.value = true
}

function getStatusLabel(status: string): string {
    const map: Record<string, string> = {
        open: '拼单中',
        formed: '已成团',
        expired: '已过期',
    }
    return map[status] || status
}

function getStatusClass(status: string): string {
    return `status-${status}`
}

// 工具函数
function formatOptionText(optionValues: Record<string, any>): string {
    if (!optionValues) return ''
    return Object.values(optionValues).filter(v => v !== null && v !== undefined && v !== '').map(v => String(v)).join(' / ')
}

function formatPrice(cents: number): string {
    return '¥' + (cents / 100).toFixed(2)
}

function maskPhone(phone: string): string {
    if (!phone) return ''
    if (phone.length <= 3) return phone
    return phone.slice(0, 3) + '****' + phone.slice(-4)
}

onMounted(() => {
    fetchGroups()
})

// 监听页面滚动到底部加载更多
function onReachBottom() {
    if (hasMore.value && !loadingMore.value) {
        fetchGroups(true)
    }
}

// 加载拣货单
async function loadPickList() {
    if (!selectedGroup.value) return
    try {
        const res = await adminApi.getPintuanPickList(selectedGroup.value.group_id)
        uni.showModal({
            title: '拣货清单',
            content: JSON.stringify(res, null, 2),
            showCancel: false,
        })
    } catch (e: any) {
        uni.showToast({ title: e.message || '获取拣货单失败', icon: 'none' })
    }
}
</script>

<template>
    <view class="page">
        <view class="header">
            <text class="header-title">团购管理</text>
            <view class="header-actions">
                <button class="btn-sweep" @tap="sweepExpired" :disabled="sweepLoading" size="mini">
                    <text v-if="!sweepLoading">过期清理</text>
                    <text v-else>处理中...</text>
                </button>
            </view>
        </view>

        <view class="status-tabs">
            <view
                class="tab-item"
                :class="{ active: statusFilter === tab.value }"
                v-for="tab in statusTabs"
                :key="tab.value"
                @tap="statusFilter = tab.value; fetchGroups()"
            >
                {{ tab.label }}
            </view>
        </view>

        <scroll-view
            class="group-list"
            scroll-y
            :style="{ height: 'calc(100vh - 160px)' }"
            refresher-enabled
            :refresher-triggered="loading"
            @refresherrefresh="fetchGroups"
            @scrolltolower="hasMore && !loadingMore && fetchGroups(true)"
        >
            <view class="empty-state" v-if="!loading && groups.length === 0">
                <text class="empty-icon">🛒</text>
                <text class="empty-text">暂无团购活动</text>
            </view>

            <view class="group-items" v-else>
                <view
                    class="group-item"
                    v-for="group in groups"
                    :key="group.group_id"
                    @tap="openGroupDetail(group)"
                >
                    <view class="item-header">
                        <image class="item-image" :src="group.variant?.image_url || '/static/images/placeholder.png'" mode="aspectFill" />
                        <view class="item-basic">
                            <text class="item-title">{{ group.variant?.title || group.variant?.product_title || '未知商品' }}</text>
                            <text class="item-spec" v-if="group.variant?.option_values">
                                {{ formatOptionText(group.variant.option_values) }}
                            </text>
                        </view>
                    </view>

                    <view class="item-info">
                        <view class="info-row">
                            <text class="info-label">团购价</text>
                            <text class="info-value price">{{ formatPrice(group.pintuan_price_cents) }}</text>
                        </view>
                        <view class="info-row">
                            <text class="info-label">目标人数</text>
                            <text class="info-value">{{ group.target_size }} 人</text>
                        </view>
                        <view class="info-row">
                            <text class="info-label">已参团</text>
                            <text class="info-value highlight">{{ group.participant_count }} 人</text>
                        </view>
                        <view class="info-row">
                            <text class="info-label">拣货量</text>
                            <text class="info-value">{{ group.total_pick_qty || 0 }} 份</text>
                        </view>
                    </view>

                    <view class="item-footer">
                        <view class="status-badge" :class="getStatusClass(group.status)">
                            {{ getStatusLabel(group.status) }}
                        </view>
                        <view class="time-info">
                            <text class="time-label" v-if="group.status === 'open'">剩余: {{ formatTimeRemaining(group.expires_at) }}</text>
                            <text class="time-label" v-else>创建: {{ formatDateTime(group.created_at) }}</text>
                        </view>
                    </view>
                </view>
            </view>

            <view class="load-more" v-if="hasMore">
                <view class="spinner" v-if="loadingMore" />
                <text v-else class="load-more-text">上拉加载更多</text>
            </view>
        </scroll-view>

        <view class="loading-overlay" v-if="loading && groups.length === 0">
            <view class="spinner" />
        </view>

        <!-- 详情弹窗 -->
        <view class="modal-mask" v-if="showGroupDetail" @tap="showGroupDetail = false">
            <view class="modal detail-modal" @tap.stop>
                <view class="modal-header">
                    <text class="modal-title">团购详情</text>
                    <text class="modal-close" @tap="showGroupDetail = false">✕</text>
                </view>

                <scroll-view class="modal-body" scroll-y v-if="selectedGroup">
                    <view class="detail-section">
                        <view class="detail-header">
                            <image class="detail-image" :src="selectedGroup.variant?.image_url || '/static/images/placeholder.png'" mode="aspectFill" />
                            <view class="detail-basic">
                                <text class="detail-title">{{ selectedGroup.variant?.title || selectedGroup.variant?.product_title || '未知商品' }}</text>
                                <text class="detail-spec" v-if="selectedGroup.variant?.option_values">
                                    {{ formatOptionText(selectedGroup.variant.option_values) }}
                                </text>
                                <view class="detail-price-row">
                                    <text class="detail-price-label">团购价</text>
                                    <text class="detail-price">{{ formatPrice(selectedGroup.pintuan_price_cents) }}</text>
                                </view>
                            </view>
                        </view>

                        <view class="detail-meta">
                            <view class="meta-item">
                                <text class="meta-label">状态</text>
                                <view class="status-badge" :class="getStatusClass(selectedGroup.status)">
                                    {{ getStatusLabel(selectedGroup.status) }}
                                </view>
                            </view>
                            <view class="meta-item">
                                <text class="meta-label">目标人数</text>
                                <text class="meta-value">{{ selectedGroup.target_size }} 人</text>
                            </view>
                            <view class="meta-item">
                                <text class="meta-label">已参团</text>
                                <text class="meta-value highlight">{{ selectedGroup.participant_count }} 人</text>
                            </view>
                            <view class="meta-item">
                                <text class="meta-label">拣货总量</text>
                                <text class="meta-value">{{ selectedGroup.total_pick_qty || 0 }} 份</text>
                            </view>
                            <view class="meta-item" v-if="selectedGroup.status === 'open'">
                                <text class="meta-label">截止时间</text>
                                <text class="meta-value">{{ formatDateTime(selectedGroup.expires_at) }}</text>
                            </view>
                            <view class="meta-item">
                                <text class="meta-label">创建时间</text>
                                <text class="meta-value">{{ formatDateTime(selectedGroup.created_at) }}</text>
                            </view>
                        </view>
                    </view>

                    <view class="detail-section">
                        <text class="section-title">参团名单 ({{ selectedGroup.participants?.length || 0 }})</text>
                        <view class="participant-list" v-if="selectedGroup.participants && selectedGroup.participants.length > 0">
                            <view class="participant-item" v-for="(p, index) in selectedGroup.participants" :key="p.customer_id || index">
                                <text class="participant-index">{{ index + 1 }}</text>
                                <view class="participant-info">
                                    <text class="participant-name">{{ p.name || maskNickname(p.nickname_masked || p.name) }}</text>
                                    <text class="participant-phone" v-if="p.phone">{{ maskPhone(p.phone) }}</text>
                                </view>
                                <text class="participant-qty">x {{ p.quantity }}</text>
                                <text class="participant-time">{{ formatDateTime(p.created_at) }}</text>
                            </view>
                        </view>
                        <view class="no-participants" v-else>
                            暂无参团记录
                        </view>
                    </view>

                    <view class="detail-section" v-if="selectedGroup.status === 'formed'">
                        <text class="section-title">拣货清单</text>
                        <button class="btn-picklist" @tap="loadPickList">
                            生成/查看拣货单
                        </button>
                    </view>
                </scroll-view>

                <view class="modal-footer">
                    <button class="btn-confirm" @tap="showGroupDetail = false">关闭</button>
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

.header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px;
    background: #FFFFFF;
    border-bottom: 1px solid #EEEEEE;
    position: sticky;
    top: 0;
    z-index: 10;
}

.header-title {
    font-size: 18px;
    font-weight: 600;
    color: #1A1A1A;
}

.header-actions {
    display: flex;
    gap: 8px;
}

.btn-sweep {
    background: #FFF3E0;
    color: #E65100;
    border: none;
    border-radius: 16px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 500;
}

.btn-sweep:disabled {
    background: #FFE0B2;
    color: #FFB74D;
}

.status-tabs {
    display: flex;
    gap: 8px;
    padding: 12px 16px;
    background: #FFFFFF;
    border-bottom: 1px solid #EEEEEE;
    overflow-x: auto;
    position: sticky;
    top: 64px;
    z-index: 9;
}

.tab-item {
    padding: 6px 14px;
    border-radius: 16px;
    font-size: 13px;
    color: #616161;
    white-space: nowrap;
    background: #F5F5F5;
    flex-shrink: 0;
}

.tab-item.active {
    background: #2E7D32;
    color: #FFFFFF;
}

.group-list {
    flex: 1;
    padding: 12px;
}

.empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    padding: 60px 24px;
    background: #FFFFFF;
    border-radius: 12px;
    margin-top: 12px;
    text-align: center;
}

.empty-icon {
    font-size: 48px;
    margin-bottom: 16px;
}

.empty-text {
    font-size: 16px;
    font-weight: 500;
    color: #1A1A1A;
}

.group-items {
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.group-item {
    background: #FFFFFF;
    border-radius: 12px;
    padding: 12px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.item-header {
    display: flex;
    gap: 12px;
    margin-bottom: 12px;
}

.item-image {
    width: 70px;
    height: 70px;
    border-radius: 8px;
    object-fit: cover;
    flex-shrink: 0;
}

.item-basic {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
}

.item-title {
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.item-spec {
    font-size: 12px;
    color: #9E9E9E;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.item-info {
    display: flex;
    flex-direction: column;
    gap: 6px;
    margin-bottom: 12px;
    padding: 8px;
    background: #FAFAFA;
    border-radius: 8px;
}

.info-row {
    display: flex;
    justify-content: space-between;
    align-items: center;
}

.info-label {
    font-size: 13px;
    color: #616161;
}

.info-value {
    font-size: 13px;
    font-weight: 500;
    color: #1A1A1A;
}

.info-value.price {
    font-size: 16px;
    font-weight: 700;
    color: #2E7D32;
}

.info-value.highlight {
    color: #F57C00;
    font-weight: 600;
}

.item-footer {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
}

.status-badge {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 600;
}

.status-open {
    background: #E3F2FD;
    color: #1565C0;
}

.status-formed {
    background: #E8F5E9;
    color: #2E7D32;
}

.status-expired {
    background: #F5F5F5;
    color: #9E9E9E;
}

.time-info {
    text-align: right;
}

.time-label {
    font-size: 11px;
    color: #9E9E9E;
}

.load-more {
    padding: 24px;
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

@keyframes spin {
    to { transform: rotate(360deg); }
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

/* 详情弹窗 */
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
    padding: 24px;
}

.detail-modal {
    background: #FFFFFF;
    border-radius: 16px;
    width: 100%;
    max-width: 400px;
    max-height: 85vh;
    display: flex;
    flex-direction: column;
    animation: slideUp 0.3s ease;
}

@keyframes slideUp {
    from { transform: translateY(20px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
}

.modal-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px;
    border-bottom: 1px solid #EEEEEE;
    position: sticky;
    top: 0;
    background: #FFFFFF;
    border-radius: 16px 16px 0 0;
    z-index: 10;
}

.modal-title {
    font-size: 17px;
    font-weight: 600;
    color: #1A1A1A;
}

.modal-close {
    font-size: 22px;
    color: #9E9E9E;
}

.modal-body {
    flex: 1;
    overflow-y: auto;
    padding: 16px;
}

.detail-section {
    margin-bottom: 20px;
}

.section-title {
    font-size: 15px;
    font-weight: 600;
    color: #1A1A1A;
    margin-bottom: 12px;
}

.detail-header {
    display: flex;
    gap: 12px;
    margin-bottom: 16px;
}

.detail-image {
    width: 80px;
    height: 80px;
    border-radius: 8px;
    object-fit: cover;
    flex-shrink: 0;
}

.detail-basic {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
}

.detail-title {
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
}

.detail-spec {
    font-size: 13px;
    color: #9E9E9E;
}

.detail-price-row {
    display: flex;
    align-items: baseline;
    gap: 8px;
    margin-top: 8px;
}

.detail-price-label {
    font-size: 13px;
    color: #616161;
}

.detail-price {
    font-size: 20px;
    font-weight: 700;
    color: #2E7D32;
}

.detail-meta {
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.meta-item {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 0;
    border-bottom: 1px solid #F5F5F5;
}

.meta-label {
    font-size: 13px;
    color: #616161;
}

.meta-value {
    font-size: 13px;
    font-weight: 500;
    color: #1A1A1A;
}

.meta-value.highlight {
    color: #F57C00;
    font-weight: 600;
}

.participant-list {
    display: flex;
    flex-direction: column;
    gap: 10px;
}

.participant-item {
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 10px;
    background: #FAFAFA;
    border-radius: 8px;
}

.participant-index {
    width: 24px;
    height: 24px;
    background: #2E7D32;
    color: #FFFFFF;
    border-radius: 50%;
    display: flex;
    align-items: center;
    justify-content: center;
    font-size: 11px;
    font-weight: 600;
    flex-shrink: 0;
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
    font-weight: 500;
    color: #1A1A1A;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.participant-phone {
    font-size: 11px;
    color: #9E9E9E;
}

.participant-qty {
    font-size: 13px;
    color: #616161;
}

.participant-time {
    font-size: 11px;
    color: #9E9E9E;
}

.no-participants {
    text-align: center;
    padding: 24px;
    color: #9E9E9E;
    font-size: 13px;
}

.btn-picklist {
    width: 100%;
    background: #E3F2FD;
    color: #1565C0;
    border: none;
    border-radius: 8px;
    padding: 12px;
    font-size: 14px;
    font-weight: 500;
}

.modal-footer {
    padding: 16px;
    border-top: 1px solid #EEEEEE;
    background: #FFFFFF;
    border-radius: 0 0 16px 16px;
    position: sticky;
    bottom: 0;
}

.btn-confirm {
    width: 100%;
    height: 48px;
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 12px;
    font-size: 16px;
    font-weight: 600;
}
</style>
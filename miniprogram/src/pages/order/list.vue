<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { orderApi } from '@/utils/api'
import { formatPriceWithSymbol, formatDateTime, getOrderStatusLabel, getOrderStatusColor } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { Order } from '@/types'

const orders = ref<Order[]>([])
const loading = ref(false)
const hasMore = ref(true)
const page = ref(0)
const pageSize = 20
const activeTab = ref<'all' | 'pending' | 'paid' | 'shipped' | 'completed' | 'cancelled'>('all')

const statusTabs = [
    { key: 'all' as const, label: '全部' },
    { key: 'pending' as const, label: '待支付' },
    { key: 'paid' as const, label: '待发货' },
    { key: 'shipped' as const, label: '待收货' },
    { key: 'completed' as const, label: '已完成' },
    { key: 'cancelled' as const, label: '已取消' }
]

// Helper functions
function getOrderStatusBg(status: string): string {
    const colors: Record<string, string> = {
        pending: '#FFF3E0',
        payment_pending: '#FFF3E0',
        paid: '#E3F2FD',
        confirmed: '#E3F2FD',
        preparing: '#E3F2FD',
        shipped: '#F3E5F5',
        delivering: '#F3E5F5',
        completed: '#E8F5E9',
        cancelled: '#F5F5F5',
        refunded: '#F5F5F5'
    }
    return colors[status] || '#F5F5F5'
}

function getItemImage(item: any): string {
    return '/static/images/placeholder.png'
}

function formatOptionText(optionValues: Record<string, any>): string {
    if (!optionValues) return ''
    return Object.values(optionValues).filter(v => v !== null && v !== undefined && v !== '').map(v => String(v)).join(' / ')
}

function getEmptyTitle(): string {
    const titles: Record<string, string> = {
        all: '暂无订单',
        pending: '暂无待支付订单',
        paid: '暂无待发货订单',
        shipped: '暂无待收货订单',
        completed: '暂无已完成订单',
        cancelled: '暂无取消/退款订单'
    }
    return titles[activeTab.value] || '暂无订单'
}

function getEmptyDesc(): string {
    const descs: Record<string, string> = {
        all: '去首页挑选心仪的生鲜好物',
        pending: '已支付的订单会显示在这里',
        paid: '商家正在备货中',
        shipped: '配送中的订单会显示在这里',
        completed: '完成的订单可在此查看',
        cancelled: '取消或退款的订单记录'
    }
    return descs[activeTab.value] || ''
}

const filteredOrders = computed(() => {
    if (activeTab.value === 'all') return orders.value
    const statusMap: Record<string, string[]> = {
        pending: ['pending', 'payment_pending'],
        paid: ['paid', 'confirmed'],
        shipped: ['preparing', 'shipped', 'delivering'],
        completed: ['completed'],
        cancelled: ['cancelled', 'refunded']
    }
    const statuses = statusMap[activeTab.value] || []
    return orders.value.filter(o => statuses.includes(o.status))
})

async function fetchOrders(reset = false) {
    if (loading.value) return
    if (reset) {
        page.value = 0
        orders.value = []
        hasMore.value = true
    }
    if (!hasMore.value) return

    loading.value = true
    try {
        const res = await orderApi.getMyOrders()
        if (res.length < pageSize) hasMore.value = false
        orders.value = [...orders.value, ...res]
        page.value++
    } catch (e) {
        console.error('获取订单失败:', e)
        hasMore.value = false
    } finally {
        loading.value = false
        uni.stopPullDownRefresh()
    }
}

async function onRefresh() {
    await fetchOrders(true)
}

async function onLoadMore() {
    if (!loading.value && hasMore.value) {
        await fetchOrders(false)
    }
}

function onTabChange(tab: typeof statusTabs[0]) {
    activeTab.value = tab.key
}

function goDetail(order: Order) {
    uni.navigateTo({ url: `/pages/order/detail?id=${order.id}` })
}

function goPay(order: Order) {
    uni.navigateTo({ url: `/pages/checkout/checkout?order_id=${order.id}` })
}

function goRebuy(order: Order) {
    // 重新购买：将订单商品加入购物车
    uni.showToast({ title: '功能开发中', icon: 'none' })
}

function formatOrderTime(order: Order): string {
    return formatDateTime(order.created_at, 'MM-DD HH:mm')
}

onMounted(() => {
    fetchOrders(true)
})

onShow(() => {
    // 从支付/详情页返回可能需要刷新
    const needRefresh = uni.getStorageSync('order_need_refresh')
    if (needRefresh) {
        uni.removeStorageSync('order_need_refresh')
        fetchOrders(true)
    }
})

onPullDownRefresh(() => {
    onRefresh()
})
</script>

<template>
    <view class="page">
        <!-- 顶部标签栏 -->
        <view class="tab-bar" :style="{ transform: 'translateX(' + (-activeTabIndex * 100 / statusTabs.length) + '%)' }">
            <view
                class="tab-item"
                :class="{ active: activeTab === tab.key }"
                v-for="(tab, index) in statusTabs"
                :key="tab.key"
                @tap="onTabChange(tab)"
            >
                {{ tab.label }}
            </view>
        </view>

        <scroll-view
            class="order-scroll"
            scroll-y
            :style="{ height: 'calc(100vh - 90px)' }"
            refresher-enabled
            :refresher-triggered="loading"
            @refresherrefresh="onRefresh"
            @scrolltolower="onLoadMore"
        >
            <view class="order-list" v-if="filteredOrders.length">
                <view class="order-card" v-for="order in filteredOrders" :key="order.id" @tap="goDetail(order)">
                    <view class="order-header">
                        <view class="order-id-time">
                            <text class="order-id">订单号：{{ order.id.slice(0, 8) }}...</text>
                            <text class="order-time">{{ formatOrderTime(order) }}</text>
                        </view>
                        <view class="order-status" :style="{ color: getOrderStatusColor(order.status), background: getOrderStatusBg(order.status) }">
                            {{ getOrderStatusLabel(order.status) }}
                        </view>
                    </view>

                    <view class="order-items">
                        <view class="order-item" v-for="(item, index) in order.line_items.slice(0, 3)" :key="item.id">
                            <image class="item-image" :src="getItemImage(item)" mode="aspectFill" />
                            <view class="item-info">
                                <text class="item-title" style="-webkit-line-clamp: 1; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ item.product_title }}</text>
                                <text class="item-spec" v-if="item.option_values && Object.keys(item.option_values).length > 0">{{ formatOptionText(item.option_values) }}</text>
                                <view class="item-price-qty">
                                    <text class="item-price">{{ formatPriceWithSymbol(item.unit_price_cents) }}</text>
                                    <text class="item-qty">x {{ item.quantity }}</text>
                                </view>
                            </view>
                        </view>
                        <view class="item-more" v-if="order.line_items.length > 3">
                            共 {{ order.line_items.length }} 件商品
                        </view>
                    </view>

                    <view class="order-footer">
                        <view class="order-total">
                            <text>实付</text>
                            <text class="total-price">{{ formatPriceWithSymbol(order.grand_total_cents) }}</text>
                        </view>
                        <view class="order-actions">
                            <button class="btn-secondary" v-if="['pending', 'payment_pending'].includes(order.status)" @tap.stop="goPay(order)">去支付</button>
                            <button class="btn-secondary" v-if="order.status === 'cancelled'" @tap.stop="goRebuy(order)">重新购买</button>
                            <button class="btn-primary" v-if="['completed'].includes(order.status)" @tap.stop="goRebuy(order)">再次购买</button>
                        </view>
                    </view>
                </view>
            </view>

            <view class="empty-state" v-else-if="!loading">
                <image class="empty-icon" src="/static/images/empty-order.png" mode="aspectFit" />
                <text class="empty-title">{{ getEmptyTitle() }}</text>
                <text class="empty-desc">{{ getEmptyDesc() }}</text>
                <button class="btn-go-shop" v-if="activeTab === 'all'" @tap="uni.switchTab({ url: '/pages/index/index' })">去逛逛</button>
            </view>

            <view class="load-more" v-if="loading && filteredOrders.length">
                <uni-loading-icon />
                <text>加载中...</text>
            </view>
            <view class="load-more" v-if="!hasMore && filteredOrders.length">
                <text>已加载全部订单</text>
            </view>
        </scroll-view>
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
    width: 100%;
    overflow-x: auto;
    -webkit-overflow-scrolling: touch;
    scrollbar-width: none;
}

.tab-bar::-webkit-scrollbar {
    display: none;
}

.tab-item {
    flex: 0 0 auto;
    padding: 14px 16px;
    font-size: 14px;
    color: #616161;
    white-space: nowrap;
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
    left: 50%;
    transform: translateX(-50%);
    width: 24px;
    height: 2px;
    background: #2E7D32;
    border-radius: 1px;
}

.order-scroll {
    flex: 1;
}

.order-list {
    padding: 8px 16px 24px;
}

.order-card {
    background: #FFFFFF;
    border-radius: 12px;
    margin-bottom: 8px;
    padding: 12px 16px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
}

.order-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 12px;
}

.order-id-time {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.order-id {
    font-size: 13px;
    color: #9E9E9E;
}

.order-time {
    font-size: 12px;
    color: #9E9E9E;
}

.order-status {
    padding: 4px 10px;
    border-radius: 12px;
    font-size: 12px;
    font-weight: 500;
    white-space: nowrap;
}

.order-items {
    margin-bottom: 12px;
}

.order-item {
    display: flex;
    gap: 10px;
    padding: 8px 0;
    border-bottom: 1px solid #F5F5F5;
}

.order-item:last-child {
    border-bottom: none;
}

.item-image {
    width: 50px;
    height: 50px;
    border-radius: 6px;
    object-fit: cover;
}

.item-info {
    flex: 1;
    min-width: 0;
}

.item-title {
    font-size: 13px;
    color: #1A1A1A;
    margin-bottom: 2px;
}

.item-spec {
    font-size: 11px;
    color: #9E9E9E;
    margin-bottom: 4px;
}

.item-price-qty {
    display: flex;
    align-items: center;
    gap: 8px;
}

.item-price {
    font-size: 13px;
    font-weight: 500;
    color: #2E7D32;
}

.item-qty {
    font-size: 12px;
    color: #9E9E9E;
}

.item-more {
    padding: 8px 0;
    text-align: right;
    font-size: 12px;
    color: #9E9E9E;
}

.order-footer {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
}

.order-total {
    display: flex;
    align-items: baseline;
    gap: 4px;
}

.order-total text:first-child {
    font-size: 13px;
    color: #616161;
}

.total-price {
    font-size: 18px;
    font-weight: 700;
    color: #2E7D32;
}

.order-actions {
    display: flex;
    gap: 8px;
}

.btn-primary,
.btn-secondary {
    height: 32px;
    padding: 0 16px;
    border-radius: 6px;
    font-size: 13px;
    font-weight: 500;
    border: none;
}

.btn-primary {
    background: #2E7D32;
    color: #FFFFFF;
}

.btn-secondary {
    background: #FFFFFF;
    color: #2E7D32;
    border: 1px solid #2E7D32;
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

.empty-title {
    font-size: 16px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 8px;
}

.empty-desc {
    font-size: 14px;
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

.load-more {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 24px;
    color: #9E9E9E;
    font-size: 13px;
}

.load-more text {
    margin-top: 8px;
}
</style>
<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { orderApi } from '@/utils/api'
import { formatPriceWithSymbol, formatDateTime, getOrderStatusLabel, getOrderStatusColor } from '@/utils/helpers'
import type { Order, OrderAdjustment } from '@/types'

const props = defineProps<{ id: string }>()

// Helper functions
const timelineNodes = computed(() => {
    if (!order.value) return []
    const nodes = [
        { title: '下单成功', time: formatDateTime(order.value.created_at), active: true }
    ]

    if (['paid', 'confirmed', 'preparing', 'shipped', 'delivering', 'completed'].includes(order.value.status)) {
        nodes.push({ title: '支付成功', time: formatDateTime(order.value.payment_verified_at || order.value.updated_at), active: true, desc: `支付方式：${order.value.payment_provider_name || '微信支付'}` })
    }

    if (['confirmed', 'preparing', 'shipped', 'delivering', 'completed'].includes(order.value.status)) {
        nodes.push({ title: '商家确认', time: formatDateTime(order.value.updated_at), active: true })
    }

    if (['preparing', 'shipped', 'delivering', 'completed'].includes(order.value.status)) {
        nodes.push({ title: '备货中', time: formatDateTime(order.value.updated_at), active: true })
    }

    if (['shipped', 'delivering', 'completed'].includes(order.value.status)) {
        nodes.push({ title: '已发货', time: formatDateTime(order.value.updated_at), active: true, desc: order.value.promised_delivery_at ? `预计送达：${formatDateTime(order.value.promised_delivery_at)}` : '' })
    }

    if (['delivering', 'completed'].includes(order.value.status)) {
        nodes.push({ title: '配送中', time: formatDateTime(order.value.updated_at), active: true })
    }

    if (['completed'].includes(order.value.status)) {
        nodes.push({ title: '已完成', time: formatDateTime(order.value.updated_at), active: true, desc: '感谢您的惠顾，欢迎再次光临！' })
    }

    if (['cancelled', 'refunded'].includes(order.value.status)) {
        nodes.push({ title: order.value.status === 'cancelled' ? '已取消' : '已退款', time: formatDateTime(order.value.updated_at), active: true })
    }

    return nodes
})

function getItemImage(item: any): string {
    return '/static/images/placeholder.png'
}

function formatOptionText(optionValues: Record<string, any>): string {
    if (!optionValues) return ''
    return Object.values(optionValues).filter(v => v !== null && v !== undefined && v !== '').map(v => String(v)).join(' / ')
}

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

const order = ref<Order | null>(null)
const adjustments = ref<OrderAdjustment[]>([])
const loading = ref(true)
const showLogistics = ref(false)

async function fetchOrder() {
    loading.value = true
    try {
        const res = await orderApi.getMyOrders()
        const found = res.find(o => o.id === props.id)
        if (found) {
            order.value = found
            // 获取补差记录（称重商品多退少补）
            try {
                const adjRes = await orderApi.getOrderAdjustments(props.id)
                adjustments.value = adjRes
            } catch (e) {
                console.warn('获取补差记录失败:', e)
            }
        } else {
            uni.showToast({ title: '订单不存在', icon: 'none' })
            setTimeout(() => uni.navigateBack(), 1500)
        }
    } catch (e) {
        console.error('获取订单详情失败:', e)
        uni.showToast({ title: '加载失败', icon: 'none' })
    } finally {
        loading.value = false
    }
}

function goPay() {
    if (order.value) {
        uni.navigateTo({ url: `/pages/checkout/checkout?order_id=${order.value.id}` })
    }
}

function copyOrderNo() {
    if (order.value) {
        uni.setClipboardData({ data: order.value.id })
        uni.showToast({ title: '订单号已复制', icon: 'success' })
    }
}

function formatAdjustType(type: string): string {
    return type === 'supplement' ? '补差' : '退差'
}

function getAdjustColor(type: string): string {
    return type === 'supplement' ? '#F44336' : '#4CAF50'
}

onMounted(() => {
    fetchOrder()
})
</script>

<template>
    <view class="page">
        <scroll-view class="detail-scroll" scroll-y :style="{ height: '100vh' }" v-if="order">
            <!-- 订单状态头部 -->
            <view class="status-header" :style="{ background: getOrderStatusColor(order.status) }">
                <view class="status-main">
                    <text class="status-label">{{ getOrderStatusLabel(order.status) }}</text>
                    <text class="status-id">订单号：{{ order.id }}</text>
                </view>
                <view class="status-actions">
                    <button class="btn-copy" @tap="copyOrderNo">复制单号</button>
                    <button class="btn-pay" v-if="['pending', 'payment_pending'].includes(order.status)" @tap="goPay">去支付</button>
                </view>
            </view>

            <!-- 商品列表 -->
            <view class="section">
                <view class="section-title">商品 ({{ order.line_items.length }})</view>
                <view class="product-list">
                    <view class="product-item" v-for="item in order.line_items" :key="item.id">
                        <image class="product-image" :src="getItemImage(item)" mode="aspectFill" />
                        <view class="product-info">
                            <text class="product-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ item.product_title }}</text>
                            <text class="product-spec" v-if="item.option_values && Object.keys(item.option_values).length > 0">{{ formatOptionText(item.option_values) }}</text>
                            <view class="product-price-qty">
                                <text class="product-price">{{ formatPriceWithSymbol(item.unit_price_cents) }}</text>
                                <text class="product-qty">x {{ item.quantity }}</text>
                            </view>
                            <view class="product-preorder" v-if="item.preorder_qty > 0">
                                <text class="preorder-label">含预订 {{ item.preorder_qty }} 份</text>
                            </view>
                        </view>
                    </view>
                </view>
            </view>

            <!-- 补差记录（称重商品多退少补） -->
            <view class="section" v-if="adjustments.length > 0">
                <view class="section-title">补差记录（称重商品多退少补）</view>
                <view class="adjustment-list">
                    <view class="adjustment-item" v-for="adj in adjustments" :key="adj.id">
                        <view class="adj-main">
                            <view class="adj-type" :style="{ color: getAdjustColor(adj.adjust_type) }">
                                {{ formatAdjustType(adj.adjust_type) }}
                            </view>
                            <view class="adj-detail">
                                <text class="adj-weight">{{ adj.weight_grams }}g</text>
                                <text class="adj-amount" :style="{ color: getAdjustColor(adj.adjust_type) }">
                                    {{ adj.adjust_type === 'supplement' ? '+' : '-' }}{{ formatPriceWithSymbol(adj.amount_cents) }}
                                </text>
                            </view>
                        </view>
                        <text class="adj-time">{{ formatDateTime(adj.created_at, 'MM-DD HH:mm') }}</text>
                    </view>
                </view>
            </view>

            <!-- 收货地址 -->
            <view class="section" v-if="order.shipping_address">
                <view class="section-title">收货地址</view>
                <view class="address-card">
                    <view class="address-tag" v-if="order.shipping_address.region_code === 'pickup'">自提</view>
                    <view class="address-tag delivery" v-else>配送</view>
                    <view class="address-info">
                        <view class="address-name-phone">
                            <text class="address-name">{{ order.shipping_address.recipient_name }}</text>
                            <text class="address-phone">{{ order.shipping_address.phone }}</text>
                        </view>
                        <text class="address-detail">
                            {{ order.shipping_address.city }}{{ order.shipping_address.address_line1 }}{{ order.shipping_address.address_line2 }}
                        </text>
                    </view>
                </view>
            </view>

            <!-- 价格明细 -->
            <view class="section price-detail">
                <view class="price-row">
                    <text>商品小计</text>
                    <text>{{ formatPriceWithSymbol(order.subtotal_cents) }}</text>
                </view>
                <view class="price-row" v-if="order.discount_cents > 0">
                    <text>优惠</text>
                    <text class="discount">-{{ formatPriceWithSymbol(order.discount_cents) }}</text>
                </view>
                <view class="price-row" v-if="order.shipping_cents > 0">
                    <text>配送费</text>
                    <text>{{ formatPriceWithSymbol(order.shipping_cents) }}</text>
                </view>
                <view class="price-row" v-if="order.tax_cents > 0">
                    <text>税费</text>
                    <text>{{ formatPriceWithSymbol(order.tax_cents) }}</text>
                </view>
                <view class="price-row total">
                    <text>实付款</text>
                    <text>{{ formatPriceWithSymbol(order.grand_total_cents) }}</text>
                </view>
            </view>

            <!-- 订单时间线 -->
            <view class="section">
                <view class="section-title">订单时间线</view>
                <view class="timeline">
                    <view class="timeline-item" v-for="(node, index) in timelineNodes" :key="index" :class="{ last: index === timelineNodes.length - 1 }">
                        <view class="timeline-dot" :class="{ active: node.active }" />
                        <view class="timeline-content">
                            <text class="timeline-title">{{ node.title }}</text>
                            <text class="timeline-time">{{ node.time }}</text>
                            <text class="timeline-desc" v-if="node.desc">{{ node.desc }}</text>
                        </view>
                    </view>
                </view>
            </view>

            <view class="bottom-placeholder" />
        </scroll-view>

        <view class="loading-container" v-else>
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

.detail-scroll {
    flex: 1;
}

.status-header {
    padding: 20px 16px;
    color: #FFFFFF;
}

.status-main {
    margin-bottom: 16px;
}

.status-label {
    display: block;
    font-size: 18px;
    font-weight: 600;
    margin-bottom: 8px;
}

.status-id {
    font-size: 13px;
    opacity: 0.9;
}

.status-actions {
    display: flex;
    gap: 12px;
}

.btn-copy,
.btn-pay {
    padding: 8px 16px;
    border-radius: 20px;
    font-size: 13px;
    font-weight: 500;
    border: none;
}

.btn-copy {
    background: rgba(255, 255, 255, 0.2);
    color: #FFFFFF;
}

.btn-pay {
    background: #FFFFFF;
    color: #2E7D32;
}

.section {
    background: #FFFFFF;
    margin: 8px 16px;
    border-radius: 12px;
    padding: 16px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
}

.section-title {
    font-size: 15px;
    font-weight: 600;
    color: #1A1A1A;
    margin-bottom: 12px;
}

.product-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.product-item {
    display: flex;
    gap: 12px;
}

.product-image {
    width: 70px;
    height: 70px;
    border-radius: 8px;
    object-fit: cover;
}

.product-info {
    flex: 1;
    min-width: 0;
}

.product-title {
    font-size: 14px;
    color: #1A1A1A;
    line-height: 1.4;
    margin-bottom: 4px;
}

.product-spec {
    font-size: 12px;
    color: #9E9E9E;
    margin-bottom: 4px;
}

.product-price-qty {
    display: flex;
    align-items: center;
    gap: 12px;
}

.product-price {
    font-size: 14px;
    font-weight: 500;
    color: #2E7D32;
}

.product-qty {
    font-size: 13px;
    color: #9E9E9E;
}

.product-preorder {
    margin-top: 4px;
}

.preorder-label {
    font-size: 11px;
    color: #E65100;
    background: #FFF3E0;
    padding: 2px 6px;
    border-radius: 3px;
}

/* 补差记录 */
.adjustment-list {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.adjustment-item {
    display: flex;
    justify-content: space-between;
    align-items: center;
    padding: 12px;
    background: #FAFAFA;
    border-radius: 8px;
}

.adj-main {
    display: flex;
    align-items: center;
    gap: 12px;
}

.adj-type {
    font-size: 12px;
    font-weight: 600;
    padding: 2px 8px;
    border-radius: 4px;
    background: rgba(0, 0, 0, 0.05);
}

.adj-detail {
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.adj-weight {
    font-size: 12px;
    color: #616161;
}

.adj-amount {
    font-size: 14px;
    font-weight: 600;
}

.adj-time {
    font-size: 12px;
    color: #9E9E9E;
}

/* 地址卡片 */
.address-card {
    display: flex;
    align-items: flex-start;
    gap: 12px;
}

.address-tag {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
    margin-top: 2px;
}

.address-tag:not(.delivery) {
    background: #E8F5E9;
    color: #2E7D32;
}

.address-tag.delivery {
    background: #E3F2FD;
    color: #1565C0;
}

.address-info {
    flex: 1;
}

.address-name-phone {
    display: flex;
    align-items: center;
    gap: 8px;
    margin-bottom: 4px;
}

.address-name {
    font-size: 14px;
    font-weight: 500;
    color: #1A1A1A;
}

.address-phone {
    font-size: 14px;
    color: #616161;
}

.address-detail {
    font-size: 13px;
    color: #616161;
    line-height: 1.5;
}

/* 价格明细 */
.price-detail {
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
}

.price-row {
    display: flex;
    justify-content: space-between;
    font-size: 14px;
    color: #616161;
    margin-bottom: 8px;
}

.price-row.total {
    margin-top: 8px;
    padding-top: 8px;
    border-top: 1px solid #F5F5F5;
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
}

.price-row .discount {
    color: #F44336;
}

/* 时间线 */
.timeline {
    position: relative;
    padding-left: 16px;
}

.timeline::before {
    content: '';
    position: absolute;
    left: 7px;
    top: 0;
    bottom: 0;
    width: 2px;
    background: #EEEEEE;
}

.timeline-item {
    position: relative;
    display: flex;
    padding: 12px 0 12px 16px;
}

.timeline-item.last::before {
    content: '';
    position: absolute;
    left: 7px;
    top: 50%;
    bottom: 0;
    width: 2px;
    background: #FFFFFF;
}

.timeline-dot {
    position: absolute;
    left: 0;
    top: 16px;
    width: 16px;
    height: 16px;
    border-radius: 50%;
    background: #FFFFFF;
    border: 2px solid #EEEEEE;
    z-index: 1;
}

.timeline-dot.active {
    border-color: #2E7D32;
    background: #2E7D32;
}

.timeline-content {
    flex: 1;
    min-width: 0;
}

.timeline-title {
    display: block;
    font-size: 14px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 2px;
}

.timeline-time {
    display: block;
    font-size: 12px;
    color: #9E9E9E;
    margin-bottom: 2px;
}

.timeline-desc {
    display: block;
    font-size: 12px;
    color: #616161;
}

.bottom-placeholder {
    height: 40px;
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
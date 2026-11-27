<script setup lang="ts">
import { ref, onMounted, onUnmounted, computed } from 'vue'
import { orderApi, adminApi } from '@/utils/api'
import { formatPriceWithSymbol, formatDateTime } from '@/utils/helpers'
import type { Order, OrderLineItem } from '@/types'

const scanning = ref(false)
const manualMode = ref(false)
const pickupCode = ref('')
const order = ref<Order | null>(null)
const loading = ref(false)
const verifying = ref(false)
let lastScanTime = 0
const scanCooldown = 2000 // 2秒冷却

const torchOn = ref(false)
const scanFrame = ref<UniApp.CanvasContext | null>(null)

// 扫码相关
let scanInterval: number | null = null

async function fetchOrderByPickupCode(code: string) {
  if (!code.trim()) return

  loading.value = true
  try {
    // 通过取货码查询订单
    const res = await orderApi.lookupOrderByPickupCode(code)
    order.value = res
  } catch (e: any) {
    order.value = null
    uni.showToast({ title: e.message || '未找到该取货码对应的订单', icon: 'none' })
  } finally {
    loading.value = false
  }
}

async function verifyPickup() {
  if (!order.value || !order.value.pickup_code) {
    uni.showToast({ title: '无效的订单', icon: 'none' })
    return
  }

  if (order.value.status === 'completed') {
    uni.showModal({
      title: '订单已完成',
      content: '该订单已于 ' + formatDateTime(order.value.updated_at) + ' 核销完成，是否重新核销？',
      confirmText: '重新核销',
      cancelText: '取消',
      success: async (res) => {
        if (res.confirm) {
          await doVerify()
        }
      },
    })
    return
  }

  if (order.value.status !== 'paid' && order.value.status !== 'confirmed' && order.value.status !== 'preparing') {
    uni.showToast({ title: `订单状态为 ${order.value.status}，无法核销`, icon: 'none' })
    return
  }

  await doVerify()
}

async function doVerify() {
  if (!order.value) return

  verifying.value = true
  try {
    await adminApi.verifyPickup(order.value.id)
    uni.showToast({ title: '核销成功，订单已完成', icon: 'success' })
    // 刷新订单状态
    await fetchOrderByPickupCode(order.value.pickup_code)
  } catch (e: any) {
    uni.showToast({ title: e.message || '核销失败', icon: 'none' })
  } finally {
    verifying.value = false
  }
}

function onPickupCodeInput(e: any) {
  pickupCode.value = e.detail.value.toUpperCase()
}

function onPickupCodeConfirm() {
  if (pickupCode.value.length >= 6) {
    fetchOrderByPickupCode(pickupCode.value)
  }
}

function clearPickupCode() {
  pickupCode.value = ''
  order.value = null
}

function toggleManualMode() {
  manualMode.value = !manualMode.value
  if (!manualMode.value) {
    stopScan()
  }
}

function toggleTorch() {
  torchOn.value = !torchOn.value
  // 注意：微信小程序扫码不支持直接控制手电筒，这里仅作状态标记
  uni.showToast({ title: torchOn.value ? '手电筒已开启' : '手电筒已关闭', icon: 'none' })
}

function startScan() {
  if (scanning.value) return

  const now = Date.now()
  if (now - lastScanTime < scanCooldown) return
  lastScanTime = now

  scanning.value = true

  uni.scanCode({
    onlyFromCamera: true,
    scanType: ['qrCode', 'barCode'],
    success: (res) => {
      const code = res.result
      // 解析二维码内容，可能是纯取货码或包含订单ID的 JSON
      try {
        const parsed = JSON.parse(code)
        if (parsed.pickup_code) {
          pickupCode.value = parsed.pickup_code
        } else if (parsed.order_id) {
          // 通过订单ID查询
          fetchOrderByPickupCode(parsed.pickup_code || '')
        } else {
          pickupCode.value = code
        }
      } catch {
        // 纯文本取货码
        pickupCode.value = code
      }
      if (pickupCode.value) {
        fetchOrderByPickupCode(pickupCode.value)
      }
    },
    fail: (err) => {
      if (err.errMsg && !err.errMsg.includes('cancel')) {
        console.warn('扫码失败:', err)
      }
    },
    complete: () => {
      scanning.value = false
    },
  })
}

function stopScan() {
  scanning.value = false
}

function copyOrderNo() {
  if (order.value) {
    uni.setClipboardData({ data: order.value.id })
    uni.showToast({ title: '订单号已复制', icon: 'success' })
  }
}

function copyPickupCode() {
  if (order.value?.pickup_code) {
    uni.setClipboardData({ data: order.value.pickup_code })
    uni.showToast({ title: '取货码已复制', icon: 'success' })
  }
}

function formatOptionText(optionValues: Record<string, any>): string {
  if (!optionValues) return ''
  return Object.values(optionValues).filter(v => v !== null && v !== undefined && v !== '').map(v => String(v)).join(' / ')
}

function getOrderStatusLabel(status: string): string {
  const labels: Record<string, string> = {
    pending: '待付款',
    payment_pending: '待付款',
    paid: '已付款',
    confirmed: '已确认',
    preparing: '备货中',
    shipped: '已发货',
    delivering: '配送中',
    completed: '已完成',
    cancelled: '已取消',
    refunded: '已退款',
  }
  return labels[status] || status
}

function getOrderStatusColor(status: string): string {
  const colors: Record<string, string> = {
    pending: '#FF9800',
    payment_pending: '#FF9800',
    paid: '#2196F3',
    confirmed: '#2196F3',
    preparing: '#2196F3',
    shipped: '#9C27B0',
    delivering: '#9C27B0',
    completed: '#4CAF50',
    cancelled: '#9E9E9E',
    refunded: '#9E9E9E',
  }
  return colors[status] || '#9E9E9E'
}

onMounted(() => {
  // 默认开启扫码模式
})

onUnmounted(() => {
  stopScan()
})
</script>

<template>
  <view class="page">
    <!-- 顶部模式切换 -->
    <view class="mode-bar">
      <view class="mode-item" :class="{ active: !manualMode }" @tap="toggleManualMode">
        <text class="mode-icon">📷</text>
        <text class="mode-label">扫码核销</text>
      </view>
      <view class="mode-item" :class="{ active: manualMode }" @tap="toggleManualMode">
        <text class="mode-icon">⌨️</text>
        <text class="mode-label">手动输入</text>
      </view>
    </view>

    <!-- 扫码/输入区域 -->
    <view class="scan-section" v-if="!manualMode">
      <view class="scan-area" @tap="startScan">
        <view class="scan-frame">
          <view class="scan-line" />
        </view>
        <text class="scan-hint">对准取货码二维码扫描</text>
        <text class="scan-hint-sub">或点击切换手动输入模式</text>
      </view>
      <button class="btn-torch" @tap.stop="toggleTorch">
        <text>{{ torchOn ? '🔦 关闭手电筒' : '🔦 开启手电筒' }}</text>
      </button>
    </view>

    <view class="manual-section" v-if="manualMode">
      <view class="input-wrapper">
        <input
          class="pickup-code-input"
          type="text"
          v-model="pickupCode"
          @input="onPickupCodeInput"
          @confirm="onPickupCodeConfirm"
          placeholder="请输入 6 位取货码"
          maxlength="10"
        />
        <button class="btn-search" @tap="onPickupCodeConfirm" :disabled="loading || pickupCode.length < 6">
          <text v-if="!loading">查询</text>
          <text v-else>查询中...</text>
        </button>
      </view>
      <view class="manual-hint">
        取货码为 6 位数字，可在顾客订单详情或自提通知中查看
      </view>
    </view>

    <!-- 订单详情 -->
    <scroll-view class="detail-scroll" scroll-y :style="{ height: 'calc(100vh - 300px)' }" v-if="order">
      <!-- 订单状态头部 -->
      <view class="status-header" :style="{ background: getOrderStatusColor(order.status) }">
        <view class="status-main">
          <text class="status-label">{{ getOrderStatusLabel(order.status) }}</text>
          <text class="status-id">订单号：{{ order.id }}</text>
        </view>
        <view class="status-actions">
          <button class="btn-copy" @tap="copyOrderNo">复制单号</button>
          <button class="btn-copy" @tap="copyPickupCode">复制取货码</button>
        </view>
      </view>

      <!-- 自提信息 -->
      <view class="section">
        <view class="section-title">自提信息</view>
        <view class="pickup-info" v-if="order.pickup_point">
          <view class="pickup-point">
            <text class="pickup-point-label">自提门店</text>
            <text class="pickup-point-name">{{ order.pickup_point.name }}</text>
            <text class="pickup-point-address">{{ order.pickup_point.address }}</text>
            <text class="pickup-point-phone">{{ order.pickup_point.phone }}</text>
          </view>
          <view class="pickup-time" v-if="order.pickup_time_slot">
            <text class="pickup-time-label">自提时段</text>
            <text class="pickup-time-value">{{ order.pickup_time_slot.start }} - {{ order.pickup_time_slot.end }}</text>
          </view>
          <view class="pickup-date" v-if="order.pickup_date">
            <text class="pickup-date-label">自提日期</text>
            <text class="pickup-date-value">{{ order.pickup_date }}</text>
          </view>
        </view>
      </view>

      <!-- 取货码展示 -->
      <view class="section">
        <view class="section-title">取货码</view>
        <view class="pickup-code-display">
          <text class="pickup-code-value">{{ order.pickup_code }}</text>
        </view>
      </view>

      <!-- 商品列表 -->
      <view class="section">
        <view class="section-title">商品清单 ({{ order.line_items.length }})</view>
        <view class="product-list">
          <view class="product-item" v-for="item in order.line_items" :key="item.id">
            <image class="product-image" :src="'/static/images/placeholder.png'" mode="aspectFill" />
            <view class="product-info">
              <text class="product-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ item.product_title }}</text>
              <text class="product-spec" v-if="item.option_values && Object.keys(item.option_values).length > 0">{{ formatOptionText(item.option_values) }}</text>
              <view class="product-price-qty">
                <text class="product-price">{{ formatPriceWithSymbol(item.unit_price_cents) }}</text>
                <text class="product-qty">x {{ item.quantity }}</text>
              </view>
            </view>
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
        <view class="price-row total">
          <text>实付款</text>
          <text>{{ formatPriceWithSymbol(order.grand_total_cents) }}</text>
        </view>
      </view>

      <!-- 核销按钮 -->
      <view class="section verify-section">
        <button
          class="btn-verify"
          @tap="verifyPickup"
          :disabled="verifying || order.status === 'completed'"
        >
          <text v-if="!verifying && order.status !== 'completed'">核销完成订单</text>
          <text v-if="verifying">核销中...</text>
          <text v-if="order.status === 'completed'">订单已完成（{{ formatDateTime(order.updated_at) }} 核销）</text>
        </button>
      </view>
    </scroll-view>

    <!-- 空状态 -->
    <view class="empty-state" v-if="!order && !loading && !scanning">
      <image class="empty-icon" src="/static/images/empty-scan.png" mode="aspectFit" />
      <text class="empty-text" v-if="!manualMode">扫描顾客取货码二维码</text>
      <text class="empty-text" v-else>输入取货码查询订单</text>
      <text class="empty-subtext">核销后订单状态变为已完成</text>
    </view>

    <view class="loading-container" v-if="loading">
      <uni-loading-icon />
      <text>查询中...</text>
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

/* 模式切换栏 */
.mode-bar {
  display: flex;
  background: #FFFFFF;
  border-bottom: 1px solid #EEEEEE;
}

.mode-item {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  padding: 16px 12px;
  gap: 4px;
  color: #9E9E9E;
}

.mode-item.active {
  color: #2E7D32;
  border-bottom: 2px solid #2E7D32;
}

.mode-icon {
  font-size: 20px;
}

.mode-label {
  font-size: 13px;
  font-weight: 500;
}

/* 扫码区域 */
.scan-section {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 24px 16px;
  gap: 24px;
}

.scan-area {
  width: 280px;
  height: 280px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
}

.scan-frame {
  position: relative;
  width: 240px;
  height: 240px;
  border: 2px solid #2E7D32;
  border-radius: 12px;
  overflow: hidden;
}

.scan-line {
  position: absolute;
  left: 0;
  right: 0;
  height: 2px;
  background: linear-gradient(90deg, transparent, #2E7D32, transparent);
  animation: scanMove 2s linear infinite;
}

@keyframes scanMove {
  0% { top: 0; }
  100% { top: 238px; }
}

.scan-hint {
  margin-top: 16px;
  font-size: 15px;
  color: #1A1A1A;
  font-weight: 500;
}

.scan-hint-sub {
  font-size: 12px;
  color: #9E9E9E;
}

.btn-torch {
  padding: 10px 24px;
  background: #FFFFFF;
  border: 1px solid #E0E0E0;
  border-radius: 24px;
  font-size: 14px;
  color: #616161;
}

/* 手动输入区域 */
.manual-section {
  padding: 24px 16px;
}

.input-wrapper {
  display: flex;
  gap: 12px;
  margin-bottom: 12px;
}

.pickup-code-input {
  flex: 1;
  height: 50px;
  border: 1px solid #E0E0E0;
  border-radius: 8px;
  padding: 0 16px;
  font-size: 24px;
  letter-spacing: 8px;
  text-align: center;
  color: #1A1A1A;
  background: #FFFFFF;
}

.pickup-code-input::placeholder {
  color: #BDBDBD;
  letter-spacing: normal;
  font-size: 14px;
  text-align: left;
  padding-left: 8px;
}

.btn-search {
  width: 100px;
  height: 50px;
  background: linear-gradient(135deg, #2E7D32, #4CAF50);
  color: #FFFFFF;
  border: none;
  border-radius: 8px;
  font-size: 16px;
  font-weight: 600;
}

.btn-search:disabled {
  background: #A5D6A7;
}

.manual-hint {
  font-size: 12px;
  color: #9E9E9E;
  text-align: center;
}

/* 订单详情 */
.detail-scroll {
  flex: 1;
  margin-top: 8px;
}

.status-header {
  padding: 20px 16px;
  color: #FFFFFF;
  margin: 8px 16px 0;
  border-radius: 12px;
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
  font-family: monospace;
}

.status-actions {
  display: flex;
  gap: 12px;
}

.btn-copy {
  padding: 8px 16px;
  border-radius: 20px;
  font-size: 13px;
  font-weight: 500;
  border: none;
  background: rgba(255, 255, 255, 0.2);
  color: #FFFFFF;
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

/* 自提信息 */
.pickup-info {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.pickup-point,
.pickup-time,
.pickup-date {
  padding: 12px;
  background: #FAFAFA;
  border-radius: 8px;
}

.pickup-point-label,
.pickup-time-label,
.pickup-date-label {
  display: block;
  font-size: 12px;
  color: #9E9E9E;
  margin-bottom: 4px;
}

.pickup-point-name {
  display: block;
  font-size: 15px;
  font-weight: 500;
  color: #1A1A1A;
  margin-bottom: 2px;
}

.pickup-point-address,
.pickup-point-phone {
  display: block;
  font-size: 12px;
  color: #616161;
  margin-bottom: 2px;
}

.pickup-time-value,
.pickup-date-value {
  font-size: 15px;
  font-weight: 500;
  color: #1A1A1A;
}

/* 取货码展示 */
.pickup-code-display {
  padding: 16px;
  background: #FAFAFA;
  border-radius: 8px;
  text-align: center;
}

.pickup-code-value {
  font-size: 32px;
  font-weight: 700;
  letter-spacing: 8px;
  color: #2E7D32;
  font-variant-numeric: tabular-nums;
  font-family: monospace;
}

/* 商品列表 */
.product-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.product-item {
  display: flex;
  gap: 12px;
  padding: 8px 0;
  border-bottom: 1px solid #F5F5F5;
}

.product-item:last-child {
  border-bottom: none;
}

.product-image {
  width: 60px;
  height: 60px;
  border-radius: 8px;
  object-fit: cover;
}

.product-info {
  flex: 1;
  min-width: 0;
}

.product-title {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 14px;
  color: #1A1A1A;
  line-height: 1.4;
  margin-bottom: 4px;
}

.product-spec {
  display: block;
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

/* 核销按钮 */
.verify-section {
  padding-top: 0;
}

.btn-verify {
  width: 100%;
  height: 52px;
  background: linear-gradient(135deg, #2E7D32, #4CAF50);
  color: #FFFFFF;
  border: none;
  border-radius: 12px;
  font-size: 17px;
  font-weight: 600;
  box-shadow: 0 4px 12px rgba(46, 125, 50, 0.3);
}

.btn-verify:disabled {
  background: #A5D6A7;
  box-shadow: none;
}

/* 空状态 */
.empty-state {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  padding: 40px 24px;
  color: #9E9E9E;
}

.empty-icon {
  width: 120px;
  height: 120px;
  margin-bottom: 20px;
  opacity: 0.5;
}

.empty-text {
  font-size: 18px;
  font-weight: 500;
  margin-bottom: 8px;
  color: #616161;
}

.empty-subtext {
  font-size: 13px;
}

/* 加载状态 */
.loading-container {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 16px;
  color: #9E9E9E;
}
</style>
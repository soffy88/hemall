<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { orderApi } from '@/utils/api'
import { formatPriceWithSymbol, formatDateTime } from '@/utils/helpers'
import type { Order, OrderLineItem } from '@/types'

const props = defineProps<{ orderId?: string; lineItemId?: string }>()

const order = ref<Order | null>(null)
const selectedItem = ref<OrderLineItem | null>(null)
const loading = ref(true)
const submitting = ref(false)

const form = ref({
  reason: '',
  description: '',
  images: [] as string[],
  quantity: 1,
})

const reasons = [
  { value: 'quality', label: '质量问题（变质、破损、异物等）' },
  { value: 'shortage', label: '少发/漏发' },
  { value: 'wrong_item', label: '发错商品' },
  { value: 'other', label: '其他原因' },
]

const maxImages = 3
const maxFileSize = 5 * 1024 * 1024 // 5MB

async function fetchOrder() {
  loading.value = true
  try {
    const res = await orderApi.getMyOrders()
    const found = res.find(o => o.id === props.orderId)
    if (found) {
      order.value = found
      if (props.lineItemId) {
        const item = found.line_items.find(i => i.id === props.lineItemId)
        if (item) {
          selectedItem.value = item
          form.value.quantity = item.quantity
        }
      } else if (found.line_items.length > 0) {
        selectedItem.value = found.line_items[0]
        form.value.quantity = found.line_items[0].quantity
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

function selectItem(item: OrderLineItem) {
  selectedItem.value = item
  form.value.quantity = item.quantity
}

function selectReason(reason: string) {
  form.value.reason = reason
}

async function chooseImage() {
  if (form.value.images.length >= maxImages) {
    uni.showToast({ title: `最多上传 ${maxImages} 张图片`, icon: 'none' })
    return
  }

  try {
    const res = await uni.chooseImage({
      count: maxImages - form.value.images.length,
      sizeType: ['compressed'],
      sourceType: ['album', 'camera'],
    })

    for (const tempPath of res.tempFilePaths) {
      const fileInfo = await uni.getFileInfo({ filePath: tempPath })
      if (fileInfo.size > maxFileSize) {
        uni.showToast({ title: '图片大小不能超过 5MB', icon: 'none' })
        continue
      }
      form.value.images.push(tempPath)
    }
  } catch (e) {
    console.error('选择图片失败:', e)
  }
}

function removeImage(index: number) {
  form.value.images.splice(index, 1)
}

function previewImage(index: number) {
  uni.previewImage({
    urls: form.value.images,
    current: index,
  })
}

function validateForm(): boolean {
  if (!selectedItem.value) {
    uni.showToast({ title: '请选择商品', icon: 'none' })
    return false
  }
  if (!form.value.reason) {
    uni.showToast({ title: '请选择售后原因', icon: 'none' })
    return false
  }
  if (!form.value.description.trim()) {
    uni.showToast({ title: '请填写问题描述', icon: 'none' })
    return false
  }
  if (form.value.images.length === 0) {
    uni.showToast({ title: '请上传问题商品照片（至少1张）', icon: 'none' })
    return false
  }
  if (form.value.quantity <= 0 || form.value.quantity > (selectedItem.value?.quantity || 0)) {
    uni.showToast({ title: '申请数量不正确', icon: 'none' })
    return false
  }
  return true
}

async function submitClaim() {
  if (!validateForm()) return

  submitting.value = true
  try {
    // 上传图片
    const imageUrls: string[] = []
    for (const tempPath of form.value.images) {
      try {
        const uploadRes = await uni.uploadFile({
          url: `${import.meta.env.VITE_API_BASE_URL || ''}/store/upload/image`,
          filePath: tempPath,
          name: 'file',
          header: {
            'Authorization': `Bearer ${uni.getStorageSync('access_token')}`,
          },
        })
        const data = JSON.parse(uploadRes.data)
        if (data.url) {
          imageUrls.push(data.url)
        }
      } catch (e) {
        console.error('上传图片失败:', e)
      }
    }

    // 提交售后申请
    await orderApi.submitAftersalesClaim({
      order_id: props.orderId!,
      line_item_id: selectedItem.value!.id,
      reason: form.value.reason,
      description: form.value.description,
      images: imageUrls,
      quantity: form.value.quantity,
    })

    uni.showToast({ title: '售后申请已提交，客服将在 24 小时内处理', icon: 'success' })
    setTimeout(() => {
      uni.navigateBack()
    }, 2000)
  } catch (e: any) {
    uni.showToast({ title: e.message || '提交失败，请重试', icon: 'none' })
  } finally {
    submitting.value = false
  }
}

onMounted(() => {
  fetchOrder()
})
</script>

<template>
  <view class="page">
    <scroll-view class="apply-scroll" scroll-y :style="{ height: '100vh' }" v-if="!loading && order">
      <!-- 订单信息 -->
      <view class="section order-info">
        <text class="section-title">订单信息</text>
        <view class="order-detail">
          <text class="order-no">订单号：{{ order.id }}</text>
          <text class="order-time">{{ formatDateTime(order.created_at) }}</text>
          <text class="order-status">{{ order.status }}</text>
        </view>
      </view>

      <!-- 选择商品 -->
      <view class="section">
        <text class="section-title">选择售后商品</text>
        <view class="item-list">
          <view
            class="item-card"
            v-for="item in order.line_items"
            :key="item.id"
            :class="{ selected: selectedItem?.id === item.id }"
            @tap="selectItem(item)"
          >
            <image class="item-image" :src="'/static/images/placeholder.png'" mode="aspectFill" />
            <view class="item-info">
              <text class="item-title" style="-webkit-line-clamp: 2; display: -webkit-box; -webkit-box-orient: vertical; overflow: hidden;">{{ item.product_title }}</text>
              <text class="item-spec" v-if="item.option_values && Object.keys(item.option_values).length > 0">
                {{ Object.values(item.option_values).join(' / ') }}
              </text>
              <view class="item-price-qty">
                <text class="item-price">{{ formatPriceWithSymbol(item.unit_price_cents) }}</text>
                <text class="item-qty">x {{ item.quantity }}</text>
              </view>
            </view>
            <view class="item-check" v-if="selectedItem?.id === item.id">✓</view>
          </view>
        </view>
      </view>

      <!-- 售后原因 -->
      <view class="section">
        <text class="section-title">售后原因 <text class="required">*</text></text>
        <view class="reason-list">
          <view
            class="reason-item"
            v-for="reason in reasons"
            :key="reason.value"
            :class="{ selected: form.reason === reason.value }"
            @tap="selectReason(reason.value)"
          >
            <text class="reason-label">{{ reason.label }}</text>
            <view class="reason-radio" :class="{ checked: form.reason === reason.value }">
              <view class="radio-inner" v-if="form.reason === reason.value" />
            </view>
          </view>
        </view>
      </view>

      <!-- 问题描述 -->
      <view class="section">
        <text class="section-title">问题描述 <text class="required">*</text></text>
        <textarea
          class="description-input"
          v-model="form.description"
          placeholder="请详细描述商品问题（如：收到时包装破损、苹果有压伤、重量不足等）"
          maxlength="500"
          auto-height
        />
        <text class="char-count">{{ form.description.length }}/500</text>
      </view>

      <!-- 申请数量 -->
      <view class="section" v-if="selectedItem">
        <text class="section-title">申请数量 <text class="required">*</text></text>
        <view class="quantity-selector">
          <button class="qty-btn" @tap="form.quantity = Math.max(1, form.quantity - 1)" :disabled="form.quantity <= 1">-</button>
          <text class="qty-value">{{ form.quantity }}</text>
          <button class="qty-btn" @tap="form.quantity = Math.min(selectedItem.quantity, form.quantity + 1)" :disabled="form.quantity >= selectedItem.quantity">+</button>
          <text class="qty-max">最多可申请 {{ selectedItem.quantity }} 份</text>
        </view>
      </view>

      <!-- 上传图片 -->
      <view class="section">
        <text class="section-title">上传凭证 <text class="required">*</text></text>
        <text class="image-hint">请上传问题商品照片（至少1张，最多{{ maxImages }}张，单张不超过5MB）</text>
        <view class="image-grid">
          <view
            class="image-item"
            v-for="(img, index) in form.images"
            :key="index"
            @tap="previewImage(index)"
          >
            <image class="image-thumb" :src="img" mode="aspectFill" />
            <view class="image-remove" @tap.stop="removeImage(index)">✕</view>
          </view>
          <view class="image-add" @tap="chooseImage" v-if="form.images.length < maxImages">
            <text class="add-icon">+</text>
            <text class="add-text">添加图片</text>
          </view>
        </view>
      </view>

      <!-- 温馨提示 -->
      <view class="section tips">
        <text class="tips-title">温馨提示</text>
        <view class="tips-list">
          <text class="tip-item">1. 请在收货后 48 小时内申请售后，过期可能无法受理</text>
          <text class="tip-item">2. 请保留原包装和商品，以便核实</text>
          <text class="tip-item">3. 质量问题请拍摄清晰的特写照片（正面、背面、细节）</text>
          <text class="tip-item">4. 客服将在 24 小时内联系您处理，请保持电话畅通</text>
        </view>
      </view>

      <view class="bottom-spacer" />
    </scroll-view>

    <!-- 底部提交按钮 -->
    <view class="bottom-bar" v-if="order">
      <button class="btn-submit" @tap="submitClaim" :disabled="submitting">
        <text v-if="!submitting">提交售后申请</text>
        <text v-else>提交中...</text>
      </button>
    </view>

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

.apply-scroll {
  flex: 1;
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
  display: flex;
  align-items: center;
  gap: 4px;
}

.required {
  color: #F44336;
  font-size: 12px;
}

/* 订单信息 */
.order-info {
  padding-bottom: 8px;
}

.order-detail {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.order-no {
  font-size: 14px;
  color: #1A1A1A;
  font-family: monospace;
}

.order-time {
  font-size: 13px;
  color: #9E9E9E;
}

.order-status {
  font-size: 12px;
  color: #2E7D32;
  background: #E8F5E9;
  padding: 2px 8px;
  border-radius: 4px;
  display: inline-block;
  width: fit-content;
}

/* 商品选择 */
.item-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.item-card {
  display: flex;
  gap: 12px;
  padding: 12px;
  border: 1px solid #EEEEEE;
  border-radius: 8px;
  background: #FAFAFA;
}

.item-card.selected {
  border-color: #2E7D32;
  background: #E8F5E9;
}

.item-image {
  width: 60px;
  height: 60px;
  border-radius: 8px;
  object-fit: cover;
}

.item-info {
  flex: 1;
  min-width: 0;
}

.item-title {
  display: -webkit-box;
  -webkit-box-orient: vertical;
  overflow: hidden;
  font-size: 14px;
  color: #1A1A1A;
  line-height: 1.4;
  margin-bottom: 4px;
}

.item-spec {
  display: block;
  font-size: 12px;
  color: #9E9E9E;
  margin-bottom: 4px;
}

.item-price-qty {
  display: flex;
  align-items: center;
  gap: 12px;
}

.item-price {
  font-size: 14px;
  font-weight: 500;
  color: #2E7D32;
}

.item-qty {
  font-size: 13px;
  color: #9E9E9E;
}

.item-check {
  font-size: 20px;
  color: #2E7D32;
  margin-top: 4px;
}

/* 售后原因 */
.reason-list {
  display: flex;
  flex-direction: column;
  gap: 8px;
}

.reason-item {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px;
  border: 1px solid #EEEEEE;
  border-radius: 8px;
  background: #FAFAFA;
}

.reason-item.selected {
  border-color: #2E7D32;
  background: #E8F5E9;
}

.reason-label {
  font-size: 14px;
  color: #1A1A1A;
  flex: 1;
}

.reason-radio {
  width: 20px;
  height: 20px;
  border: 2px solid #E0E0E0;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  margin-left: 12px;
}

.reason-radio.checked {
  border-color: #2E7D32;
}

.radio-inner {
  width: 10px;
  height: 10px;
  background: #2E7D32;
  border-radius: 50%;
}

/* 问题描述 */
.description-input {
  font-size: 14px;
  color: #1A1A1A;
  background: #FAFAFA;
  border: 1px solid #EEEEEE;
  border-radius: 8px;
  padding: 12px;
  min-height: 100px;
  line-height: 1.6;
}

.char-count {
  display: block;
  text-align: right;
  font-size: 12px;
  color: #9E9E9E;
  margin-top: 8px;
}

/* 申请数量 */
.quantity-selector {
  display: flex;
  align-items: center;
  gap: 12px;
}

.qty-btn {
  width: 40px;
  height: 40px;
  border: 1px solid #E0E0E0;
  background: #FFFFFF;
  border-radius: 8px;
  font-size: 18px;
  color: #1A1A1A;
  display: flex;
  align-items: center;
  justify-content: center;
}

.qty-btn:disabled {
  color: #BDBDBD;
  border-color: #EEEEEE;
  background: #FAFAFA;
}

.qty-value {
  font-size: 18px;
  font-weight: 600;
  color: #1A1A1A;
  min-width: 40px;
  text-align: center;
}

.qty-max {
  font-size: 12px;
  color: #9E9E9E;
}

/* 上传图片 */
.image-hint {
  display: block;
  font-size: 12px;
  color: #9E9E9E;
  margin-bottom: 12px;
}

.image-grid {
  display: flex;
  gap: 8px;
  flex-wrap: wrap;
}

.image-item {
  position: relative;
  width: 80px;
  height: 80px;
  border-radius: 8px;
  overflow: hidden;
}

.image-thumb {
  width: 100%;
  height: 100%;
  object-fit: cover;
}

.image-remove {
  position: absolute;
  top: -4px;
  right: -4px;
  width: 20px;
  height: 20px;
  background: rgba(0, 0, 0, 0.6);
  color: #FFFFFF;
  border-radius: 50%;
  display: flex;
  align-items: center;
  justify-content: center;
  font-size: 12px;
}

.image-add {
  width: 80px;
  height: 80px;
  border: 2px dashed #E0E0E0;
  border-radius: 8px;
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  gap: 4px;
  color: #9E9E9E;
}

.add-icon {
  font-size: 24px;
}

.add-text {
  font-size: 11px;
}

/* 温馨提示 */
.tips {
  background: #FFF8E1;
}

.tips-title {
  font-size: 14px;
  font-weight: 600;
  color: #E65100;
  margin-bottom: 8px;
}

.tips-list {
  display: flex;
  flex-direction: column;
  gap: 6px;
}

.tip-item {
  font-size: 12px;
  color: #BF360C;
  line-height: 1.5;
}

/* 底部栏 */
.bottom-bar {
  position: fixed;
  bottom: 0;
  left: 0;
  right: 0;
  padding: 12px 16px;
  background: #FFFFFF;
  border-top: 1px solid #EEEEEE;
  z-index: 50;
  padding-bottom: calc(12px + env(safe-area-inset-bottom, 34px));
}

.btn-submit {
  width: 100%;
  height: 50px;
  background: linear-gradient(135deg, #2E7D32, #4CAF50);
  color: #FFFFFF;
  border: none;
  border-radius: 25px;
  font-size: 16px;
  font-weight: 600;
  box-shadow: 0 4px 12px rgba(46, 125, 50, 0.3);
}

.btn-submit:disabled {
  background: #A5D6A7;
  box-shadow: none;
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

.bottom-spacer {
  height: 80px;
}
</style>
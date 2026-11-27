<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { authApi, membershipApi } from '@/utils/api'
import { formatPriceWithSymbol, formatDateTime } from '@/utils/helpers'
import { getToken, clearToken, saveToken } from '@/utils/request'
import type { CustomerProfile, MembershipSummary } from '@/types'

// Helper functions
function getTierLabel(tier: string): string {
    const labels: Record<string, string> = {
        normal: '普通会员',
        silver: '银牌会员',
        gold: '金牌会员',
        diamond: '钻石会员'
    }
    return labels[tier] || tier
}

function getTierDesc(tier: string): string {
    const descs: Record<string, string> = {
        normal: '注册即享基础权益',
        silver: '享受专属折扣与优先配送',
        gold: '更高折扣、免费配送、生日礼',
        diamond: '最高折扣、专属客服、定制服务'
    }
    return descs[tier] || ''
}

const userInfo = ref<CustomerProfile | null>(null)
const membership = ref<MembershipSummary | null>(null)
const loading = ref(false)
const showLoginModal = ref(false)
const pendingCount = ref(0)
const shippedCount = ref(0)

async function fetchProfile() {
    const token = getToken()
    if (!token) {
        userInfo.value = null
        membership.value = null
        return
    }

    loading.value = true
    try {
        const [profileRes, memberRes] = await Promise.all([
            authApi.getProfile(),
            membershipApi.getMembershipSummary().catch(() => null)
        ])
        userInfo.value = profileRes
        membership.value = memberRes
    } catch (e) {
        console.error('获取个人信息失败:', e)
        // Token 可能过期
        clearToken()
        userInfo.value = null
        membership.value = null
    } finally {
        loading.value = false
    }
}

function goLogin() {
    uni.navigateTo({ url: '/pages/login/login' })
}

function goOrders(status?: string) {
    if (status) {
        uni.navigateTo({ url: `/pages/order/list?tab=${status}` })
    } else {
        uni.switchTab({ url: '/pages/order/list' })
    }
}

function goAddresses() {
    uni.navigateTo({ url: '/pages/address/list' })
}

function goMembership() {
    uni.showToast({ title: '会员中心开发中', icon: 'none' })
}

function goPoints() {
    uni.showToast({ title: '积分明细开发中', icon: 'none' })
}

function goStoredValue() {
    uni.showToast({ title: '储值明细开发中', icon: 'none' })
}

function goCustomerService() {
    uni.showModal({
        title: '客服联系',
        content: '客服电话：400-800-8888\n工作时间：9:00-21:00',
        showCancel: false
    })
}

function goSettings() {
    uni.showToast({ title: '设置开发中', icon: 'none' })
}

async function logout() {
    const confirm = await new Promise<boolean>(resolve => {
        uni.showModal({
            title: '确定退出登录？',
            success: (res) => resolve(res.confirm)
        })
    })
    if (confirm) {
        clearToken()
        userInfo.value = null
        membership.value = null
        uni.showToast({ title: '已退出登录', icon: 'success' })
    }
}

const menuItems = computed(() => [
    { icon: '📦', title: '我的订单', subtitle: '查看所有订单状态', action: () => goOrders() },
    { icon: '📍', title: '收货地址', subtitle: '管理收货地址', action: goAddresses },
    { icon: '💎', title: '会员中心', subtitle: '等级、积分、储值', action: goMembership },
    { icon: '⭐', title: '我的积分', subtitle: '积分获取与使用记录', action: goPoints },
    { icon: '💰', title: '储值余额', subtitle: '储值充值与消费记录', action: goStoredValue },
    { icon: '🎧', title: '客服中心', subtitle: '在线客服与常见问题', action: goCustomerService },
    { icon: '⚙️', title: '设置', subtitle: '通用设置与隐私', action: goSettings }
])

onMounted(() => {
    fetchProfile()
})

onShow(() => {
    fetchProfile()
})
</script>

<template>
    <view class="page">
        <scroll-view class="profile-scroll" scroll-y :style="{ height: '100vh' }" refresher-enabled :refresher-triggered="loading" @refresherrefresh="fetchProfile">
            <!-- 用户头部 -->
            <view class="user-header" v-if="userInfo">
                <image class="avatar" :src="userInfo.avatar_url || '/static/images/default-avatar.png'" mode="aspectFill"></image>
                <view class="user-name">{{ userInfo.name || '宋鲜生用户' }}</view>
                <view class="user-phone">{{ userInfo.phone || '未绑定手机号' }}</view>
                <view class="user-tags">
                    <view class="tag tag-primary" v-if="userInfo.wechat_bound">已绑定微信</view>
                    <view class="tag tag-warning" v-else>未绑定微信</view>
                </view>
            </view>

            <!-- 未登录状态 -->
            <view class="user-header guest" v-else>
                <image class="avatar" src="/static/images/default-avatar.png" mode="aspectFill"></image>
                <view class="user-name">点击登录/注册</view>
                <view class="user-desc">登录后享受会员权益，订单同步更方便</view>
                <button class="btn-login" @tap="goLogin">立即登录</button>
            </view>

            <!-- 会员卡片 -->
            <view class="section membership-card" v-if="membership">
                <view class="membership-header">
                    <view class="membership-info">
                        <text class="membership-level">{{ getTierLabel(membership.tier) }}</text>
                        <text class="membership-desc">{{ getTierDesc(membership.tier) }}</text>
                    </view>
                    <view class="membership-balance">
                        <text class="balance-label">积分</text>
                        <text class="balance-value">{{ membership.points_balance }}</text>
                    </view>
                </view>
                <view class="membership-details">
                    <view class="detail-item">
                        <text class="detail-value">¥{{ (membership.stored_value_balance_cents / 100).toFixed(2) }}</text>
                        <text class="detail-label">储值余额</text>
                    </view>
                    <view class="detail-divider"></view>
                    <view class="detail-item">
                        <text class="detail-value">¥{{ (membership.voucher_balance_cents / 100).toFixed(2) }}</text>
                        <text class="detail-label">优惠券余额</text>
                    </view>
                </view>
            </view>

            <!-- 功能菜单 -->
            <view class="section">
                <view class="menu-list">
                    <view class="menu-item" v-for="(item, index) in menuItems" :key="index" @tap="item.action()">
                        <view class="menu-icon">{{ item.icon }}</view>
                        <view class="menu-content">
                            <text class="menu-title">{{ item.title }}</text>
                            <text class="menu-subtitle">{{ item.subtitle }}</text>
                        </view>
                        <text class="menu-arrow">></text>
                    </view>
                </view>
            </view>

            <!-- 快捷入口 -->
            <view class="section quick-actions" v-if="userInfo">
                <view class="quick-title">快捷入口</view>
                <view class="quick-grid">
                    <view class="quick-item" @tap="goOrders('pending')">
                        <text class="quick-icon">⏳</text>
                        <text class="quick-label">待支付</text>
                        <text class="quick-count" v-if="pendingCount > 0">{{ pendingCount }}</text>
                    </view>
                    <view class="quick-item" @tap="goOrders('shipped')">
                        <text class="quick-icon">📦</text>
                        <text class="quick-label">待收货</text>
                        <text class="quick-count" v-if="shippedCount > 0">{{ shippedCount }}</text>
                    </view>
                    <view class="quick-item" @tap="goAddresses">
                        <text class="quick-icon">📍</text>
                        <text class="quick-label">地址管理</text>
                    </view>
                    <view class="quick-item" @tap="logout">
                        <text class="quick-icon">🚪</text>
                        <text class="quick-label">退出登录</text>
                    </view>
                </view>
            </view>

            <!-- 版本信息 -->
            <view class="version-info">
                <text>宋鲜生 v1.0.0</text>
                <text>Copyright 2024 SongMall All Rights Reserved</text>
            </view>
        </scroll-view>
    </view>
</template>

<style scoped>
.page {
    background: #F5F5F5;
    height: 100vh;
}

.profile-scroll {
    height: 100%;
}

.user-header {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 32px 24px 24px;
    background: linear-gradient(135deg, #2E7D32 0%, #4CAF50 100%);
    color: #FFFFFF;
    position: relative;
}

.user-header.guest {
    padding: 40px 24px;
}

.avatar {
    width: 80px;
    height: 80px;
    border-radius: 50%;
    border: 3px solid rgba(255, 255, 255, 0.3);
    margin-bottom: 12px;
}

.user-name {
    font-size: 18px;
    font-weight: 600;
    margin-bottom: 4px;
}

.user-phone {
    font-size: 13px;
    opacity: 0.8;
}

.user-desc {
    font-size: 13px;
    opacity: 0.7;
    margin-bottom: 20px;
}

.user-tags {
    display: flex;
    gap: 8px;
    margin-top: 8px;
}

.tag {
    padding: 4px 12px;
    border-radius: 12px;
    font-size: 11px;
    font-weight: 500;
}

.tag-primary {
    background: rgba(255, 255, 255, 0.2);
    color: #FFFFFF;
}

.tag-warning {
    background: rgba(255, 193, 7, 0.2);
    color: #FFC107;
}

.btn-login {
    padding: 10px 32px;
    background: #FFFFFF;
    border: none;
    border-radius: 20px;
    color: #2E7D32;
    font-size: 14px;
    font-weight: 500;
}

.section {
    background: #FFFFFF;
    margin-bottom: 8px;
    padding: 16px;
}

.membership-card {
    border-radius: 12px;
    overflow: hidden;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.membership-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 16px;
}

.membership-level {
    font-size: 20px;
    font-weight: 700;
    color: #2E7D32;
    display: block;
    margin-bottom: 4px;
}

.membership-desc {
    font-size: 12px;
    color: #616161;
}

.membership-balance {
    text-align: right;
}

.balance-label {
    font-size: 12px;
    color: #9E9E9E;
    display: block;
    margin-bottom: 2px;
}

.balance-value {
    font-size: 20px;
    font-weight: 700;
    color: #2E7D32;
}

.membership-details {
    display: flex;
    border-top: 1px solid #EEEEEE;
    padding-top: 16px;
}

.detail-item {
    flex: 1;
    text-align: center;
}

.detail-divider {
    width: 1px;
    background: #EEEEEE;
    margin: 0 16px;
}

.detail-value {
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
    display: block;
    margin-bottom: 2px;
}

.detail-label {
    font-size: 12px;
    color: #9E9E9E;
}

.menu-list {
    border-radius: 12px;
    overflow: hidden;
}

.menu-item {
    display: flex;
    align-items: center;
    padding: 14px 16px;
    background: #FFFFFF;
    border-bottom: 1px solid #F5F5F5;
}

.menu-item:last-child {
    border-bottom: none;
}

.menu-icon {
    font-size: 22px;
    width: 44px;
    text-align: center;
    margin-right: 12px;
}

.menu-content {
    flex: 1;
    min-width: 0;
}

.menu-title {
    display: block;
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 2px;
}

.menu-subtitle {
    font-size: 12px;
    color: #9E9E9E;
}

.menu-arrow {
    font-size: 14px;
    color: #9E9E9E;
}

.quick-actions {
    padding-bottom: 24px;
}

.quick-title {
    font-size: 14px;
    font-weight: 600;
    color: #1A1A1A;
    margin-bottom: 12px;
}

.quick-grid {
    display: grid;
    grid-template-columns: repeat(4, 1fr);
    gap: 12px;
}

.quick-item {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 16px 8px;
    background: #FFFFFF;
    border-radius: 12px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
}

.quick-icon {
    font-size: 24px;
    margin-bottom: 8px;
}

.quick-label {
    font-size: 12px;
    color: #616161;
    text-align: center;
}

.quick-count {
    margin-top: 4px;
    padding: 2px 6px;
    background: #FFF3E0;
    color: #E65100;
    border-radius: 8px;
    font-size: 11px;
    font-weight: 600;
}

.version-info {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 24px 16px;
    color: #9E9E9E;
    font-size: 12px;
    gap: 4px;
}
</style>
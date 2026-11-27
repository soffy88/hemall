<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { addressApi } from '@/utils/api'
import { getToken } from '@/utils/request'
import type { CustomerAddress } from '@/types'

const props = defineProps<{ select?: boolean }>()

const addresses = ref<CustomerAddress[]>([])
const loading = false

async function fetchAddresses() {
    try {
        const res = await addressApi.getAddresses()
        addresses.value = res
    } catch (e) {
        console.error('获取地址失败:', e)
        uni.showToast({ title: '加载失败', icon: 'none' })
    }
}

function goEdit(address?: CustomerAddress) {
    const url = address
        ? `/pages/address/edit?id=${address.id}`
        : '/pages/address/edit'
    uni.navigateTo({ url })
}

async function setDefault(address: CustomerAddress) {
    try {
        await addressApi.updateAddress(address.id, { is_default: true })
        uni.showToast({ title: '设置成功', icon: 'success' })
        await fetchAddresses()
    } catch (e: any) {
        uni.showToast({ title: e.message || '设置失败', icon: 'none' })
    }
}

async function deleteAddress(address: CustomerAddress) {
    const confirm = await new Promise<boolean>(resolve => {
        uni.showModal({
            title: '确定删除该地址？',
            success: (res) => resolve(res.confirm)
        })
    })
    if (!confirm) return

    try {
        await addressApi.deleteAddress(address.id)
        uni.showToast({ title: '删除成功', icon: 'success' })
        await fetchAddresses()
    } catch (e: any) {
        uni.showToast({ title: e.message || '删除失败', icon: 'none' })
    }
}

function selectAddress(address: CustomerAddress) {
    if (props.select) {
        uni.setStorageSync('selected_checkout_address', address)
        uni.navigateBack()
    }
}

function formatAddress(addr: CustomerAddress): string {
    return `${addr.city}${addr.address_line1}${addr.address_line2} ${addr.postal_code}`
}

onMounted(() => {
    fetchAddresses()
})

onShow(() => {
    fetchAddresses()
})
</script>

<template>
    <view class="page">
        <view class="page-header">
            <text class="page-title">收货地址</text>
        </view>

        <scroll-view class="address-scroll" scroll-y :style="{ height: 'calc(100vh - 90px)' }" refresher-enabled @refresherrefresh="fetchAddresses">
            <view class="address-list" v-if="addresses.length">
                <view class="address-card" v-for="addr in addresses" :key="addr.id" @tap="props.select ? selectAddress(addr) : null">
                    <view class="address-main">
                        <view class="address-header">
                            <view class="address-name-phone">
                                <text class="address-name">{{ addr.recipient_name }}</text>
                                <text class="address-phone">{{ addr.phone }}</text>
                            </view>
                            <view class="address-tags">
                                <view class="tag tag-primary" v-if="addr.is_default">默认</view>
                                <view class="tag" v-if="props.select">选择</view>
                            </view>
                        </view>
                        <text class="address-detail">{{ formatAddress(addr) }}</text>
                    </view>
                    <view class="address-actions" v-if="!props.select">
                        <button class="action-btn" @tap.stop="setDefault(addr)" v-if="!addr.is_default">设为默认</button>
                        <button class="action-btn edit" @tap.stop="goEdit(addr)">编辑</button>
                        <button class="action-btn danger" @tap.stop="deleteAddress(addr)">删除</button>
                    </view>
                </view>
            </view>

            <view class="empty-state" v-else>
                <image class="empty-icon" src="/static/images/empty-address.png" mode="aspectFit"></image>
                <text class="empty-title">暂无收货地址</text>
                <text class="empty-desc">添加地址以便更快下单</text>
                <button class="btn-add" @tap="goEdit">添加地址</button>
            </view>
        </scroll-view>

        <!-- 底部添加按钮 -->
        <view class="bottom-add" v-if="!props.select">
            <button class="btn-add-main" @tap="goEdit">+ 添加新地址</button>
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

.page-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 16px;
    background: #FFFFFF;
    border-bottom: 1px solid #EEEEEE;
}

.page-title {
    font-size: 18px;
    font-weight: 600;
    color: #1A1A1A;
}

.address-scroll {
    flex: 1;
}

.address-list {
    padding: 8px 16px 100px;
}

.address-card {
    background: #FFFFFF;
    border-radius: 12px;
    margin-bottom: 8px;
    box-shadow: 0 1px 4px rgba(0, 0, 0, 0.03);
    overflow: hidden;
}

.address-main {
    padding: 16px;
}

.address-header {
    display: flex;
    justify-content: space-between;
    align-items: center;
    margin-bottom: 8px;
}

.address-name-phone {
    display: flex;
    align-items: center;
    gap: 8px;
}

.address-name {
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
}

.address-phone {
    font-size: 15px;
    color: #616161;
}

.address-tags {
    display: flex;
    gap: 6px;
}

.tag {
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
}

.tag-primary {
    background: #E8F5E9;
    color: #2E7D32;
}

.tag:not(.tag-primary) {
    background: #E3F2FD;
    color: #1565C0;
}

.address-detail {
    font-size: 13px;
    color: #616161;
    line-height: 1.5;
}

.address-actions {
    display: flex;
    border-top: 1px solid #F5F5F5;
}

.action-btn {
    flex: 1;
    padding: 12px;
    font-size: 13px;
    font-weight: 500;
    color: #616161;
    background: transparent;
    border: none;
    text-align: center;
}

.action-btn.edit {
    color: #2E7D32;
    border-left: 1px solid #F5F5F5;
    border-right: 1px solid #F5F5F5;
}

.action-btn.danger {
    color: #F44336;
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
    margin-bottom: 4px;
}

.empty-desc {
    font-size: 13px;
    margin-bottom: 20px;
}

.btn-add {
    padding: 10px 32px;
    background: #2E7D32;
    border: none;
    border-radius: 20px;
    color: #FFFFFF;
    font-size: 14px;
}

.bottom-add {
    padding: 16px;
    background: #FFFFFF;
    border-top: 1px solid #EEEEEE;
}

.btn-add-main {
    width: 100%;
    padding: 14px;
    background: #E8F5E9;
    border: 1px solid #C8E6C9;
    border-radius: 8px;
    color: #2E7D32;
    font-size: 15px;
    font-weight: 500;
}
</style>
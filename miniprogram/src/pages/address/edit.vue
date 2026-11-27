<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { addressApi } from '@/utils/api'
import { validatePhone } from '@/utils/helpers'
import { getToken } from '@/utils/request'
import type { CustomerAddress, AddAddressRequest, UpdateAddressRequest } from '@/types'

const props = defineProps<{ id?: string }>()
const emit = defineEmits<{ success: [] }>()

const isEditing = computed(() => !!props.id)
const loading = ref(false)
const saving = ref(false)

const form = ref<AddAddressRequest & { id?: string }>({
    recipient_name: '',
    phone: '',
    address_line1: '',
    address_line2: '',
    city: '',
    region_code: '',
    postal_code: '',
    is_default: false
})

async function fetchAddress() {
    if (!props.id) return
    try {
        const res = await addressApi.getAddresses()
        const addr = res.find(a => a.id === props.id)
        if (addr) {
            form.value = {
                id: addr.id,
                recipient_name: addr.recipient_name,
                phone: addr.phone,
                address_line1: addr.address_line1,
                address_line2: addr.address_line2,
                city: addr.city,
                region_code: addr.region_code,
                postal_code: addr.postal_code,
                is_default: addr.is_default
            }
        }
    } catch (e) {
        console.error('获取地址详情失败:', e)
    }
}

async function saveAddress() {
    // 验证
    if (!form.value.recipient_name.trim()) {
        uni.showToast({ title: '请填写收货人姓名', icon: 'none' })
        return
    }
    if (!form.value.phone.trim()) {
        uni.showToast({ title: '请填写手机号', icon: 'none' })
        return
    }
    if (!validatePhone(form.value.phone)) {
        uni.showToast({ title: '手机号格式不正确', icon: 'none' })
        return
    }
    if (!form.value.city.trim()) {
        uni.showToast({ title: '请填写城市', icon: 'none' })
        return
    }
    if (!form.value.address_line1.trim()) {
        uni.showToast({ title: '请填写详细地址', icon: 'none' })
        return
    }
    if (!form.value.postal_code.trim()) {
        uni.showToast({ title: '请填写邮政编码', icon: 'none' })
        return
    }

    saving.value = true
    try {
        if (isEditing.value) {
            const { id, ...updateData } = form.value
            await addressApi.updateAddress(props.id!, updateData as UpdateAddressRequest)
            uni.showToast({ title: '保存成功', icon: 'success' })
        } else {
            await addressApi.addAddress(form.value as AddAddressRequest)
            uni.showToast({ title: '添加成功', icon: 'success' })
        }
        emit('success')
        setTimeout(() => uni.navigateBack(), 500)
    } catch (e: any) {
        uni.showToast({ title: e.message || '保存失败', icon: 'none' })
    } finally {
        saving.value = false
    }
}

function chooseLocation() {
    uni.chooseLocation({
        success: (res) => {
            form.value.city = res.city
            form.value.address_line1 = res.address
            form.value.address_line2 = res.name || ''
            // 微信选择位置不返回邮编，需用户手动填写
        },
        fail: (err) => {
            if (err.errMsg !== 'chooseLocation:fail cancel') {
                uni.showToast({ title: '获取位置失败', icon: 'none' })
            }
        }
    })
}

onMounted(() => {
    fetchAddress()
})
</script>

<template>
    <view class="page">
        <view class="page-header">
            <text class="page-title">{{ isEditing ? '编辑地址' : '新增地址' }}</text>
        </view>

        <scroll-view class="form-scroll" scroll-y :style="{ height: 'calc(100vh - 90px)' }">
            <view class="form-section">
                <view class="form-row">
                    <text class="form-label"><text class="required">*</text>收货人</text>
                    <input class="form-input" v-model="form.recipient_name" placeholder="请输入收货人姓名" maxlength="20" />
                </view>
                <view class="form-row">
                    <text class="form-label"><text class="required">*</text>手机号</text>
                    <input class="form-input" v-model="form.phone" placeholder="请输入手机号" type="number" maxlength="11" />
                </view>
            </view>

            <view class="form-section">
                <view class="form-row">
                    <text class="form-label"><text class="required">*</text>所在地区</text>
                    <view class="location-input" @tap="chooseLocation">
                        <input class="form-input" v-model="form.city" placeholder="点击选择城市" readonly />
                        <text class="location-icon">📍</text>
                    </view>
                </view>
                <view class="form-row">
                    <text class="form-label"><text class="required">*</text>详细地址</text>
                    <input class="form-input" v-model="form.address_line1" placeholder="街道、小区、楼栋、门牌号" maxlength="100" />
                </view>
                <view class="form-row">
                    <text class="form-label">标签/备注</text>
                    <input class="form-input" v-model="form.address_line2" placeholder="如：公司、家、学校、菜鸟驿站" maxlength="30" />
                </view>
                <view class="form-row">
                    <text class="form-label"><text class="required">*</text>邮政编码</text>
                    <input class="form-input" v-model="form.postal_code" placeholder="请输入邮政编码" type="number" maxlength="6" />
                </view>
            </view>

            <view class="form-section">
                <view class="switch-row">
                    <view class="switch-label">
                        <text class="switch-title">设为默认地址</text>
                        <text class="switch-desc">新增地址时自动设为默认</text>
                    </view>
                    <switch class="switch" :checked="form.is_default" @change="form.is_default = $event.detail.value" />
                </view>
            </view>

            <view class="bottom-placeholder" />
        </scroll-view>

        <view class="form-footer">
            <button class="btn-save" @tap="saveAddress" :disabled="saving">
                <text v-if="!saving">保存地址</text>
                <text v-else>保存中...</text>
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

.form-scroll {
    flex: 1;
}

.form-section {
    background: #FFFFFF;
    margin-bottom: 8px;
    padding: 16px;
}

.form-row {
    display: flex;
    align-items: center;
    margin-bottom: 16px;
}

.form-row:last-child {
    margin-bottom: 0;
}

.form-label {
    width: 80px;
    flex-shrink: 0;
    font-size: 14px;
    color: #1A1A1A;
}

.required {
    color: #F44336;
    margin-right: 4px;
}

.form-input {
    flex: 1;
    height: 40px;
    padding: 0 12px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 14px;
    color: #1A1A1A;
    background: #FFFFFF;
}

.form-input[readonly] {
    background: #F5F5F5;
    color: #9E9E9E;
}

.location-input {
    flex: 1;
    position: relative;
}

.location-icon {
    position: absolute;
    right: 12px;
    top: 50%;
    transform: translateY(-50%);
    font-size: 16px;
}

.switch-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.switch-label {
    flex: 1;
}

.switch-title {
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
    display: block;
    margin-bottom: 2px;
}

.switch-desc {
    font-size: 12px;
    color: #9E9E9E;
}

.form-footer {
    padding: 16px;
    background: #FFFFFF;
    border-top: 1px solid #EEEEEE;
    position: sticky;
    bottom: 0;
}

.btn-save {
    width: 100%;
    height: 48px;
    background: #2E7D32;
    border: none;
    border-radius: 8px;
    color: #FFFFFF;
    font-size: 16px;
    font-weight: 600;
    display: flex;
    align-items: center;
    justify-content: center;
}

.btn-save[disabled] {
    background: #A5D6A7;
}
</style>
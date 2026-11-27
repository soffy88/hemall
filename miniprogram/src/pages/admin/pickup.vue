<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { adminApi } from '@/utils/api'

const loading = ref(false)
const saving = ref(false)
const showPointEditor = ref(false)
const editingPoint = ref<any | null>(null)
const isNewPoint = ref(false)

const config = ref<any>({
    points: [],
    default_point_id: '',
    note: ''
})

// 自提点名称列表（用于 picker）
const pointNames = computed(() => config.value.points?.map((p: any) => p.name) || [])

// 默认自提点索引
const defaultPointIndex = computed(() => {
    const points = config.value.points || []
    const defaultId = config.value.default_point_id
    return points.findIndex((p: any) => p.id === defaultId)
})

// 默认自提点名称
const defaultPointName = computed(() => {
    const points = config.value.points || []
    const defaultId = config.value.default_point_id
    return points.find((p: any) => p.id === defaultId)?.name
})

// 默认自提点变更
function onDefaultPointChange(e: any) {
    const points = config.value.points || []
    const index = e.detail.value
    if (points[index]) {
        config.value.default_point_id = points[index].id
    }
}

const pointForm = ref({
    id: '',
    name: '',
    address: '',
    phone: '',
    latitude: 0,
    longitude: 0,
    business_hours: { start: '08:00', end: '21:00' },
    time_slots: [] as any[],
    is_active: true,
})

async function fetchSettings() {
    loading.value = true
    try {
        const res = await adminApi.getPickupSettings()
        config.value = res.config || { points: [], default_point_id: '', note: '' }
    } catch (e: any) {
        uni.showToast({ title: e.message || '加载失败', icon: 'none' })
    } finally {
        loading.value = false
    }
}

function openPointEditor(point?: any) {
    if (point) {
        editingPoint.value = point
        isNewPoint.value = false
        pointForm.value = { ...point }
    } else {
        editingPoint.value = null
        isNewPoint.value = true
        pointForm.value = {
            id: `point_${Date.now()}`,
            name: '',
            address: '',
            phone: '',
            latitude: 0,
            longitude: 0,
            business_hours: { start: '08:00', end: '21:00' },
            time_slots: [],
            is_active: true,
        }
    }
    showPointEditor.value = true
}

function addTimeSlot() {
    pointForm.value.time_slots.push({
        start: '10:00',
        end: '12:00',
        capacity: 30,
    })
}

function removeTimeSlot(index: number) {
    pointForm.value.time_slots.splice(index, 1)
}

async function savePoint() {
    if (!pointForm.value.name || !pointForm.value.address) {
        uni.showToast({ title: '请填写名称和地址', icon: 'none' })
        return
    }

    const newPoints = [...config.value.points]
    if (isNewPoint.value) {
        newPoints.push({ ...pointForm.value })
    } else {
        const idx = newPoints.findIndex(p => p.id === pointForm.value.id)
        if (idx >= 0) newPoints[idx] = { ...pointForm.value }
    }

    try {
        await adminApi.savePickupSettings({ ...config.value, points: newPoints })
        uni.showToast({ title: '保存成功', icon: 'success' })
        showPointEditor.value = false
        await fetchSettings()
    } catch (e: any) {
        uni.showToast({ title: e.message || '保存失败', icon: 'none' })
    }
}

async function deletePoint(point: any) {
    const confirm = await uni.showModal({
        title: '确认删除',
        content: `确定要删除"${point.name}"吗？`,
        confirmColor: '#F44336',
    })
    if (!confirm.confirm) return

    const newPoints = config.value.points.filter((p: any) => p.id !== point.id)
    try {
        await adminApi.savePickupSettings({ ...config.value, points: newPoints })
        uni.showToast({ title: '已删除', icon: 'success' })
        await fetchSettings()
    } catch (e: any) {
        uni.showToast({ title: e.message || '删除失败', icon: 'none' })
    }
}

async function togglePointStatus(point: any) {
    const newPoints = config.value.points.map((p: any) =>
        p.id === point.id ? { ...p, is_active: !p.is_active } : p
    )
    try {
        await adminApi.savePickupSettings({ ...config.value, points: newPoints })
        await fetchSettings()
    } catch (e: any) {
        uni.showToast({ title: e.message || '操作失败', icon: 'none' })
    }
}

async function setAsDefault(point: any) {
    try {
        await adminApi.savePickupSettings({ ...config.value, default_point_id: point.id })
        await fetchSettings()
    } catch (e: any) {
        uni.showToast({ title: e.message || '设置失败', icon: 'none' })
    }
}

function chooseLocation() {
    uni.chooseLocation({
        success: (res) => {
            pointForm.value.latitude = res.latitude
            pointForm.value.longitude = res.longitude
            pointForm.value.address = res.address || res.name
        }
    })
}

onMounted(() => {
    fetchSettings()
})
</script>

<template>
    <view class="page">
        <view class="header">
            <text class="header-title">自提点管理</text>
            <button class="btn-add" @tap="openPointEditor">+ 新增</button>
        </view>

        <scroll-view class="pickup-list" scroll-y :style="{ height: 'calc(100vh - 98px)' }" v-if="!loading">
            <view class="section">
                <text class="section-title">基本设置</text>
                <view class="setting-item">
                    <text class="setting-label">默认自提点</text>
                    <picker class="setting-picker" mode="selector" :range="pointNames" :value="defaultPointIndex" @change="onDefaultPointChange">
                        <view class="picker-display">
                            <text>{{ defaultPointName || '请选择' }}</text>
                        </view>
                    </picker>
                </view>
                <view class="setting-item">
                    <text class="setting-label">备注</text>
                    <input class="setting-input" v-model="config.note" placeholder="配置备注" />
                </view>
            </view>

            <view class="section">
                <view class="section-header">
                    <text class="section-title">自提点列表 ({{ config.points.length }})</text>
                    <button class="btn-add-small" @tap="openPointEditor">+ 添加自提点</button>
                </view>

                <view class="empty-state" v-if="config.points.length === 0">
                    <text class="empty-icon">🏪</text>
                    <text class="empty-text">暂无自提点</text>
                    <text class="empty-desc">请添加至少一个自提点供顾客选择</text>
                </view>

                <view class="point-items" v-else>
                    <view class="point-item" v-for="point in config.points" :key="point.id">
                        <view class="point-main">
                            <view class="point-status" :class="{ active: point.is_active, default: config.default_point_id === point.id }">
                                <text v-if="config.default_point_id === point.id" class="default-tag">默认</text>
                                <text v-else class="status-tag">{{ point.is_active ? '启用' : '禁用' }}</text>
                            </view>
                            <view class="point-info">
                                <text class="point-name">{{ point.name }}</text>
                                <text class="point-address">{{ point.address }}</text>
                                <text class="point-phone">{{ point.phone }}</text>
                            </view>
                            <view class="point-slots">
                                <text class="slots-label">{{ point.time_slots?.length || 0 }} 个时段</text>
                                <text class="slots-capacity">营业: {{ point.business_hours?.start }} - {{ point.business_hours?.end }}</text>
                            </view>
                        </view>
                        <view class="point-actions">
                            <button class="action-btn default" v-if="config.default_point_id !== point.id" @tap="setAsDefault(point)" size="mini">设为默认</button>
                            <button class="action-btn toggle" @tap="togglePointStatus(point)" size="mini">{{ point.is_active ? '禁用' : '启用' }}</button>
                            <button class="action-btn edit" @tap="openPointEditor(point)" size="mini">编辑</button>
                            <button class="action-btn delete" @tap="deletePoint(point)" size="mini">删除</button>
                        </view>
                    </view>
                </view>
            </view>
        </scroll-view>

        <view class="loading-overlay" v-if="loading">
            <view class="spinner" />
        </view>

        <!-- 编辑弹窗 -->
        <view class="modal-mask" v-if="showPointEditor" @tap="showPointEditor = false">
            <view class="modal editor-modal" @tap.stop>
                <view class="modal-header">
                    <text class="modal-title">{{ isNewPoint ? '新增自提点' : '编辑自提点' }}</text>
                    <text class="modal-close" @tap="showPointEditor = false">✕</text>
                </view>

                <scroll-view class="modal-body" scroll-y>
                    <view class="form-section">
                        <view class="form-group">
                            <label class="form-label">自提点 ID</label>
                            <input class="form-input" :value="pointForm.id" disabled />
                        </view>

                        <view class="form-group">
                            <label class="form-label">名称 *</label>
                            <input class="form-input" v-model="pointForm.name" placeholder="如：惠州仲恺惠环社区生鲜店" />
                        </view>

                        <view class="form-group">
                            <label class="form-label">地址 *</label>
                            <input class="form-input" v-model="pointForm.address" placeholder="详细地址" />
                        </view>

                        <view class="form-group">
                            <label class="form-label">联系电话</label>
                            <input class="form-input" v-model="pointForm.phone" placeholder="门店电话" />
                        </view>

                        <view class="form-group">
                            <label class="form-label">坐标</label>
                            <view class="coord-row">
                                <input class="form-input" v-model.number="pointForm.latitude" type="number" placeholder="纬度" step="0.000001" />
                                <input class="form-input" v-model.number="pointForm.longitude" type="number" placeholder="经度" step="0.000001" />
                            </view>
                            <button class="btn-location" @tap="chooseLocation">地图选点</button>
                        </view>

                        <view class="form-group">
                            <label class="form-label">营业时间</label>
                            <view class="time-row">
                                <picker class="form-picker" mode="time" :value="pointForm.business_hours.start" @change="pointForm.business_hours.start = $event.detail.value">
                                    <view class="picker-display">{{ pointForm.business_hours.start }}</view>
                                </picker>
                                <text class="time-separator">-</text>
                                <picker class="form-picker" mode="time" :value="pointForm.business_hours.end" @change="pointForm.business_hours.end = $event.detail.value">
                                    <view class="picker-display">{{ pointForm.business_hours.end }}</view>
                                </picker>
                            </view>
                        </view>

                        <view class="form-group">
                            <label class="form-label">可用时段</label>
                            <view class="slots-editor">
                                <view class="slot-row" v-for="(slot, index) in pointForm.time_slots" :key="index">
                                    <picker class="slot-picker" mode="time" :value="slot.start" @change="slot.start = $event.detail.value">
                                        <view class="picker-display">{{ slot.start }}</view>
                                    </picker>
                                    <text class="time-separator">-</text>
                                    <picker class="slot-picker" mode="time" :value="slot.end" @change="slot.end = $event.detail.value">
                                        <view class="picker-display">{{ slot.end }}</view>
                                    </picker>
                                    <input class="slot-capacity" type="number" v-model.number="slot.capacity" placeholder="容量" />
                                    <button class="btn-remove-slot" @tap="removeTimeSlot(index)" size="mini">删除</button>
                                </view>
                                <button class="btn-add-slot" @tap="addTimeSlot">+ 添加时段</button>
                            </view>
                        </view>

                        <view class="form-group switch-group">
                            <text class="switch-label">启用状态</text>
                            <switch :checked="pointForm.is_active" @change="pointForm.is_active = $event.detail.value" />
                        </view>
                    </view>
                </scroll-view>

                <view class="modal-footer">
                    <button class="btn-cancel" @tap="showPointEditor = false">取消</button>
                    <button class="btn-confirm" @tap="savePoint" :disabled="saving">
                        <text v-if="!saving">保存</text>
                        <text v-else>保存中...</text>
                    </button>
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

.btn-add {
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 20px;
    padding: 8px 16px;
    font-size: 14px;
    font-weight: 500;
}

.pickup-list {
    flex: 1;
    padding: 12px;
}

.section {
    background: #FFFFFF;
    border-radius: 12px;
    padding: 16px;
    margin-bottom: 12px;
}

.section-header {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 12px;
}

.section-title {
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
}

.btn-add-small {
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 16px;
    padding: 6px 12px;
    font-size: 12px;
    font-weight: 500;
}

.setting-item {
    display: flex;
    align-items: center;
    gap: 12px;
    padding: 12px 0;
    border-bottom: 1px solid #F5F5F5;
}

.setting-item:last-child {
    border-bottom: none;
}

.setting-label {
    width: 100px;
    font-size: 14px;
    color: #616161;
    flex-shrink: 0;
}

.setting-picker {
    flex: 1;
    height: 40px;
}

.setting-input {
    flex: 1;
    height: 40px;
    padding: 0 12px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 14px;
    background: #FAFAFA;
}

.picker-display {
    display: flex;
    align-items: center;
    height: 100%;
    padding: 0 12px;
    font-size: 14px;
    color: #1A1A1A;
}

.empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 40px 24px;
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
    margin-bottom: 8px;
}

.empty-desc {
    font-size: 13px;
    color: #9E9E9E;
}

.point-items {
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.point-item {
    background: #FAFAFA;
    border-radius: 8px;
    padding: 12px;
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.point-main {
    display: flex;
    gap: 12px;
    align-items: flex-start;
}

.point-status {
    display: flex;
    flex-direction: column;
    gap: 4px;
    align-items: center;
    min-width: 60px;
}

.default-tag {
    padding: 2px 8px;
    background: #FFF3E0;
    color: #E65100;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 600;
}

.status-tag {
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 500;
}

.status-tag.active {
    background: #E8F5E9;
    color: #2E7D32;
}

.status-tag:not(.active):not(.default-tag) {
    background: #F5F5F5;
    color: #9E9E9E;
}

.point-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    gap: 2px;
}

.point-name {
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
}

.point-address {
    font-size: 12px;
    color: #9E9E9E;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.point-phone {
    font-size: 12px;
    color: #9E9E9E;
}

.point-slots {
    display: flex;
    flex-direction: column;
    gap: 2px;
    min-width: 80px;
    text-align: right;
}

.slots-label {
    font-size: 12px;
    color: #616161;
}

.slots-capacity {
    font-size: 11px;
    color: #9E9E9E;
}

.point-actions {
    display: flex;
    gap: 8px;
    flex-wrap: wrap;
}

.action-btn {
    height: 28px;
    font-size: 11px;
    border-radius: 4px;
    padding: 0 8px;
}

.action-btn.default {
    background: #E3F2FD;
    color: #1565C0;
    border: none;
}

.action-btn.toggle {
    background: #FFF3E0;
    color: #E65100;
    border: none;
}

.action-btn.edit {
    background: #E8F5E9;
    color: #2E7D32;
    border: none;
}

.action-btn.delete {
    background: #FBE9E7;
    color: #C62828;
    border: none;
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

/* 编辑弹窗 */
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

.editor-modal {
    background: #FFFFFF;
    border-radius: 16px;
    width: 100%;
    max-width: 400px;
    max-height: 90vh;
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

.form-section {
    display: flex;
    flex-direction: column;
    gap: 20px;
}

.form-group {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.form-label {
    font-size: 13px;
    font-weight: 500;
    color: #1A1A1A;
}

.form-input {
    height: 44px;
    padding: 0 12px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 14px;
    color: #1A1A1A;
    background: #FAFAFA;
}

.form-input:disabled {
    background: #F5F5F5;
    color: #9E9E9E;
}

.coord-row {
    display: flex;
    gap: 8px;
}

.coord-row .form-input {
    flex: 1;
}

.btn-location {
    margin-top: 8px;
    background: #E3F2FD;
    color: #1565C0;
    border: none;
    border-radius: 8px;
    padding: 10px;
    font-size: 13px;
    font-weight: 500;
}

.time-row {
    display: flex;
    align-items: center;
    gap: 8px;
}

.time-row .form-picker {
    flex: 1;
    height: 44px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    background: #FAFAFA;
}

.time-separator {
    font-size: 14px;
    color: #9E9E9E;
}

.slots-editor {
    display: flex;
    flex-direction: column;
    gap: 8px;
}

.slot-row {
    display: flex;
    gap: 8px;
    align-items: center;
}

.slot-picker {
    flex: 1;
    height: 44px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    background: #FAFAFA;
}

.slot-capacity {
    width: 70px;
    height: 44px;
    padding: 0 8px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 14px;
    background: #FAFAFA;
}

.btn-remove-slot {
    background: #FBE9E7;
    color: #C62828;
    border: none;
    border-radius: 4px;
    padding: 6px 10px;
    font-size: 11px;
}

.btn-add-slot {
    margin-top: 8px;
    background: #E8F5E9;
    color: #2E7D32;
    border: none;
    border-radius: 8px;
    padding: 10px;
    font-size: 13px;
    font-weight: 500;
}

.switch-group {
    flex-direction: row;
    align-items: center;
    justify-content: space-between;
}

.switch-label {
    font-size: 14px;
    color: #1A1A1A;
}

.modal-footer {
    display: flex;
    gap: 12px;
    padding: 16px;
    border-top: 1px solid #EEEEEE;
    background: #FFFFFF;
    border-radius: 0 0 16px 16px;
    position: sticky;
    bottom: 0;
}

.btn-cancel,
.btn-confirm {
    flex: 1;
    height: 48px;
    border-radius: 12px;
    font-size: 16px;
    font-weight: 600;
    border: none;
}

.btn-cancel {
    background: #F5F5F5;
    color: #424242;
}

.btn-confirm {
    background: #2E7D32;
    color: #FFFFFF;
}

.btn-confirm:disabled {
    background: #A5D6A7;
}
</style>
<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { adminApi, bannerApi } from '@/utils/api'
import { formatDateTime } from '@/utils/helpers'
import type { BannerItem } from '@/types'

const banners = ref<BannerItem[]>([])
const loading = ref(false)
const saving = ref(false)
const showEditor = ref(false)
const editingBanner = ref<BannerItem | null>(null)
const isNewBanner = ref(false)

const formData = ref<Partial<BannerItem>>({
    image_url: '',
    link_type: 'none',
    link_target: '',
    title: '',
    subtitle: '',
    sort_order: 0,
    is_active: true,
    start_at: '',
    end_at: '',
})

const linkTypes = [
    { value: 'none', label: '无链接' },
    { value: 'product', label: '商品详情' },
    { value: 'category', label: '分类页面' },
    { value: 'pintuan', label: '团购活动' },
    { value: 'pickup', label: '自提点' },
    { value: 'url', label: '自定义链接' },
]

async function fetchBanners() {
    loading.value = true
    try {
        const res = await adminApi.getBannerSettings()
        banners.value = res.config?.banners || []
    } catch (e: any) {
        uni.showToast({ title: e.message || '加载失败', icon: 'none' })
    } finally {
        loading.value = false
    }
}

function onModalTap(e: any) {
    e.stopPropagation()
}

function openEditor(banner?: BannerItem) {
    if (banner) {
        editingBanner.value = banner
        isNewBanner.value = false
        formData.value = { ...banner }
    } else {
        editingBanner.value = null
        isNewBanner.value = true
        formData.value = {
            image_url: '',
            link_type: 'none',
            link_target: '',
            title: '',
            subtitle: '',
            sort_order: banners.value.length,
            is_active: true,
            start_at: '',
            end_at: '',
        }
    }
    showEditor.value = true
}

async function saveBanner() {
    if (!formData.value.image_url) {
        uni.showToast({ title: '请上传图片', icon: 'none' })
        return
    }
    if (!formData.value.title) {
        uni.showToast({ title: '请输入标题', icon: 'none' })
        return
    }

    saving.value = true
    try {
        if (isNewBanner.value) {
            await adminApi.createBanner(formData.value as BannerItem)
        } else if (editingBanner.value) {
            await adminApi.updateBanner(editingBanner.value.id, formData.value as BannerItem)
        }
        uni.showToast({ title: '保存成功', icon: 'success' })
        showEditor.value = false
        await fetchBanners()
    } catch (e: any) {
        uni.showToast({ title: e.message || '保存失败', icon: 'none' })
    } finally {
        saving.value = false
    }
}

async function deleteBanner(banner: BannerItem) {
    const confirm = await uni.showModal({
        title: '确认删除',
        content: `确定要删除"${banner.title}"吗？`,
        confirmColor: '#F44336',
    })
    if (!confirm.confirm) return

    try {
        await adminApi.deleteBanner(banner.id)
        uni.showToast({ title: '已删除', icon: 'success' })
        await fetchBanners()
    } catch (e: any) {
        uni.showToast({ title: e.message || '删除失败', icon: 'none' })
    }
}

async function toggleStatus(banner: BannerItem) {
    const newBanner = { ...banner, is_active: !banner.is_active }
    try {
        await adminApi.updateBanner(banner.id, newBanner)
        await fetchBanners()
    } catch (e: any) {
        uni.showToast({ title: e.message || '操作失败', icon: 'none' })
    }
}

async function moveUp(index: number) {
    if (index <= 0) return
    const items = banners.value.map((b, i) => ({
        id: b.id,
        sort_order: i === index ? i - 1 : i === index - 1 ? i + 1 : i
    }))
    try {
        await adminApi.reorderBanners(items)
        await fetchBanners()
    } catch (e: any) {
        uni.showToast({ title: e.message || '移动失败', icon: 'none' })
    }
}

async function moveDown(index: number) {
    if (index >= banners.value.length - 1) return
    const items = banners.value.map((b, i) => ({
        id: b.id,
        sort_order: i === index ? i + 1 : i === index + 1 ? i - 1 : i
    }))
    try {
        await adminApi.reorderBanners(items)
        await fetchBanners()
    } catch (e: any) {
        uni.showToast({ title: e.message || '移动失败', icon: 'none' })
    }
}

function chooseImage() {
    uni.chooseImage({
        count: 1,
        sizeType: ['compressed'],
        sourceType: ['album', 'camera'],
        success: (res) => {
            formData.value.image_url = res.tempFilePaths[0]
            // 实际应用中需要上传到服务器获取 URL
            uni.showToast({ title: '请上传图片到服务器获取 URL', icon: 'none' })
        }
    })
}

function onLinkTypeChange(e: any) {
    const index = e.detail.value
    if (index >= 0 && index < linkTypes.length) {
        formData.value.link_type = linkTypes[index].value as BannerItem['link_type']
    }
}

onMounted(() => {
    fetchBanners()
})
</script>

<template>
    <view class="page">
        <view class="header">
            <text class="header-title">轮播图管理</text>
            <button class="btn-add" @tap="openEditor">+ 新增</button>
        </view>

        <scroll-view class="banner-list" scroll-y :style="{ height: 'calc(100vh - 98px)' }" v-if="!loading">
            <view class="empty-state" v-if="banners.length === 0">
                <text class="empty-icon">🖼️</text>
                <text class="empty-text">暂无轮播图</text>
                <text class="empty-desc">点击"新增"添加首页轮播图</text>
                <button class="btn-add-main" @tap="openEditor">+ 新增轮播图</button>
            </view>

            <view class="banner-items" v-else>
                <view
                    class="banner-item"
                    v-for="(banner, index) in banners"
                    :key="banner.id"
                >
                    <view class="item-drag" @tap="moveUp(index)" style="margin-right: 8px;">↑</view>
                    <view class="item-drag" @tap="moveDown(index)">↓</view>

                    <image class="item-image" :src="banner.image_url" mode="aspectFill" />

                    <view class="item-info">
                        <view class="info-top">
                            <text class="item-title">{{ banner.title }}</text>
                            <view class="item-status">
                                <text class="status-badge" :class="{ active: banner.is_active }">
                                    {{ banner.is_active ? '启用' : '禁用' }}
                                </text>
                            </view>
                        </view>
                        <text class="item-subtitle" v-if="banner.subtitle">{{ banner.subtitle }}</text>
                        <view class="item-meta">
                            <text class="meta-item">链接: {{ getLinkTypeLabel(banner.link_type) }}</text>
                            <text class="meta-item" v-if="banner.link_target">目标: {{ banner.link_target }}</text>
                            <text class="meta-item">排序: {{ banner.sort_order }}</text>
                        </view>
                    </view>

                    <view class="item-actions">
                        <button class="action-btn toggle" @tap="toggleStatus(banner)" size="mini">
                            {{ banner.is_active ? '禁用' : '启用' }}
                        </button>
                        <button class="action-btn edit" @tap="openEditor(banner)" size="mini">编辑</button>
                        <button class="action-btn delete" @tap="deleteBanner(banner)" size="mini">删除</button>
                    </view>
                </view>
            </view>
        </scroll-view>

        <view class="loading-overlay" v-if="loading">
            <view class="spinner" />
        </view>

        <!-- 编辑弹窗 -->
        <view class="modal-mask" v-if="showEditor" @tap="showEditor = false">
            <view class="modal editor-modal" @tap="onModalTap">
                <view class="modal-header">
                    <text class="modal-title">{{ isNewBanner ? '新增轮播图' : '编辑轮播图' }}</text>
                    <text class="modal-close" @tap="showEditor = false">✕</text>
                </view>

                <scroll-view class="modal-body" scroll-y>
                    <view class="form-section">
                        <view class="form-group">
                            <label class="form-label">轮播图片 *</label>
                            <view class="image-picker" @tap="chooseImage">
                                <image class="preview-image" :src="formData.image_url" mode="aspectFill" v-if="formData.image_url" />
                                <view class="picker-placeholder" v-else>
                                    <text class="picker-icon">📷</text>
                                    <text>点击上传图片</text>
                                </view>
                            </view>
                            <text class="form-hint">建议尺寸 750×350，支持 jpg/png，不超过 2MB</text>
                        </view>

                        <view class="form-group">
                            <label class="form-label">标题 *</label>
                            <input class="form-input" v-model="formData.title" placeholder="请输入标题" maxlength="30" />
                        </view>

                        <view class="form-group">
                            <label class="form-label">副标题</label>
                            <input class="form-input" v-model="formData.subtitle" placeholder="请输入副标题" maxlength="50" />
                        </view>

                        <view class="form-group">
                            <label class="form-label">链接类型</label>
                            <picker class="form-picker" mode="selector" :value="linkTypes.findIndex(t => t.value === formData.link_type)" @change="onLinkTypeChange">
                                <view class="picker-display">
                                    <text>{{ getLinkTypeLabel(formData.link_type || 'none') }}</text>
                                    <text class="arrow">▼</text>
                                </view>
                            </picker>
                        </view>

                        <view class="form-group" v-if="formData.link_type !== 'none'">
                            <label class="form-label">链接目标</label>
                            <input class="form-input" v-model="formData.link_target" placeholder="商品ID/分类ID/团购ID/URL地址" />
                            <text class="form-hint" v-if="formData.link_type === 'url'">请输入完整 URL（含 http:// 或 https://）</text>
                        </view>

                        <view class="form-group">
                            <label class="form-label">排序权重</label>
                            <input class="form-input" v-model.number="formData.sort_order" type="number" placeholder="数值越小越靠前" />
                        </view>

                        <view class="form-group">
                            <label class="form-label">生效时间</label>
                            <picker class="form-picker" mode="datetime" :value="formData.start_at" @change="formData.start_at = $event.detail.value" v-if="formData.start_at">
                                <view class="picker-display">
                                    <text>{{ formData.start_at }}</text>
                                </view>
                            </picker>
                            <picker class="form-picker" mode="datetime" @change="formData.start_at = $event.detail.value" v-else>
                                <view class="picker-display">
                                    <text class="placeholder">不限制</text>
                                </view>
                            </picker>
                        </view>

                        <view class="form-group">
                            <label class="form-label">失效时间</label>
                            <picker class="form-picker" mode="datetime" :value="formData.end_at" @change="formData.end_at = $event.detail.value" v-if="formData.end_at">
                                <view class="picker-display">
                                    <text>{{ formData.end_at }}</text>
                                </view>
                            </picker>
                            <picker class="form-picker" mode="datetime" @change="formData.end_at = $event.detail.value" v-else>
                                <view class="picker-display">
                                    <text class="placeholder">不限制</text>
                                </view>
                            </picker>
                        </view>

                        <view class="form-group switch-group">
                            <text class="switch-label">启用状态</text>
                            <switch :checked="formData.is_active" @change="formData.is_active = $event.detail.value" />
                        </view>
                    </view>
                </scroll-view>

                <view class="modal-footer">
                    <button class="btn-cancel" @tap="showEditor = false">取消</button>
                    <button class="btn-confirm" @tap="saveBanner" :disabled="saving">
                        <text v-if="!saving">保存</text>
                        <text v-else>保存中...</text>
                    </button>
                </view>
            </view>
        </view>
    </view>
</template>

<script lang="ts">
function getLinkTypeLabel(type: string): string {
    const map: Record<string, string> = {
        none: '无链接',
        product: '商品详情',
        category: '分类页面',
        pintuan: '团购活动',
        pickup: '自提点',
        url: '自定义链接'
    }
    return map[type] || type
}
</script>

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

.banner-list {
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
}

.empty-icon {
    font-size: 48px;
    margin-bottom: 16px;
}

.empty-text {
    font-size: 16px;
    font-weight: 500;
    color: #1A1A1A;
    margin-bottom: 8px
}

.empty-desc {
    font-size: 13px;
    color: #9E9E9E;
    margin-bottom: 24px;
}

.btn-add-main {
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 20px;
    padding: 10px 24px;
    font-size: 14px;
    font-weight: 500;
}

.banner-items {
    display: flex;
    flex-direction: column;
    gap: 12px;
}

.banner-item {
    background: #FFFFFF;
    border-radius: 12px;
    padding: 12px;
    display: flex;
    gap: 12px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.04);
}

.item-drag {
    width: 32px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 8px;
    color: #9E9E9E;
    font-size: 16px;
}

.item-image {
    width: 100px;
    height: 56px;
    border-radius: 8px;
    object-fit: cover;
    flex-shrink: 0;
}

.item-info {
    flex: 1;
    min-width: 0;
    display: flex;
    flex-direction: column;
    justify-content: center;
    gap: 4px;
}

.info-top {
    display: flex;
    align-items: center;
    justify-content: space-between;
}

.item-title {
    font-size: 15px;
    font-weight: 500;
    color: #1A1A1A;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.item-subtitle {
    font-size: 12px;
    color: #9E9E9E;
    max-width: 200px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}

.item-meta {
    display: flex;
    flex-wrap: wrap;
    gap: 12px;
    margin-top: 4px;
}

.meta-item {
    font-size: 11px;
    color: #9E9E9E;
}

.status-badge {
    padding: 2px 8px;
    border-radius: 10px;
    font-size: 10px;
    font-weight: 500;
}

.status-badge.active {
    background: #E8F5E9;
    color: #2E7D32;
}

.status-badge:not(.active) {
    background: #F5F5F5;
    color: #9E9E9E;
}

.item-actions {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.action-btn {
    width: 60px;
    height: 28px;
    font-size: 11px;
    border-radius: 4px;
}

.action-btn.toggle {
    background: #E3F2FD;
    color: #1565C0;
    border: none;
}

.action-btn.edit {
    background: #FFF3E0;
    color: #E65100;
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

.form-input:focus {
    border-color: #2E7D32;
    background: #FFFFFF;
}

.form-picker {
    height: 44px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    background: #FAFAFA;
}

.picker-display {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 0 12px;
    height: 100%;
    font-size: 14px;
    color: #1A1A1A;
}

.picker-display .placeholder {
    color: #9E9E9E;
}

.arrow {
    font-size: 12px;
    color: #9E9E9E;
}

.form-hint {
    font-size: 11px;
    color: #9E9E9E;
}

.image-picker {
    width: 100%;
    height: 160px;
    border: 1px dashed #E0E0E0;
    border-radius: 8px;
    display: flex;
    align-items: center;
    justify-content: center;
    overflow: hidden;
}

.preview-image {
    width: 100%;
    height: 100%;
    object-fit: cover;
}

.picker-placeholder {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 8px;
    color: #9E9E9E;
}

.picker-icon {
    font-size: 24px;
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
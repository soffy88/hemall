<script setup lang="ts">
import { ref } from 'vue'
import { authApi } from '@/utils/api'
import { getToken, saveToken } from '@/utils/request'

const username = ref('')
const password = ref('')
const loading = ref(false)
const error = ref('')

async function handleLogin() {
    if (!username.value || !password.value) {
        error.value = '请输入用户名和密码'
        return
    }
    loading.value = true
    error.value = ''
    try {
        // 这里简化处理：实际应该调用后台登录接口
        // 暂时使用微信登录作为演示
        uni.showToast({ title: '请使用微信登录', icon: 'none' })
        uni.navigateTo({ url: '/pages/login/login?redirect=admin_banner' })
    } catch (e: any) {
        error.value = e.message || '登录失败'
    } finally {
        loading.value = false
    }
}

function goWechatLogin() {
    uni.navigateTo({ url: '/pages/login/login?redirect=admin_banner' })
}
</script>

<template>
    <view class="page">
        <view class="login-container">
            <view class="logo">
                <text class="logo-icon">🌿</text>
                <text class="logo-text">宋鲜生后台</text>
            </view>

            <view class="form-section">
                <view class="form-group">
                    <label class="form-label">管理员账号</label>
                    <input class="form-input" v-model="username" placeholder="请输入用户名" @confirm="handleLogin" />
                </view>

                <view class="form-group">
                    <label class="form-label">密码</label>
                    <input class="form-input" v-model="password" type="password" placeholder="请输入密码" @confirm="handleLogin" />
                </view>

                <view class="error-message" v-if="error">{{ error }}</view>

                <button class="btn-login" @tap="handleLogin" :disabled="loading">
                    <text v-if="!loading">登录</text>
                    <text v-else>登录中...</text>
                </button>
            </view>

            <view class="divider">
                <text>或</text>
            </view>

            <button class="btn-wechat" @tap="goWechatLogin">
                <text class="wechat-icon">💚</text>
                <text>微信授权登录</text>
            </button>

            <view class="hint">
                管理员请使用微信登录，系统将自动校验权限
            </view>
        </view>
    </view>
</template>

<style scoped>
.page {
    background: #F5F5F5;
    height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 24px;
}

.login-container {
    width: 100%;
    max-width: 360px;
    background: #FFFFFF;
    border-radius: 16px;
    padding: 32px 24px;
    box-shadow: 0 4px 20px rgba(0, 0, 0, 0.08);
}

.logo {
    display: flex;
    flex-direction: column;
    align-items: center;
    margin-bottom: 32px;
}

.logo-icon {
    font-size: 48px;
    margin-bottom: 8px;
}

.logo-text {
    font-size: 22px;
    font-weight: 600;
    color: #1A1A1A;
}

.form-section {
    display: flex;
    flex-direction: column;
    gap: 16px;
}

.form-group {
    display: flex;
    flex-direction: column;
    gap: 6px;
}

.form-label {
    font-size: 14px;
    color: #616161;
    font-weight: 500;
}

.form-input {
    height: 48px;
    padding: 0 16px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 16px;
    color: #1A1A1A;
    background: #FAFAFA;
}

.form-input:focus {
    border-color: #2E7D32;
    background: #FFFFFF;
}

.error-message {
    color: #F44336;
    font-size: 13px;
    text-align: center;
    margin-top: -8px;
}

.btn-login {
    height: 48px;
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    font-size: 16px;
    font-weight: 600;
    margin-top: 8px;
}

.btn-login:disabled {
    background: #A5D6A7;
}

.divider {
    display: flex;
    align-items: center;
    gap: 12px;
    margin: 24px 0;
    color: #9E9E9E;
    font-size: 13px;
}

.divider::before,
.divider::after {
    content: "";
    flex: 1;
    height: 1px;
    background: #E0E0E0;
}

.btn-wechat {
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    height: 48px;
    background: #FFFFFF;
    color: #1A1A1A;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 16px;
    font-weight: 500;
}

.wechat-icon {
    font-size: 18px;
}

.hint {
    text-align: center;
    margin-top: 20px;
    font-size: 12px;
    color: #9E9E9E;
}
</style>
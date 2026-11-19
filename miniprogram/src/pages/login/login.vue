<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { authApi } from '@/utils/api'
import { wxLogin, saveToken, getToken } from '@/utils/request'

const loading = ref(false)
const agreeing = ref(true)
const redirectUrl = ref('')

// Helper functions
function goPhoneLogin() {
    uni.showToast({ title: '手机验证码登录开发中', icon: 'none' })
}

async function handleWeChatLogin() {
    if (!agreeing.value) {
        uni.showToast({ title: '请先阅读并同意用户协议和隐私政策', icon: 'none' })
        return
    }

    loading.value = true
    try {
        const { code } = await wxLogin()
        const res = await authApi.wechatLogin(code)
        saveToken(res)
        uni.showToast({ title: '登录成功', icon: 'success' })

        // 跳转回原页面或首页
        setTimeout(() => {
            if (redirectUrl.value) {
                uni.redirectTo({ url: redirectUrl.value })
            } else {
                uni.switchTab({ url: '/pages/index/index' })
            }
        }, 500)
    } catch (e: any) {
        uni.showToast({ title: e.message || '登录失败', icon: 'none' })
    } finally {
        loading.value = false
    }
}

function goAgreement(type: 'user' | 'privacy') {
    uni.showModal({
        title: type === 'user' ? '用户协议' : '隐私政策',
        content: '这里是' + (type === 'user' ? '用户协议' : '隐私政策') + '的详细内容...',
        showCancel: false
    })
}

onMounted(() => {
    // 检查是否已登录
    const token = getToken()
    if (token) {
        uni.switchTab({ url: '/pages/index/index' })
    }

    // 获取重定向参数
    const pages = getCurrentPages()
    if (pages.length > 0) {
        const options = pages[pages.length - 1].$page?.options || {}
        if (options.redirect) {
            redirectUrl.value = '/' + decodeURIComponent(options.redirect)
        }
    }
})
</script>

<template>
    <view class="page">
        <view class="login-container">
            <!-- Logo -->
            <view class="logo-section">
                <image class="logo" src="/static/logo.png" mode="aspectFit" />
                <text class="brand-name">宋鲜生</text>
                <text class="brand-slogan">新鲜到家，品质生活</text>
            </view>

            <!-- 微信登录按钮 -->
            <button class="btn-wechat" @tap="handleWeChatLogin" :disabled="loading" open-type="getUserInfo" lang="zh_CN">
                <view class="btn-content" v-if="!loading">
                    <image class="wechat-icon" src="/static/icons/wechat.png" mode="aspectFit" />
                    <text>微信一键登录</text>
                </view>
                <view class="btn-content" v-else>
                    <uni-loading-icon size="20" color="#FFFFFF" />
                    <text>登录中...</text>
                </view>
            </button>

            <!-- 协议勾选 -->
            <view class="agreement-row">
                <checkbox class="agreement-checkbox" :checked="agreeing" @change="agreeing = $event.detail.value" />
                <view class="agreement-text">
                    我已阅读并同意
                    <text class="agreement-link" @tap="goAgreement('user')">《用户协议》</text>
                    和
                    <text class="agreement-link" @tap="goAgreement('privacy')">《隐私政策》</text>
                </view>
            </view>

            <!-- 其他登录方式占位 -->
            <view class="divider-row">
                <view class="divider-line" />
                <text class="divider-text">其他方式登录</text>
                <view class="divider-line" />
            </view>

            <view class="other-login">
                <button class="btn-phone" @tap="goPhoneLogin">
                    <text class="phone-icon">📱</text>
                    <text>手机验证码登录</text>
                </button>
            </view>

            <!-- 版本信息 -->
            <text class="version">版本 1.0.0</text>
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
}

.logo-section {
    text-align: center;
    margin-bottom: 48px;
}

.logo {
    width: 100px;
    height: 100px;
    border-radius: 20px;
    margin-bottom: 16px;
}

.brand-name {
    display: block;
    font-size: 28px;
    font-weight: 700;
    color: #1A1A1A;
    margin-bottom: 8px;
}

.brand-slogan {
    font-size: 14px;
    color: #9E9E9E;
}

.btn-wechat {
    width: 100%;
    height: 52px;
    background: #07C160;
    border: none;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    margin-bottom: 20px;
}

.btn-wechat:disabled {
    background: #81D8A0;
}

.btn-content {
    display: flex;
    align-items: center;
    gap: 10px;
    color: #FFFFFF;
    font-size: 17px;
    font-weight: 600;
}

.wechat-icon {
    width: 24px;
    height: 24px;
}

.agreement-row {
    display: flex;
    align-items: flex-start;
    margin-bottom: 24px;
}

.agreement-checkbox {
    margin-top: 4px;
    margin-right: 8px;
}

.agreement-text {
    font-size: 12px;
    color: #9E9E9E;
    line-height: 1.5;
}

.agreement-link {
    color: #2E7D32;
}

.divider-row {
    display: flex;
    align-items: center;
    margin-bottom: 20px;
}

.divider-line {
    flex: 1;
    height: 1px;
    background: #E0E0E0;
}

.divider-text {
    padding: 0 16px;
    font-size: 13px;
    color: #9E9E9E;
}

.other-login {
    margin-bottom: 40px;
}

.btn-phone {
    width: 100%;
    height: 48px;
    background: #FFFFFF;
    border: 1px solid #E0E0E0;
    border-radius: 12px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 10px;
    color: #616161;
    font-size: 15px;
}

.phone-icon {
    font-size: 18px;
}

.version {
    text-align: center;
    font-size: 12px;
    color: #9E9E9E;
}
</style>
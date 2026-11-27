<script setup lang="ts">
import { onLaunch, onShow, onHide } from '@dcloudio/uni-app'
import { getToken, wxLogin, saveToken } from './utils/request'
import { authApi } from './utils/api'

onLaunch(() => {
    console.log('App Launch - 宋鲜生小程序启动')
    // 检查 Token 是否存在，不存在则尝试静默登录
    const token = getToken()
    if (!token) {
        // 静默登录（不跳转登录页，仅获取 code 换 token）
        silentLogin()
    }
})

onShow(() => {
    console.log('App Show')
})

onHide(() => {
    console.log('App Hide')
})

// 静默登录：wx.login -> 后端 wechat-login
async function silentLogin() {
    try {
        const { code } = await wxLogin()
        const res = await authApi.wechatLogin(code)
        saveToken(res)
        console.log('静默登录成功', res.customer_id)
    } catch (e) {
        console.warn('静默登录失败，等待用户主动登录:', e)
    }
}
</script>

<style>
/* 全局样式 */
page {
    background-color: #F5F5F5;
    font-family: -apple-system, BlinkMacSystemFont, 'Helvetica Neue', Helvetica, 'PingFang SC', 'Hiragino Sans GB', 'Microsoft YaHei', Arial, sans-serif;
}

/* 通用间距 */
.page-section {
    padding: 16px;
    background: #FFFFFF;
    margin-bottom: 8px;
}

.page-section-title {
    font-size: 16px;
    font-weight: 600;
    color: #1A1A1A;
    margin-bottom: 12px;
}

/* 通用按钮 */
.btn-primary {
    background: #2E7D32;
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 12px 24px;
    font-size: 16px;
    font-weight: 500;
}

.btn-primary:disabled {
    background: #A5D6A7;
}

.btn-secondary {
    background: #FFFFFF;
    color: #2E7D32;
    border: 1px solid #2E7D32;
    border-radius: 8px;
    padding: 12px 24px;
    font-size: 16px;
    font-weight: 500;
}

.btn-danger {
    background: #F44336;
    color: #FFFFFF;
    border: none;
    border-radius: 8px;
    padding: 12px 24px;
    font-size: 16px;
    font-weight: 500;
}

/* 通用输入框 */
.input-field {
    width: 100%;
    padding: 12px 16px;
    border: 1px solid #E0E0E0;
    border-radius: 8px;
    font-size: 15px;
    color: #1A1A1A;
    background: #FFFFFF;
    box-sizing: border-box;
}

.input-field:focus {
    border-color: #2E7D32;
    outline: none;
}

.input-field::placeholder {
    color: #9E9E9E;
}

/* 通用卡片 */
.card {
    background: #FFFFFF;
    border-radius: 12px;
    box-shadow: 0 2px 8px rgba(0, 0, 0, 0.06);
    overflow: hidden;
}

.card-image {
    width: 100%;
    aspect-ratio: 1 / 1;
    object-fit: cover;
}

/* 价格样式 */
.price {
    color: #2E7D32;
    font-weight: 600;
}

.price-original {
    color: #9E9E9E;
    text-decoration: line-through;
    font-size: 13px;
    margin-left: 8px;
}

.price-unit {
    font-size: 12px;
    font-weight: 400;
    color: #9E9E9E;
}

/* 标签 */
.tag {
    display: inline-block;
    padding: 2px 8px;
    border-radius: 4px;
    font-size: 11px;
    font-weight: 500;
}

.tag-primary {
    background: #E8F5E9;
    color: #2E7D32;
}

.tag-warning {
    background: #FFF3E0;
    color: #E65100;
}

.tag-danger {
    background: #FBE9E7;
    color: #C62828;
}

.tag-info {
    background: #E3F2FD;
    color: #1565C0;
}

/* 空状态 */
.empty-state {
    display: flex;
    flex-direction: column;
    align-items: center;
    padding: 48px 24px;
    color: #9E9E9E;
}

.empty-state-icon {
    width: 80px;
    height: 80px;
    margin-bottom: 16px;
    opacity: 0.5;
}

.empty-state-text {
    font-size: 15px;
    text-align: center;
}

/* 分割线 */
.divider {
    height: 1px;
    background: #EEEEEE;
    margin: 0 16px;
}

.divider-full {
    height: 1px;
    background: #EEEEEE;
}

/* 加载中 */
.loading-container {
    display: flex;
    justify-content: center;
    align-items: center;
    padding: 48px 0;
}

/* 底部安全区适配 */
.safe-area-bottom {
    padding-bottom: env(safe-area-inset-bottom, 34px);
}
</style>
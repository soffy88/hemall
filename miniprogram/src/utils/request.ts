// 宋鲜生小程序请求封装
// 支持：Token 自动携带、自动刷新、错误统一处理、Loading 提示

import { API_BASE_URL, REQUEST_TIMEOUT, TOKEN_KEY, REFRESH_TOKEN_KEY, USER_INFO_KEY } from '../config'
import type { WeChatLoginResponse, ApiResponse } from '../types'

// 请求队列（用于 Token 刷新期间的请求排队）
let isRefreshing = false
let pendingRequests: Array<() => void> = []

// 获取存储的 Token
function getToken(): string | null {
    return uni.getStorageSync(TOKEN_KEY)
}

// 获取 Refresh Token
function getRefreshToken(): string | null {
    return uni.getStorageSync(REFRESH_TOKEN_KEY)
}

// 保存 Token
function saveToken(data: WeChatLoginResponse): void {
    uni.setStorageSync(TOKEN_KEY, data.access_token)
    if (data.refresh_token) {
        uni.setStorageSync(REFRESH_TOKEN_KEY, data.refresh_token)
    }
    uni.setStorageSync(USER_INFO_KEY, {
        customer_id: data.customer_id,
        email: data.email,
        wechat_bound: data.wechat_bound
    })
}

// 清除 Token
function clearToken(): void {
    uni.removeStorageSync(TOKEN_KEY)
    uni.removeStorageSync(REFRESH_TOKEN_KEY)
    uni.removeStorageSync(USER_INFO_KEY)
}

// 刷新 Token（微信小程序登录无刷新机制，这里重新走 wechat-login）
async function refreshAccessToken(): Promise<string | null> {
    try {
        // 重新调用 wx.login 获取新 code
        const loginRes = await wxLogin()
        const res = await request<WeChatLoginResponse>('/store/customers/wechat-login', {
            method: 'POST',
            data: { code: loginRes.code }
        })
        if (res.access_token) {
            saveToken(res)
            return res.access_token
        }
        return null
    } catch (e) {
        console.error('Token 刷新失败:', e)
        return null
    }
}

// 微信登录获取 code
function wxLogin(): Promise<{ code: string }> {
    return new Promise((resolve, reject) => {
        uni.login({
            provider: 'weixin',
            onlyAuthorize: true,
            success: (res) => {
                if (res.code) {
                    resolve({ code: res.code })
                } else {
                    reject(new Error('wx.login 失败: ' + res.errMsg))
                }
            },
            fail: (err) => reject(new Error('wx.login 调用失败: ' + err.errMsg))
        })
    })
}

// 处理 401 错误：尝试刷新 Token 并重试
async function handle401(requestFn: () => Promise<any>): Promise<any> {
    if (isRefreshing) {
        // 正在刷新，加入队列等待
        return new Promise((resolve, reject) => {
            pendingRequests.push(() => {
                requestFn().then(resolve).catch(reject)
            })
        })
    }

    isRefreshing = true

    try {
        const newToken = await refreshAccessToken()
        if (!newToken) {
            // 刷新失败，清除 Token 并跳转登录页
            clearToken()
            uni.reLaunch({ url: '/pages/login/login' })
            throw new Error('登录已过期，请重新登录')
        }

        // 刷新成功，重试原请求
        const result = await requestFn()

        // 执行队列中等待的请求
        pendingRequests.forEach(cb => cb())
        pendingRequests = []

        return result
    } catch (e) {
        pendingRequests.forEach(cb => cb())
        pendingRequests = []
        throw e
    } finally {
        isRefreshing = false
    }
}

// 核心请求函数
interface RequestOptions {
    method?: 'GET' | 'POST' | 'PUT' | 'DELETE'
    data?: any
    header?: Record<string, string>
    showLoading?: boolean
    loadingText?: string
    hideErrorToast?: boolean
}

export async function request<T = any>(
    url: string,
    options: RequestOptions = {}
): Promise<T> {
    const {
        method = 'GET',
        data,
        header = {},
        showLoading = false,
        loadingText = '加载中...',
        hideErrorToast = false
    } = options

    // 显示 Loading
    if (showLoading) {
        uni.showLoading({ title: loadingText, mask: true })
    }

    // 构造请求头
    const requestHeader: Record<string, string> = {
        'Content-Type': 'application/json',
        ...header
    }

    // 自动携带 Token
    const token = getToken()
    if (token) {
        requestHeader['Authorization'] = `Bearer ${token}`
    }

    const fullUrl = url.startsWith('http') ? url : `${API_BASE_URL}${url}`

    return new Promise((resolve, reject) => {
        uni.request({
            url: fullUrl,
            method,
            data,
            header: requestHeader,
            timeout: REQUEST_TIMEOUT,
            success: async (res) => {
                if (showLoading) uni.hideLoading()

                const statusCode = res.statusCode
                const responseData = res.data

                // HTTP 2xx
                if (statusCode >= 200 && statusCode < 300) {
                    resolve(responseData as T)
                    return
                }

                // 401：Token 失效，尝试刷新
                if (statusCode === 401) {
                    if (showLoading) uni.hideLoading()
                    try {
                        const result = await handle401(() => request<T>(url, options))
                        resolve(result)
                    } catch (e) {
                        reject(e)
                    }
                    return
                }

                // 其他错误
                const errorMsg = (responseData as any)?.error?.message ||
                    (responseData as any)?.detail ||
                    (responseData as any)?.message ||
                    `请求失败 (${statusCode})`

                if (!hideErrorToast) {
                    uni.showToast({ title: errorMsg, icon: 'none', duration: 3000 })
                }

                reject(new Error(errorMsg))
            },
            fail: (err) => {
                if (showLoading) uni.hideLoading()
                const errorMsg = err.errMsg || '网络请求失败'
                if (!hideErrorToast) {
                    uni.showToast({ title: errorMsg, icon: 'none', duration: 3000 })
                }
                reject(new Error(errorMsg))
            }
        })
    })
}

// 便捷方法
export const http = {
    get<T = any>(url: string, options?: Omit<RequestOptions, 'method' | 'data'>) {
        return request<T>(url, { ...options, method: 'GET' })
    },
    post<T = any>(url: string, data?: any, options?: Omit<RequestOptions, 'method' | 'data'>) {
        return request<T>(url, { ...options, method: 'POST', data })
    },
    put<T = any>(url: string, data?: any, options?: Omit<RequestOptions, 'method' | 'data'>) {
        return request<T>(url, { ...options, method: 'PUT', data })
    },
    delete<T = any>(url: string, options?: Omit<RequestOptions, 'method' | 'data'>) {
        return request<T>(url, { ...options, method: 'DELETE' })
    }
}

// 导出工具函数
export { getToken, saveToken, clearToken, wxLogin }
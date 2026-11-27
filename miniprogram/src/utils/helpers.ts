// 宋鲜生小程序通用工具函数

// ==================== 价格格式化 ====================

/**
 * 分转元，保留两位小数
 * @param cents 价格（分）
 * @returns 格式化后的价格字符串，如 "25.99"
 */
export function formatPrice(cents: number | null | undefined): string {
    if (cents === null || cents === undefined || isNaN(cents)) {
        return '0.00'
    }
    return (cents / 100).toFixed(2)
}

/**
 * 分转元，不保留小数（整数时）
 * @param cents 价格（分）
 * @returns 格式化后的价格字符串，如 "25" 或 "25.99"
 */
export function formatPriceSmart(cents: number | null | undefined): string {
    if (cents === null || cents === undefined || isNaN(cents)) {
        return '0'
    }
    const yuan = cents / 100
    return yuan % 1 === 0 ? String(yuan) : yuan.toFixed(2)
}

/**
 * 带货币符号的价格格式化
 * @param cents 价格（分）
 * @param symbol 货币符号，默认 "¥"
 * @returns 格式化后的价格字符串，如 "¥25.99"
 */
export function formatPriceWithSymbol(cents: number | null | undefined, symbol = '¥'): string {
    return `${symbol}${formatPrice(cents)}`
}

// ==================== 日期格式化 ====================

/**
 * 格式化日期时间
 * @param dateString ISO 日期字符串
 * @param format 格式模板，默认 "YYYY-MM-DD HH:mm"
 * @returns 格式化后的日期字符串
 */
export function formatDateTime(dateString: string | null | undefined, format = 'YYYY-MM-DD HH:mm'): string {
    if (!dateString) return '-'
    const date = new Date(dateString)
    if (isNaN(date.getTime())) return '-'

    const year = date.getFullYear()
    const month = String(date.getMonth() + 1).padStart(2, '0')
    const day = String(date.getDate()).padStart(2, '0')
    const hours = String(date.getHours()).padStart(2, '0')
    const minutes = String(date.getMinutes()).padStart(2, '0')
    const seconds = String(date.getSeconds()).padStart(2, '0')

    return format
        .replace('YYYY', String(year))
        .replace('MM', month)
        .replace('DD', day)
        .replace('HH', hours)
        .replace('mm', minutes)
        .replace('ss', seconds)
}

/**
 * 格式化日期（仅年月日）
 * @param dateString ISO 日期字符串
 * @returns 格式化后的日期字符串，如 "2024-01-15"
 */
export function formatDate(dateString: string | null | undefined): string {
    return formatDateTime(dateString, 'YYYY-MM-DD')
}

/**
 * 格式化时间（仅时分）
 * @param dateString ISO 日期字符串
 * @returns 格式化后的时间字符串，如 "14:30"
 */
export function formatTime(dateString: string | null | undefined): string {
    return formatDateTime(dateString, 'HH:mm')
}

/**
 * 格式化时长（分钟转为 "X小时Y分钟" 或 "X分钟"）
 * @param minutes 分钟数
 * @returns 格式化后的时长字符串
 */
export function formatDuration(minutes: number | null | undefined): string {
    if (!minutes || minutes <= 0) return '-'
    const hours = Math.floor(minutes / 60)
    const mins = minutes % 60
    if (hours > 0 && mins > 0) {
        return `${hours}小时${mins}分钟`
    }
    if (hours > 0) {
        return `${hours}小时`
    }
    return `${mins}分钟`
}

/**
 * 相对时间（如 "刚刚"、"5分钟前"、"2小时前"、"3天前"）
 * @param dateString ISO 日期字符串
 * @returns 相对时间字符串
 */
export function formatRelativeTime(dateString: string | null | undefined): string {
    if (!dateString) return '-'
    const date = new Date(dateString)
    if (isNaN(date.getTime())) return '-'

    const now = new Date()
    const diffMs = now.getTime() - date.getTime()
    const diffSec = Math.floor(diffMs / 1000)
    const diffMin = Math.floor(diffSec / 60)
    const diffHour = Math.floor(diffMin / 60)
    const diffDay = Math.floor(diffHour / 24)

    if (diffSec < 60) return '刚刚'
    if (diffMin < 60) return `${diffMin}分钟前`
    if (diffHour < 24) return `${diffHour}小时前`
    if (diffDay < 30) return `${diffDay}天前`
    return formatDate(dateString)
}

// ==================== 订单状态文案 ====================

export const ORDER_STATUS_LABELS: Record<string, string> = {
    pending: '待支付',
    payment_pending: '待支付',
    paid: '已支付',
    confirmed: '已确认',
    preparing: '备货中',
    shipped: '已发货',
    delivering: '配送中',
    completed: '已完成',
    cancelled: '已取消',
    refunded: '已退款',
    draft: '草稿',
    preorder: '预订中'
}

export const ORDER_STATUS_COLORS: Record<string, string> = {
    pending: '#FF9800',
    payment_pending: '#FF9800',
    paid: '#2196F3',
    confirmed: '#2196F3',
    preparing: '#2196F3',
    shipped: '#9C27B0',
    delivering: '#9C27B0',
    completed: '#4CAF50',
    cancelled: '#9E9E9E',
    refunded: '#9E9E9E',
    draft: '#9E9E9E',
    preorder: '#FF9800'
}

/**
 * 获取订单状态显示文案
 * @param status 订单状态
 * @returns 显示文案
 */
export function getOrderStatusLabel(status: string): string {
    return ORDER_STATUS_LABELS[status] || status
}

/**
 * 获取订单状态颜色
 * @param status 订单状态
 * @returns 颜色值
 */
export function getOrderStatusColor(status: string): string {
    return ORDER_STATUS_COLORS[status] || '#9E9E9E'
}

// ==================== 支付状态文案 ====================

export const PAYMENT_STATUS_LABELS: Record<string, string> = {
    pending: '待支付',
    provider_pending: '支付处理中',
    paid: '支付成功',
    failed: '支付失败',
    partial_refund: '部分退款',
    refunded: '已退款',
    cancelled: '已取消'
}

export function getPaymentStatusLabel(status: string): string {
    return PAYMENT_STATUS_LABELS[status] || status
}

// ==================== 规格解析 ====================

/**
 * 从 option_values 解析规格文本（用于显示"按实际重量多退少补"等提示）
 * @param optionValues 规格值对象
 * @returns 规格文本数组
 */
export function parseOptionValues(optionValues: Record<string, any> | null | undefined): string[] {
    if (!optionValues) return []
    return Object.values(optionValues)
        .filter(v => v !== null && v !== undefined && v !== '')
        .map(v => String(v))
}

/**
 * 判断是否为称重商品（规格中含 g/kg 单位）
 * @param optionValues 规格值对象
 * @returns 是否为称重商品
 */
export function isWeightProduct(optionValues: Record<string, any> | null | undefined): boolean {
    const texts = parseOptionValues(optionValues)
    return texts.some(text => /\d+\.?\d*\s*(g|kg)/i.test(text))
}

/**
 * 从规格文本提取计价基准（克）
 * @param optionValues 规格值对象
 * @returns 计价基准（克），无法解析返回 null
 */
export function extractWeightBasis(optionValues: Record<string, any> | null | undefined): number | null {
    const texts = parseOptionValues(optionValues)
    for (const text of texts) {
        const match = text.match(/(\d+(?:\.\d+)?)\s*(kg|g)/i)
        if (match) {
            const value = parseFloat(match[1])
            const unit = match[2].toLowerCase()
            return unit === 'kg' ? Math.round(value * 1000) : Math.round(value)
        }
    }
    return null
}

// ==================== 图片处理 ====================

/**
 * 获取商品主图 URL
 * @param product 商品对象
 * @returns 图片 URL 或默认占位图
 */
export function getProductMainImage(product: { variants?: Array<{ batches?: Array<{ shelf_image_url?: string }> }> }): string {
    if (!product?.variants?.length) return '/static/images/placeholder.png'
    for (const variant of product.variants) {
        if (variant.batches?.length) {
            for (const batch of variant.batches) {
                if (batch.shelf_image_url) return batch.shelf_image_url
            }
        }
    }
    return '/static/images/placeholder.png'
}

/**
 * 获取变体主图 URL
 * @param variant 变体对象
 * @returns 图片 URL 或默认占位图
 */
export function getVariantMainImage(variant: { batches?: Array<{ shelf_image_url?: string }> }): string {
    if (!variant?.batches?.length) return '/static/images/placeholder.png'
    for (const batch of variant.batches) {
        if (batch.shelf_image_url) return batch.shelf_image_url
    }
    return '/static/images/placeholder.png'
}

// ==================== 库存状态 ====================

export const STOCK_STATUS = {
    IN_STOCK: 'in_stock',
    LOW_STOCK: 'low_stock',
    OUT_OF_STOCK: 'out_of_stock',
    PREORDER: 'preorder'
} as const

export type StockStatus = typeof STOCK_STATUS[keyof typeof STOCK_STATUS]

/**
 * 判断库存状态
 * @param totalStock 总库存
 * @param preorderQty 预订量（可选）
 * @returns 库存状态
 */
export function getStockStatus(totalStock: number, preorderQty = 0): StockStatus {
    if (totalStock <= 0) {
        return preorderQty > 0 ? STOCK_STATUS.PREORDER : STOCK_STATUS.OUT_OF_STOCK
    }
    if (totalStock <= 10) return STOCK_STATUS.LOW_STOCK
    return STOCK_STATUS.IN_STOCK
}

/**
 * 获取库存状态显示文案
 * @param status 库存状态
 * @returns 显示文案
 */
export function getStockStatusLabel(status: StockStatus): string {
    const labels: Record<StockStatus, string> = {
        in_stock: '有货',
        low_stock: '库存紧张',
        out_of_stock: '缺货',
        preorder: '可预订'
    }
    return labels[status]
}

/**
 * 获取库存状态颜色
 * @param status 库存状态
 * @returns 颜色值
 */
export function getStockStatusColor(status: StockStatus): string {
    const colors: Record<StockStatus, string> = {
        in_stock: '#4CAF50',
        low_stock: '#FF9800',
        out_of_stock: '#F44336',
        preorder: '#2196F3'
    }
    return colors[status]
}

// ==================== 表单验证 ====================

/**
 * 手机号验证（中国大陆）
 * @param phone 手机号
 * @returns 是否合法
 */
export function validatePhone(phone: string): boolean {
    return /^1[3-9]\d{9}$/.test(phone)
}

/**
 * 邮箱验证
 * @param email 邮箱
 * @returns 是否合法
 */
export function validateEmail(email: string): boolean {
    return /^[^\s@]+@[^\s@]+\.[^\s@]+$/.test(email)
}

/**
 * 身份证号验证（简单版）
 * @param idCard 身份证号
 * @returns 是否合法
 */
export function validateIdCard(idCard: string): boolean {
    return /(^\d{15}$)|(^\d{18}$)|(^\d{17}[\dXx]$)/.test(idCard)
}

// ==================== 防抖/节流 ====================

/**
 * 防抖函数
 * @param fn 目标函数
 * @param delay 延迟毫秒数
 * @returns 防抖后的函数
 */
export function debounce<T extends (...args: any[]) => any>(fn: T, delay: number): T {
    let timer: number | null = null
    return ((...args: any[]) => {
        if (timer) clearTimeout(timer)
        timer = setTimeout(() => fn(...args), delay) as unknown as number
    }) as T
}

/**
 * 节流函数
 * @param fn 目标函数
 * @param delay 间隔毫秒数
 * @returns 节流后的函数
 */
export function throttle<T extends (...args: any[]) => any>(fn: T, delay: number): T {
    let lastCall = 0
    return ((...args: any[]) => {
        const now = Date.now()
        if (now - lastCall >= delay) {
            lastCall = now
            fn(...args)
        }
    }) as T
}

// ==================== 深拷贝 ====================

/**
 * 深拷贝
 * @param obj 目标对象
 * @returns 拷贝后的对象
 */
export function deepClone<T>(obj: T): T {
    return JSON.parse(JSON.stringify(obj))
}

// ==================== 生成唯一 ID ====================

/**
 * 生成简单的唯一 ID
 * @returns 唯一 ID 字符串
 */
export function generateId(): string {
    return Date.now().toString(36) + Math.random().toString(36).substr(2, 9)
}

// ==================== 团购相关 ====================

/**
 * 格式化剩余时间（倒计时）
 * @param expiresAt ISO 日期字符串
 * @returns 格式化后的倒计时，如 "02:30:15"、"1天 05:20"
 */
export function formatTimeRemaining(expiresAt: string | null | undefined): string {
    if (!expiresAt) return '--:--:--'
    const end = new Date(expiresAt)
    const now = new Date()
    if (isNaN(end.getTime())) return '--:--:--'

    const diffMs = end.getTime() - now.getTime()
    if (diffMs <= 0) return '已结束'

    const totalSec = Math.floor(diffMs / 1000)
    const days = Math.floor(totalSec / 86400)
    const hours = Math.floor((totalSec % 86400) / 3600)
    const minutes = Math.floor((totalSec % 3600) / 60)
    const seconds = totalSec % 60

    if (days > 0) {
        return `${days}天 ${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}`
    }
    return `${String(hours).padStart(2, '0')}:${String(minutes).padStart(2, '0')}:${String(seconds).padStart(2, '0')}`
}

/**
 * 昵称脱敏
 * @param nickname 原始昵称
 * @returns 脱敏后的昵称，如 "张**"、"李**"、"王*五"
 */
export function maskNickname(nickname: string | null | undefined): string {
    if (!nickname) return '用户'
    if (nickname.length <= 1) return nickname + '*'
    if (nickname.length === 2) return nickname[0] + '*'
    return nickname[0] + '*'.repeat(nickname.length - 2) + nickname[nickname.length - 1]
}

/**
 * 生成取货码二维码数据
 * @param orderId 订单 ID
 * @param pickupCode 取货码
 * @returns 二维码数据字符串（JSON）
 */
export function generateQrCode(orderId: string, pickupCode: string): string {
    return JSON.stringify({ order_id: orderId, pickup_code: pickupCode }, null, 2)
}
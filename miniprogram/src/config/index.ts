// 宋鲜生小程序配置
// 正式上线前请修改对应环境变量

// 环境标识：development | production
export const ENV = 'development' as const

// API 基础地址
export const API_BASE_URL = 'https://mall.sxueji.com'

// 微信小程序 AppID（测试号用 touristappid，正式上线替换为真实 AppID）
export const WECHAT_MP_APP_ID = 'touristappid'

// 请求超时时间（毫秒）
export const REQUEST_TIMEOUT = 15000

// Token 存储键名
export const TOKEN_KEY = 'songmall_access_token'
export const REFRESH_TOKEN_KEY = 'songmall_refresh_token'
export const USER_INFO_KEY = 'songmall_user_info'

// 品牌配置
export const BRAND = {
    name: '宋鲜生',
    nameEn: 'SongMall',
    primaryColor: '#2E7D32',
    secondaryColor: '#4CAF50',
    logoUrl: '/static/logo.png'
} as const

// 默认分页大小
export const DEFAULT_PAGE_SIZE = 20
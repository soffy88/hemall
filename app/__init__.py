"""hemall 后端 — 3O 范式项目服务层 (§8)。

把 platform/3O 的 oprim/oskill/omodul/obase/oservi 元素装配成一个可运行的
FastAPI headless commerce 引擎。本包是项目服务层: 负责 API 路由 / 鉴权 /
连接池装配 / 事件派发 / output_dir 拼装与去重, 业务逻辑全部委派给 omodul。
"""

__version__ = "0.1.0"

"""hemall 智能推荐模块 — 协同过滤 + 内容-based 混合推荐。

Phase 3 Priority 1: 为用户推荐个性化商品，提升转化率。

算法选择:
    1. User-Based Collaborative Filtering (基于用户的协同过滤)
    2. Item-Based Collaborative Filtering (基于物品的协同过滤)  
    3. Content-Based Filtering (基于内容的推荐 - 品类/品牌/标签匹配)
    4. Hybrid Recommender (加权融合上述算法)

数据处理:
    - 用户行为日志收集 (浏览/加购/购买/收藏)
    - 评分矩阵构建 (隐式反馈：点击权重 1, 加购权重 3, 购买权重 5)
    - 实时更新候选集 (Hot/Cold product 处理)

API:
    GET /recommend/home              # 首页个性化推荐
    GET /recommend/item/{id}/similar # 相似商品
    GET /recommend/user/{id}         # 用户画像驱动推荐
"""

from __future__ import annotations

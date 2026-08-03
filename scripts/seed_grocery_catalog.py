#!/usr/bin/env python3
"""seed_grocery_catalog.py — 对标小象超市的生鲜日用目录播种脚本 (Phase 9.5)

直接连生产库 (asyncpg) 插入 9 大分类 + 45 个真实生鲜日用商品：
  - 真实商品名 + 规格 (对标小象超市/盒马/永辉常见在售 SKU)
  - 真实市场价 (reference_price_cents 划线价 / retail_price_cents 售价)
  - 真实中文商品说明
  - 网上图片 (loremflickr 按关键词返回真实食物照片, 前端 emoji 兜底)

幂等: 按 slug 判重，已存在的分类/商品跳过；重复运行安全。

用法:
  cd /data/soffy/projects/hemal && uv run python scripts/seed_grocery_catalog.py
"""

from __future__ import annotations

import asyncio
import os
import sys

import asyncpg

PG_DSN = os.environ.get(
    "HEMALL_PG_DSN",
    "postgresql://hemall:xgz6YvoHVlJZFy9SZtigOK4ckoYy02BL@localhost:5432/hemall_prod?sslmode=disable",
)
LOCATION_ID = "00000000-0000-4000-8000-000000000002"  # 北京仓

# 商品图: loremflickr 按关键词给真实食物照片；关键词用英文 (服务端索引)
def img(keyword: str) -> str:
    return f"https://loremflickr.com/400/400/{keyword}?lock={abs(hash(keyword)) % 9999}"


# ── 分类: 对标小象超市 ───────────────────────────────────────────────
CATEGORIES = [
    ("蔬菜豆品", "vegetables", "🥬"),
    ("时令水果", "fruits", "🍎"),
    ("肉禽蛋", "meat-eggs", "🥩"),
    ("粮油调味", "grain-oil", "🌾"),
    ("乳品烘焙", "dairy-bakery", "🥛"),
    ("冷冻速食", "frozen", "🧊"),
    ("休闲零食", "snacks", "🍫"),
    ("酒水饮料", "drinks", "🥤"),
    ("日用百货", "daily", "🧻"),
]

# ── 商品目录: (分类slug, 商品名, 规格, 划线价分, 售价分, 库存, 图片关键词, 说明) ──
PRODUCTS = [
    # 蔬菜豆品
    ("vegetables", "西红柿", "500g/份", 699, 399, 40, "tomato", "沙瓤多汁，生吃凉拌两相宜，当日采摘冷链直供。"),
    ("vegetables", "黄瓜", "500g/份", 499, 299, 40, "cucumber,vegetable", "翠绿爽脆，水分足，拍黄瓜/凉拌首选。"),
    ("vegetables", "土豆", "1kg/袋", 599, 299, 60, "potato", "黄心土豆，粉糯沙瓤，炖煮炒炸皆宜。"),
    ("vegetables", "胡萝卜", "500g/份", 399, 199, 40, "carrot", "甜脆多汁，富含胡萝卜素，宝宝辅食好选择。"),
    ("vegetables", "西兰花", "300g/颗", 699, 499, 30, "broccoli", "新鲜翠绿，花球紧实，低卡高纤减脂必备。"),
    ("vegetables", "上海青", "400g/份", 399, 199, 40, "bok choy,vegetable", "叶嫩梗脆，清炒鲜甜，家常快手菜首选。"),
    # 时令水果
    ("fruits", "红富士苹果", "1kg/份", 1299, 899, 30, "apple,fruit", "脆甜多汁，果香浓郁，产地直采不打蜡。"),
    ("fruits", "海南香蕉", "1kg/串", 899, 599, 30, "banana", "自然熟成，软糯香甜，老人小孩都爱吃。"),
    ("fruits", "丹东草莓", "500g/盒", 2999, 1999, 15, "strawberry", "红颜品种，个大味甜，奶油香气，冷链锁鲜。"),
    ("fruits", "赣南脐橙", "1kg/份", 1399, 999, 25, "orange,fruit", "皮薄肉厚，化渣多汁，维C满满。"),
    ("fruits", "麒麟西瓜", "约2.5kg/个", 2599, 1899, 10, "watermelon", "皮薄瓤红，甜度高，冰镇更爽口。"),
    ("fruits", "巨峰葡萄", "500g/串", 1599, 1099, 20, "grape", "果粒饱满，汁多味甜，带天然果粉。"),
    # 肉禽蛋
    ("meat-eggs", "冷鲜鸡胸肉", "500g/份", 1599, 1099, 25, "chicken breast,meat", "低脂高蛋白，健身减脂首选，冷鲜锁鲜。"),
    ("meat-eggs", "猪五花肉", "500g/份", 1999, 1399, 25, "pork,bacon", "肥瘦相间，红烧回锅肉绝配，当日鲜切。"),
    ("meat-eggs", "牛腩块", "500g/份", 3599, 2599, 15, "beef,meat", "雪花纹理，炖煮软烂，番茄牛腩煲首选。"),
    ("meat-eggs", "鲜鸡蛋", "30枚/箱", 2199, 1699, 20, "egg,eggs", "谷物喂养，蛋黄橙红，蛋香浓郁。"),
    ("meat-eggs", "基围虾", "400g/份", 3599, 2599, 15, "shrimp", "鲜活现捕，肉质紧实弹牙，白灼最鲜。"),
    ("meat-eggs", "鲈鱼", "约500g/条", 2599, 1999, 12, "fish,seabass", "肉质细嫩少刺，清蒸鲜美，鲜活到家。"),
    # 粮油调味
    ("grain-oil", "东北大米", "5kg/袋", 3999, 2999, 25, "rice", "一年一季稻花香，粒粒分明，饭香浓郁。"),
    ("grain-oil", "花生油", "1.8L/桶", 5999, 4499, 20, "peanut oil,bottle", "物理压榨，浓香醇厚，炒菜更香。"),
    ("grain-oil", "鸡蛋挂面", "1kg/袋", 999, 599, 40, "noodle,egg", "筋道爽滑，久煮不糊，早餐快手。"),
    ("grain-oil", "生抽酱油", "500ml/瓶", 1299, 899, 30, "soy sauce", "零添加头道原汁，鲜味自然，红烧蘸料皆宜。"),
    ("grain-oil", "陈醋", "420ml/瓶", 899, 599, 30, "vinegar", "山西老陈醋，酸香醇厚，凉拌炒菜必备。"),
    ("grain-oil", "食用盐", "400g/袋", 299, 199, 50, "salt", "加碘精制盐，颗粒细腻，炒菜炖汤皆宜。"),
    # 乳品烘焙
    ("dairy-bakery", "鲜牛奶", "950ml/瓶", 1699, 1199, 30, "milk,glass", "当日鲜奶，巴氏杀菌，奶香醇厚。"),
    ("dairy-bakery", "原味酸奶", "100g×8杯", 1599, 1099, 30, "yogurt", "生牛乳发酵，0添加蔗糖，浓稠顺滑。"),
    ("dairy-bakery", "切片面包", "400g/袋", 999, 599, 40, "bread,loaf", "全麦吐司，柔软拉丝，早餐三明治好搭档。"),
    ("dairy-bakery", "黄油", "200g/块", 1999, 1499, 20, "butter", "动物黄油，奶香浓郁，烘焙煎烤皆宜。"),
    ("dairy-bakery", "马苏里拉芝士", "200g/袋", 2599, 1999, 15, "cheese,mozzarella", "拉丝绵长，披萨焗饭必备，冷冻锁鲜。"),
    ("dairy-bakery", "水煮蛋", "8枚/盒", 999, 699, 30, "egg,boiled", "即食溏心蛋，开袋即吃，营养早餐首选。"),
    # 冷冻速食
    ("frozen", "速冻水饺", "500g/袋", 1599, 1099, 25, "dumpling", "猪肉白菜馅，皮薄馅大，煮煎皆宜。"),
    ("frozen", "手抓饼", "10片/袋", 1299, 899, 25, "pancake,dough", "层层起酥，煎3分钟即食，早餐神器。"),
    ("frozen", "冷冻虾仁", "400g/袋", 2599, 1999, 15, "shrimp,frozen", "去壳去线，Q弹鲜甜，炒菜煮汤快手。"),
    ("frozen", "玉米粒", "1kg/袋", 1299, 899, 20, "corn,sweet", "甜糯玉米粒，沙拉炒饭好搭档。"),
    ("frozen", "鱼丸", "500g/袋", 1999, 1399, 20, "fish ball", "Q弹爽滑，火锅关东煮必备。"),
    ("frozen", "冰淇淋", "75g×6支", 1999, 1499, 15, "ice cream", "香草牛乳味，绵密顺滑，夏日解暑。"),
    # 休闲零食
    ("snacks", "薯片", "104g/袋", 899, 599, 40, "potato chips", "经典原味，薄脆香酥，追剧解馋。"),
    ("snacks", "每日坚果", "25g×7包", 1999, 1499, 25, "nuts,mixed", "七种坚果果干科学配比，每日一包。"),
    ("snacks", "牛奶巧克力", "100g/板", 999, 699, 30, "chocolate,bar", "可可脂含量高，丝滑浓郁，甜而不腻。"),
    ("snacks", "苏打饼干", "400g/盒", 899, 599, 35, "biscuit,soda", "咸香酥脆，低糖饱腹，办公室常备。"),
    ("snacks", "肉松饼", "500g/袋", 1399, 999, 25, "pastry,snack", "外酥里软，肉松满满，下午茶点。"),
    ("snacks", "辣条", "500g/袋", 999, 699, 40, "spicy strips,snack", "香辣过瘾，童年味道，休闲解馋。"),
    # 酒水饮料
    ("drinks", "可乐", "330ml×6罐", 1299, 999, 30, "cola", "冰镇气泡足，聚会餐桌必备。"),
    ("drinks", "矿泉水", "550ml×12瓶", 1299, 899, 30, "water,bottle", "天然弱碱性水，日常补水首选。"),
    ("drinks", "绿茶", "500ml×4瓶", 999, 699, 30, "green tea,bottle", "茶多酚丰富，解腻清爽，0糖更健康。"),
    ("drinks", "橙汁", "1L/瓶", 1399, 999, 20, "orange juice", "100%纯果汁，维C满满，早餐好伴侣。"),
    ("drinks", "啤酒", "500ml×6罐", 2599, 1999, 20, "beer,can", "麦香浓郁，泡沫细腻，冰镇更爽。"),
    ("drinks", "酸奶饮品", "250ml×4瓶", 999, 699, 30, "yogurt,drink", "乳酸菌发酵，酸甜开胃，餐后解腻。"),
    # 日用百货
    ("daily", "抽纸", "3层×100抽×6包", 1599, 1199, 25, "tissue,paper", "原生木浆，柔韧不易破，家庭囤货装。"),
    ("daily", "卷纸", "4层×27卷", 2599, 1999, 15, "toilet paper,roll", "加厚柔韧，易溶于水，冲厕无忧。"),
    ("daily", "洗衣液", "2kg/瓶", 1999, 1499, 20, "laundry detergent", "深层去渍，清新留香，机洗手洗皆宜。"),
    ("daily", "洗发水", "500ml/瓶", 2999, 2199, 15, "shampoo,bottle", "控油蓬松，氨基酸温和配方，无硅油。"),
    ("daily", "牙膏", "120g/支", 899, 599, 30, "toothpaste", "含氟防蛀，清新薄荷，全家适用。"),
    ("daily", "垃圾袋", "45×50cm×100只", 999, 699, 30, "trash bag,garbage", "加厚防漏，抽绳设计，厨房必备。"),
]


async def main() -> None:
    conn = await asyncpg.connect(PG_DSN)
    print(f"✅ 连接生产库成功: {PG_DSN.split('@')[-1]}")

    try:
        # 1. 建分类
        cat_ids: dict[str, str] = {}
        for name, slug, icon in CATEGORIES:
            row = await conn.fetchrow(
                'SELECT id FROM "product_category" WHERE slug = $1 AND deleted_at IS NULL',
                slug,
            )
            if row:
                cat_ids[slug] = str(row["id"])
                print(f"⏭ 分类已存在: {name}")
                continue
            cid = await conn.fetchval(
                'INSERT INTO "product_category" (id, name, slug) '
                "VALUES (gen_random_uuid(), $1, $2) RETURNING id",
                name,
                slug,
            )
            cat_ids[slug] = str(cid)
            print(f"✅ 创建分类: {icon} {name}")

        # 2. 建商品 + 变体 + 批次
        created = 0
        for cat_slug, title, spec, ref_price, retail, stock, img_kw, desc in PRODUCTS:
            slug = title
            row = await conn.fetchrow(
                'SELECT id FROM "product" WHERE slug = $1 AND deleted_at IS NULL', slug
            )
            if row:
                print(f"⏭ 商品已存在: {title}")
                continue

            cat_id = cat_ids[cat_slug]
            # product
            pid = await conn.fetchval(
                'INSERT INTO "product" (id, title, slug, description, category_id, status) '
                "VALUES (gen_random_uuid(), $1, $2, $3, $4, 'published') RETURNING id",
                title,
                slug,
                f"{desc}（{spec}）",
                cat_id,
            )
            # variant
            sku = f"GRC-{abs(hash(slug)) % 90000 + 10000}"
            vid = await conn.fetchval(
                'INSERT INTO "product_variant" (id, product_id, sku_code, '
                "option_values, reference_price_cents, status) "
                "VALUES (gen_random_uuid(), $1, $2, $3::jsonb, $4, 'active') RETURNING id",
                pid,
                sku,
                '{"规格": "' + spec + '"}',
                ref_price,
            )
            # inventory batch (挂在最近 active location)
            await conn.fetchval(
                'INSERT INTO "inventory_batch" '
                "(id, variant_id, location_id, batch_no, video_url, cost_price_cents, "
                "retail_price_cents, stock_qty, currency, media_assets, shelf_image_url, status) "
                "VALUES (gen_random_uuid(), $1, $2, $3, '', $4, $5, $6, 'CNY', "
                "$7::jsonb, $8, 'active') RETURNING id",
                vid,
                LOCATION_ID,
                f"SEED-{abs(hash(slug)) % 99999}",
                int(ref_price * 0.6),
                retail,
                stock,
                f'["{img(img_kw)}"]',
                img(img_kw),
            )
            created += 1
            print(f"✅ 上架: {title} ¥{retail/100:.2f} (划线 ¥{ref_price/100:.2f}) 库存{stock}")

        print(f"\n🎉 播种完成: 分类 {len(CATEGORIES)} 个, 新建商品 {created} 个")
    finally:
        await conn.close()


if __name__ == "__main__":
    asyncio.run(main())

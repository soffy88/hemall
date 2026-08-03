"""app.ext.schema — hemall 商城扩展层 DDL Schema (六轮 SPEC)。

**统一改造说明**：v1.0~v6.0 每一轮都把扩展特性当成一个独立新系统来建，自己起了
一整套跟 hemall 原有商城 (platform/3O/obase.commerce_batch_schema 共享库定义
的 product/product_variant/inventory_batch/cart/customer_order/claim 等 29
张表) 平行的表 (products/variants/inventory_batches/orders/...)。用户明确
指出扩展域就是 hemall 原生内建能力，不该有两套——本模块已经从"平行建表"迁移成"给共享表打
hemall 本地补丁 + 只为真正新增的业务概念 (供应商托管/循环筐/C2B集单/计件
工资/宿主分润/会员订阅/价格基线/试探单/社交播报/抖音分润...) 新建本地表"，
`_TABLES`/`_INDEXES` 全部 FK 指向共享表，不再有自己的平行副本。旧平行表已在
Phase 4.2 整体 DROP TABLE (迁移期间保留过一段时间方便回滚，全部 omodul/oservi
改接完成、全量测试通过后确认不再需要，物理删除，未做数据迁移——旧表里全是
共享测试库上积累的可丢弃 demo/smoke-test 数据)。

新表主键统一用 `obase.uuid7.uuid7()` (跟共享表同一个 ID 生成器，标准 UUID
格式)，不再用本模块自己的 `generate_id_v7()` 紧凑格式 (那种格式塞不进 UUID
类型列；`generate_id_v7()` 现在只剩 `labor_ledger` 这一张原样复用的旧表还在用，
它的 id/worker_id/order_line_item_id 从建表起就不是任何退休表的 FK，物理上
直接沿用，不用跟着搬家)。

复用 obase.persistence.ddl 的幂等 ensure_table/ensure_index (CREATE ... IF
NOT EXISTS 语义)，启动期反复调用安全；不改动 obase.commerce_batch_schema 本身
的 DDL 文件 (那是 platform/3O 下跨项目共享的库，hicode/mneme/tide 等其他项目
也在用，不能塞 hemall 专属业务字段)——给共享表追加列走跟
`app/bootstrap.py::_ensure_hemall_local_schema` (customer.password_hash 那个
本地补丁) 完全一样的套路：`ensure_column` 直接 ALTER 共享表，只是这段幂等
DDL 代码本身放在 hemall 项目自己这一层。
"""

from __future__ import annotations

from typing import Any

from obase.persistence.ddl import ensure_column, ensure_index, ensure_table
from obase.persistence.pool import PgPool

_SCHEMA = "public"

# ── 统一后的本地表 (Phase 0+1)：FK 全部指向共享表 ──────────────────────────
# v1.0~v6.0 期间建的旧平行表 (products/variants/inventory_batches/orders/...)
# 已于统一改造 Phase 4.2 整体 DROP TABLE——迁移期间保留过一段时间方便回滚，
# 全部 omodul/oservi 改接完成、全量测试通过后确认不再需要，物理删除。
# supplier 必须建在最前面 (下面 _ensure_shared_table_local_columns 里
# inventory_batch.supplier_id 这个新增列的 REFERENCES 需要它已存在)。
_TABLES: list[tuple[str, list[tuple[str, str]]]] = [
    # 供应商托管 (原 v2.0 suppliers 表的统一版本；FK 改指向共享 inventory_batch
    # 靠 inventory_batch.supplier_id 这一列，不是这张表自己拿 FK 指过去)
    (
        "supplier",
        [
            ("id", "UUID PRIMARY KEY"),
            ("wallet_account", "TEXT NOT NULL"),
            ("spatial_polygon", "JSONB NOT NULL"),
            ("polygon_name", "VARCHAR(128)"),
            ("trust_score", "INT DEFAULT 100"),
            ("escrow_balance", "INT DEFAULT 0"),
            ("status", "VARCHAR(20) DEFAULT 'sandbox'"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # 循环载具 (原 totes)
    (
        "tote",
        [
            ("id", "UUID PRIMARY KEY"),
            ("current_location_id", "UUID REFERENCES stock_location(id)"),
            ("status", "VARCHAR(20) DEFAULT 'idle'"),
        ],
    ),
    # 循环筐押金流水 (原 tote_deposits)
    (
        "tote_deposit",
        [
            ("id", "UUID PRIMARY KEY"),
            ("tote_id", "UUID REFERENCES tote(id)"),
            ("intent_id", "VARCHAR(128) NOT NULL"),
            ("amount_cents", "INT NOT NULL"),
            ("status", "VARCHAR(20) DEFAULT 'charged'"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # C2B 逆向集单 (原 crowd_intents；customer_ref 自由字符串改成真
    # customer.id FK——统一之前扩展域压根没有真实顾客账户可绑)
    (
        "crowd_intent",
        [
            ("id", "UUID PRIMARY KEY"),
            ("variant_id", "UUID REFERENCES product_variant(id)"),
            ("customer_id", "UUID REFERENCES customer(id)"),
            ("prepaid_amount_cents", "INT NOT NULL"),
            ("status", "VARCHAR(20) DEFAULT 'pending'"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # 宿主场地分润流水 (原 host_ledger；改名避免跟下面还在用的旧 host_ledger
    # 撞表名——旧表的 location_id 指向旧 stock_locations，不能直接复用)
    (
        "host_dividend_ledger",
        [
            ("id", "UUID PRIMARY KEY"),
            ("host_id", "VARCHAR(32) NOT NULL"),
            ("location_id", "UUID REFERENCES stock_location(id)"),
            ("tote_count", "INT NOT NULL"),
            ("amount_cents", "INT NOT NULL"),
            ("payout_transfer_id", "VARCHAR(128)"),
            ("period_date", "DATE"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # 会员订阅 (原 memberships；customer_ref 改真 FK)
    (
        "membership",
        [
            ("id", "UUID PRIMARY KEY"),
            ("customer_id", "UUID REFERENCES customer(id)"),
            ("status", "VARCHAR(20) DEFAULT 'active'"),
            ("intent_id", "VARCHAR(128)"),
            ("expires_at", "TIMESTAMPTZ NOT NULL"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # 微仓损耗账本 (原 location_loss_ledger；改名理由同 host_dividend_ledger)
    (
        "stock_location_loss_ledger",
        [
            ("id", "UUID PRIMARY KEY"),
            ("location_id", "UUID REFERENCES stock_location(id)"),
            ("batch_id", "UUID REFERENCES inventory_batch(id)"),
            ("amount_cents", "INT NOT NULL"),
            ("reason", "VARCHAR(32) NOT NULL"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # 微信视频号自动播报轨迹 (原 channel_broadcast_logs)
    (
        "channel_broadcast_log",
        [
            ("id", "UUID PRIMARY KEY"),
            ("batch_id", "UUID REFERENCES inventory_batch(id)"),
            ("broadcast_type", "VARCHAR(20) NOT NULL"),
            ("llm_copywriting", "TEXT NOT NULL"),
            ("wechat_feed_id", "VARCHAR(128)"),
            ("mini_program_path", "VARCHAR(255) NOT NULL"),
            ("status", "VARCHAR(20) DEFAULT 'pending'"),
            ("error_message", "TEXT"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
            ("published_at", "TIMESTAMPTZ"),
        ],
    ),
    # 全局竞争对手价格基线库 (原 price_benchmarks；sku_id 改真
    # product_variant.id FK，不再靠字符串手动对齐)
    (
        "price_benchmark",
        [
            ("id", "UUID PRIMARY KEY"),
            # variant_id 可空——统一之前用一个"匹配不上就退化成商品名 slug"
            # 的字符串顶替，现在 variant_id 是真 UUID FK，装不下 slug；匹配
            # 不上就老实存 NULL，raw_item_name 保留原始品名方便人工核对，
            # 不假装有一个不存在的商品关联。
            ("variant_id", "UUID REFERENCES product_variant(id)"),
            ("raw_item_name", "VARCHAR(128)"),
            ("source_type", "VARCHAR(20) NOT NULL"),
            ("competitor_name", "VARCHAR(64)"),
            ("raw_price_cents", "INT NOT NULL"),
            ("raw_unit", "VARCHAR(20) NOT NULL"),
            ("normalized_price_per_unit", "DECIMAL(10,4)"),
            ("captured_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # 试探单高频做市日志 (原 probe_order_logs)
    (
        "probe_order_log",
        [
            ("id", "UUID PRIMARY KEY"),
            ("batch_id", "UUID REFERENCES inventory_batch(id)"),
            ("probe_price_cents", "INT NOT NULL"),
            ("traffic_exposure", "INT DEFAULT 0"),
            ("observed_sales_velocity", "DECIMAL(10,4)"),
            ("status", "VARCHAR(20) DEFAULT 'testing'"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
            ("resolved_at", "TIMESTAMPTZ"),
        ],
    ),
    # 抖音达人智能分润契约 (原 affiliate_contracts；bound_entity_id 保持多态
    # (batch_id 或 location_id)，两者现在都指向共享表的 UUID)
    (
        "affiliate_contract",
        [
            ("id", "UUID PRIMARY KEY"),
            ("douyin_uid", "VARCHAR(64) NOT NULL"),
            ("contract_type", "VARCHAR(20) NOT NULL"),
            ("bound_entity_id", "UUID NOT NULL"),
            ("commission_logic", "JSONB NOT NULL"),
            ("status", "VARCHAR(20) DEFAULT 'active'"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # 抖音引流转化流水 (原 douyin_conversion_logs)
    (
        "douyin_conversion_log",
        [
            ("id", "UUID PRIMARY KEY"),
            ("order_id", "UUID REFERENCES customer_order(id)"),
            ("douyin_uid", "VARCHAR(64) NOT NULL"),
            ("contract_id", "UUID REFERENCES affiliate_contract(id)"),
            ("dividend_amount_cents", "INT NOT NULL"),
            ("settlement_status", "VARCHAR(20) DEFAULT 'pending'"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # 大妈计件工资流水 (原 labor_ledger)——这张表本来就没有指向旧平行表的 FK
    # (worker_id/order_line_item_id 从建表起就是自由字符串，不是外键)，物理
    # 上直接复用同一张表，不需要改名/搬家。
    (
        "labor_ledger",
        [
            # 沿用已有物理表的列类型/列名 (v1.0 就建了，CREATE TABLE IF NOT
            # EXISTS 对已存在的表不会重建/重命名列——id 物理上是
            # VARCHAR(32) (旧 generate_id_v7 紧凑格式)，不是本轮新表统一用的
            # UUID；写入这张表继续用 generate_id_v7，不能塞 uuid7() 的 36
            # 字符标准格式 (装不下)。wage_amount 同理不带 _cents 后缀。
            ("id", "VARCHAR(32) PRIMARY KEY"),
            ("worker_id", "VARCHAR(32) NOT NULL"),
            # VARCHAR(64) 不是 VARCHAR(32)——order_line_item.id 统一后是共享
            # 表的 36 字符标准 UUID，32 装不下 (见
            # _ensure_labor_ledger_widened_column 给已有物理表做的同款加宽)。
            ("order_line_item_id", "VARCHAR(64)"),
            ("wage_amount", "INT NOT NULL"),
            ("status", "VARCHAR(20) DEFAULT 'pending'"),
            ("payout_transfer_id", "VARCHAR(128)"),
            ("created_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
    # Phase 7 Task 3: 零登录设备购买轨迹 (空间-行为矩阵的"行为序列"数据源)。
    # nearby-feed 是零登录公开端点 (限流按 IP+X-Device-Id)，顾客没有账号可用；
    # checkout 时把 X-Device-Id 头 + 成交行项写入此表，feed 就能"知道用户买过
    # 什么"，用 oskill 的关联度算子把关联商品插队到视野最前方。
    (
        "device_purchase_log",
        [
            ("id", "UUID PRIMARY KEY"),
            ("device_id", "VARCHAR(64) NOT NULL"),
            ("order_id", "UUID REFERENCES customer_order(id)"),
            ("batch_id", "UUID REFERENCES inventory_batch(id)"),
            ("product_id", "UUID REFERENCES product(id)"),
            ("variant_id", "UUID REFERENCES product_variant(id)"),
            ("purchased_at", "TIMESTAMPTZ DEFAULT NOW()"),
        ],
    ),
]

_INDEXES: list[tuple[str, str, str]] = [
    ("tote_deposit", "idx_tote_deposit_tote", "tote_id, status"),
    ("device_purchase_log", "idx_device_purchase_device", "device_id, purchased_at"),
    ("crowd_intent", "idx_crowd_intent_variant", "variant_id, status"),
    ("host_dividend_ledger", "idx_host_dividend_ledger_host", "host_id, period_date"),
    ("membership", "idx_membership_customer", "customer_id"),
    (
        "stock_location_loss_ledger",
        "idx_stock_location_loss_ledger_location",
        "location_id",
    ),
    ("price_benchmark", "idx_price_benchmark_variant", "variant_id, source_type"),
    ("probe_order_log", "idx_probe_order_log_batch", "batch_id, status"),
    (
        "affiliate_contract",
        "idx_affiliate_contract_bound",
        "bound_entity_id, contract_type, status",
    ),
    (
        "douyin_conversion_log",
        "idx_douyin_conversion_log_uid",
        "douyin_uid, settlement_status",
    ),
    ("labor_ledger", "idx_labor_ledger_worker", "worker_id, status"),
]


async def _ensure_shared_table_local_columns(pool: PgPool) -> None:
    """给共享表 (platform/3O/obase 定义) 打 hemall 本地补丁列，不改共享包本身。

    跟 app/bootstrap.py::_ensure_hemall_local_schema 的 customer.password_hash
    补丁同一个套路：`ensure_column` 直接 ALTER 共享物理表，共享包的 DDL 源码
    一个字都不动，不影响 hicode/mneme/tide 等其他消费者。
    """
    for column_name, column_def in (
        ("expiration_time", "TIMESTAMPTZ"),
        ("supplier_id", "UUID REFERENCES supplier(id)"),
        ("is_ghost_batch", "BOOLEAN DEFAULT FALSE"),
        ("vwap_target_curve", "JSONB"),
        ("delivery_type", "VARCHAR(20) DEFAULT 'spot'"),
        ("shelf_image_url", "VARCHAR(255)"),
        ("shelf_slot", "VARCHAR(32)"),
    ):
        await ensure_column(
            pool=pool,
            schema=_SCHEMA,
            table="inventory_batch",
            column_name=column_name,
            column_def=column_def,
        )

    for column_name, column_def in (
        ("host_id", "VARCHAR(32)"),
        ("address", "TEXT"),
        ("douyin_host_uid", "VARCHAR(64)"),
    ):
        await ensure_column(
            pool=pool,
            schema=_SCHEMA,
            table="stock_location",
            column_name=column_name,
            column_def=column_def,
        )

    # dispatch_status/dispatched_at: oservi.delivery_wave_engine 需要知道
    # "这单还没被打包过"，否则每个 cron tick 都会把同一批 wave 订单重复打包。
    # 共享 customer_order 没有这两列 (SPEC 未给)，hemall 本地补丁。
    for column_name, column_def in (
        ("dispatch_status", "VARCHAR(20) DEFAULT 'pending'"),
        ("dispatched_at", "TIMESTAMPTZ"),
    ):
        await ensure_column(
            pool=pool,
            schema=_SCHEMA,
            table="customer_order",
            column_name=column_name,
            column_def=column_def,
        )

    # promised_delivery_at / sla_compensated_at: 履约 SLA (P1 冲刺)。
    # promised_delivery_at 由 sla_promise_engine 按配送方式回填，订单流透出；
    # sla_compensated_at 是幂等标记 (一单一赔，重复 tick 不重复赔付)。
    for column_name, column_def in (
        ("promised_delivery_at", "TIMESTAMPTZ"),
        ("sla_compensated_at", "TIMESTAMPTZ"),
    ):
        await ensure_column(
            pool=pool,
            schema=_SCHEMA,
            table="customer_order",
            column_name=column_name,
            column_def=column_def,
        )

    for column_name, column_def in (
        ("base_trust_score", "INT DEFAULT 80"),
        ("total_saved_amount", "INT DEFAULT 0"),
        ("invited_by", "UUID REFERENCES customer(id)"),
        ("active_referrals", "INT DEFAULT 0"),
        ("membership_fee_discount", "INT DEFAULT 0"),
        ("system_balance", "INT DEFAULT 0"),
    ):
        await ensure_column(
            pool=pool,
            schema=_SCHEMA,
            table="customer",
            column_name=column_name,
            column_def=column_def,
        )

    for column_name, column_def in (
        ("vlm_damage_type", "VARCHAR(32)"),
        ("vlm_severity", "DECIMAL(3,2)"),
        ("evidence_image_url", "VARCHAR(255)"),
        ("liable_party", "VARCHAR(32)"),
        # decision: 全自动仲裁的裁决结果 ("instant_refund"/"drop_to_bin"/
        # "rejected")——跟共享 claim.status (pending/canceled/fulfilled，claim
        # 生命周期状态) 是两个维度，不能塞进同一列，单独开一列。
        ("decision", "VARCHAR(32)"),
        # 报案顾客——理论上应该等于 order 的 customer_id，但报案人跟下单人
        # 不总是同一个人 (代购/家庭账号共用)，单独存一列不隐式假设两者相等。
        ("user_id", "UUID REFERENCES customer(id)"),
        # 争议指向哪个批次——共享 claim.items 是 JSONB 快照 (顺应共享商城的
        # 一般多行 claim 场景)，但 execute_liability_routing_workflow 需要按
        # 单一 batch_id 精确查供应商/记损耗账本，单独存一列比每次解析 JSONB
        # 简单可靠。
        ("batch_id", "UUID REFERENCES inventory_batch(id)"),
    ):
        await ensure_column(
            pool=pool,
            schema=_SCHEMA,
            table="claim",
            column_name=column_name,
            column_def=column_def,
        )


async def _ensure_price_benchmark_raw_item_name_column(pool: PgPool) -> None:
    """price_benchmark 这轮统一改造期间已经建过 (没有 raw_item_name 列)，
    ensure_table 的 CREATE TABLE IF NOT EXISTS 对已存在的表不会补列——用
    ensure_column 幂等补上。"""
    await ensure_column(
        pool=pool,
        schema=_SCHEMA,
        table="price_benchmark",
        column_name="raw_item_name",
        column_def="VARCHAR(128)",
    )


async def _ensure_labor_ledger_widened_column(pool: PgPool) -> None:
    """labor_ledger.order_line_item_id 物理列是旧的 VARCHAR(32)——统一之后
    order_line_item.id 是共享表的 36 字符标准 UUID 字符串，塞不进 32 字符，
    这里加宽成 VARCHAR(64) (留余量，不是刚好 36)。ensure_column 只会
    ADD COLUMN IF NOT EXISTS，改不了已存在列的类型，这条 ALTER COLUMN TYPE
    单独写，用 information_schema 查询做"已经是目标类型就跳过"的幂等判断。
    """
    async with pool.acquire() as conn:
        current_length = await conn.fetchval(
            "SELECT character_maximum_length FROM information_schema.columns "
            "WHERE table_schema = $1 AND table_name = 'labor_ledger' "
            "AND column_name = 'order_line_item_id'",
            _SCHEMA,
        )
        if current_length is not None and current_length < 64:
            await conn.execute(
                'ALTER TABLE "public"."labor_ledger" '
                'ALTER COLUMN "order_line_item_id" TYPE VARCHAR(64)'
            )


async def ensure_ext_schema(pool: PgPool) -> None:
    """幂等建齐 hemall 商城扩展层全部表 + 索引。启动期调用，可重复执行。

    统一改造已完成：只建新增本地表 (FK 指向共享表)，打共享表本地补丁列——
    旧平行表已经在 Phase 4.2 物理删除，不在这个函数的职责范围内 (DROP TABLE
    不是幂等的 CREATE ... IF NOT EXISTS 语义，本来就不该出现在每次启动都会
    跑的 bootstrap 路径里)。
    """
    # 新增本地表必须先于共享表补丁列 (inventory_batch.supplier_id 的
    # REFERENCES 需要 supplier 表已存在)。
    for table, columns in _TABLES:
        await ensure_table(pool=pool, schema=_SCHEMA, table=table, columns=columns)
    for table, index_name, columns in _INDEXES:
        await ensure_index(
            pool=pool,
            schema=_SCHEMA,
            table=table,
            index_name=index_name,
            columns=columns,
        )
    await _ensure_shared_table_local_columns(pool)
    await _ensure_labor_ledger_widened_column(pool)
    await _ensure_price_benchmark_raw_item_name_column(pool)
    await _ensure_channel_broadcast_unique_index(pool)
    await _ensure_affiliate_contract_unique_index(pool)


async def _ensure_affiliate_contract_unique_index(pool: PgPool) -> None:
    """CREATE UNIQUE INDEX IF NOT EXISTS——同一达人对同一批次/节点只应有一份
    生效契约。record_douyin_conversion_workflow 靠这个唯一约束做
    ``ON CONFLICT (douyin_uid, bound_entity_id, contract_type) DO UPDATE``，
    不是可有可无的辅助索引 (affiliate_contract.id 统一后是 UUID 主键，装不下
    旧版那种手拼的确定性字符串 ID，只能靠这个唯一约束保证幂等)。
    """
    async with pool.acquire() as conn:
        await conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS "idx_affiliate_contract_uid_bound_type" '
            'ON "public"."affiliate_contract" (douyin_uid, bound_entity_id, contract_type)'
        )


async def _ensure_channel_broadcast_unique_index(pool: PgPool) -> None:
    """CREATE UNIQUE INDEX IF NOT EXISTS——防止同一批次同一播报类型被重复轰炸。

    obase.persistence.ddl.ensure_index 不支持 UNIQUE (只有 btree/HNSW 这类
    非唯一索引场景)，这里单独写一条幂等 DDL；这个唯一索引不只是防重复，
    execute_channel_broadcast_workflow 直接靠它的唯一性约束冲突来做原子防重发
    (见该 omodul 的 docstring)，不是可有可无的辅助索引。
    """
    async with pool.acquire() as conn:
        await conn.execute(
            'CREATE UNIQUE INDEX IF NOT EXISTS "idx_channel_broadcast_log_batch_type" '
            'ON "public"."channel_broadcast_log" (batch_id, broadcast_type)'
        )

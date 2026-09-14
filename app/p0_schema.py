"""P0 数据库约束与迁移。

P0 的验收条件都依赖持久化约束，不能只靠 Python 进程内的状态或 Redis
锁。这个模块只做幂等 DDL：业务写入仍由 payments/inventory/orders 服务负责。
"""

from __future__ import annotations

from obase.persistence.pool import PgPool

P0_SCHEMA_DDL = """
-- ── payment authority / outbox / refund idempotency ────────────────
CREATE TABLE IF NOT EXISTS payment_intent (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id TEXT NOT NULL,
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    currency TEXT NOT NULL CHECK (currency <> ''),
    provider TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'provider_pending', 'paid', 'failed',
                          'partial_refund', 'refunded', 'cancelled')),
    provider_intent_id TEXT,
    provider_trade_no TEXT,
    refund_amount_cents BIGINT NOT NULL DEFAULT 0 CHECK (refund_amount_cents >= 0),
    metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (order_id, provider)
);

CREATE UNIQUE INDEX IF NOT EXISTS uq_payment_intent_provider_trade
    ON payment_intent(provider, provider_trade_no)
    WHERE provider_trade_no IS NOT NULL;
ALTER TABLE payment_intent
    ADD COLUMN IF NOT EXISTS refund_amount_cents BIGINT NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS idx_payment_intent_order ON payment_intent(order_id);
CREATE INDEX IF NOT EXISTS idx_payment_intent_status ON payment_intent(status, updated_at);

CREATE TABLE IF NOT EXISTS payment_refund (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_intent_id UUID NOT NULL REFERENCES payment_intent(id),
    idempotency_key TEXT NOT NULL,
    amount_cents BIGINT NOT NULL CHECK (amount_cents > 0),
    reason TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'succeeded', 'failed')),
    provider_refund_no TEXT,
    provider_result JSONB NOT NULL DEFAULT '{}'::jsonb,
    last_error TEXT,
    version BIGINT NOT NULL DEFAULT 0 CHECK (version >= 0),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (payment_intent_id, idempotency_key)
);
CREATE INDEX IF NOT EXISTS idx_payment_refund_status
    ON payment_refund(status, updated_at);

CREATE TABLE IF NOT EXISTS payment_outbox (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    payment_intent_id UUID NOT NULL REFERENCES payment_intent(id),
    refund_id UUID REFERENCES payment_refund(id),
    kind TEXT NOT NULL CHECK (kind IN ('prepay', 'refund', 'reconcile')),
    idempotency_key TEXT NOT NULL UNIQUE,
    payload JSONB NOT NULL DEFAULT '{}'::jsonb,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'completed', 'failed')),
    attempts INTEGER NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    locked_at TIMESTAMPTZ,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);
CREATE INDEX IF NOT EXISTS idx_payment_outbox_ready
    ON payment_outbox(status, next_attempt_at, created_at);

-- ── inventory reservation ledger ───────────────────────────────────
CREATE TABLE IF NOT EXISTS inventory_reservation (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    order_id TEXT NOT NULL,
    order_line_item_id TEXT,
    product_id TEXT NOT NULL,
    variant_id TEXT,
    location_id UUID NOT NULL REFERENCES stock_location(id),
    batch_id UUID NOT NULL REFERENCES inventory_batch(id),
    quantity INTEGER NOT NULL CHECK (quantity > 0),
    consumed_qty INTEGER NOT NULL DEFAULT 0 CHECK (consumed_qty >= 0),
    released_qty INTEGER NOT NULL DEFAULT 0 CHECK (released_qty >= 0),
    status TEXT NOT NULL DEFAULT 'reserved'
        CHECK (status IN ('reserved', 'consumed', 'released', 'partially_released')),
    idempotency_key TEXT NOT NULL UNIQUE,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CHECK (consumed_qty + released_qty <= quantity)
);
CREATE INDEX IF NOT EXISTS idx_inventory_reservation_order
    ON inventory_reservation(order_id, status);
CREATE INDEX IF NOT EXISTS idx_inventory_reservation_batch
    ON inventory_reservation(batch_id, status);

-- ── versioned order transitions / exact wiring ──────────────────────
ALTER TABLE customer_order
    ADD COLUMN IF NOT EXISTS version BIGINT NOT NULL DEFAULT 0;
ALTER TABLE customer_order
    ADD COLUMN IF NOT EXISTS payment_verified_at TIMESTAMPTZ;
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS location_id UUID REFERENCES stock_location(id);
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS reservation_id UUID REFERENCES inventory_reservation(id);
ALTER TABLE order_line_item
    ADD COLUMN IF NOT EXISTS fulfilled_qty INTEGER NOT NULL DEFAULT 0
        CHECK (fulfilled_qty >= 0 AND fulfilled_qty <= quantity);

ALTER TABLE stock_movement ADD COLUMN IF NOT EXISTS idempotency_key TEXT;
CREATE UNIQUE INDEX IF NOT EXISTS uq_stock_movement_idempotency
    ON stock_movement(idempotency_key)
    WHERE idempotency_key IS NOT NULL;

-- Existing checkout code inserts only batch_id. Populate the location from the
-- authoritative batch row; no caller is allowed to supply a different location.
CREATE OR REPLACE FUNCTION hemall_wire_order_line_item()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    IF NEW.location_id IS NULL THEN
        SELECT location_id INTO NEW.location_id
        FROM inventory_batch WHERE id = NEW.batch_id;
    END IF;
    IF NEW.reservation_id IS NULL THEN
        SELECT id INTO NEW.reservation_id
        FROM inventory_reservation
        WHERE order_id = NEW.order_id::text
          AND batch_id = NEW.batch_id
          AND status IN ('reserved', 'partially_released')
        ORDER BY created_at
        LIMIT 1;
    END IF;
    RETURN NEW;
END;
$$;
DROP TRIGGER IF EXISTS trg_hemall_wire_order_line_item ON order_line_item;
CREATE TRIGGER trg_hemall_wire_order_line_item
    BEFORE INSERT OR UPDATE OF batch_id, location_id, reservation_id
    ON order_line_item
    FOR EACH ROW EXECUTE FUNCTION hemall_wire_order_line_item();
"""


async def ensure_p0_schema(pool: PgPool) -> None:
    """幂等创建 P0 所需表、列、索引和触发器。"""

    async with pool.acquire() as conn:
        await conn.execute(P0_SCHEMA_DDL)

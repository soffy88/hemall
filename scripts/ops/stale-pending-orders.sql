-- 现网长期 pending/draft 订单人工处理清单（只生成，不在生产执行）。
--
-- 默认查询是只读的。确认清单后，优先调用
--   POST /orders/{order_id}/cancel
-- 让 OrderService 在同一事务内释放 inventory_reservation。只有在应用不可用、
-- 且已核对每行 reservation 的情况下，才由 DBA 审阅并启用文末事务。

\set timeout_minutes 30

SELECT
    o.id,
    o.status,
    o.created_at,
    o.grand_total_cents,
    o.currency,
    COUNT(r.id) AS reservation_rows,
    COALESCE(SUM(r.quantity - r.consumed_qty - r.released_qty), 0) AS qty_still_reserved
FROM customer_order o
LEFT JOIN inventory_reservation r
       ON r.order_id = o.id::text
      AND r.status IN ('reserved', 'partially_released')
WHERE o.status IN ('pending', 'draft')
  AND o.created_at < NOW() - (:timeout_minutes * INTERVAL '1 minute')
  AND NOT EXISTS (
      SELECT 1
      FROM payment_intent p
      WHERE p.order_id = o.id::text
        AND p.status IN ('paid', 'partial_refund', 'refunded')
  )
GROUP BY o.id, o.status, o.created_at, o.grand_total_cents, o.currency
ORDER BY o.created_at;

-- DBA 审阅后手工启用（会写生产库；应用在线时应优先走 cancel API）：
-- BEGIN;
-- UPDATE inventory_reservation
-- SET released_qty = quantity - consumed_qty,
--     status = 'released',
--     updated_at = NOW()
-- WHERE order_id IN (
--     SELECT o.id::text FROM customer_order o
--     WHERE o.status IN ('pending', 'draft')
--       AND o.created_at < NOW() - (:timeout_minutes * INTERVAL '1 minute')
--       AND NOT EXISTS (
--           SELECT 1 FROM payment_intent p
--           WHERE p.order_id = o.id::text
--             AND p.status IN ('paid', 'partial_refund', 'refunded')
--       )
-- )
-- AND status IN ('reserved', 'partially_released');
-- UPDATE customer_order o
-- SET status = 'cancelled', version = version + 1, updated_at = NOW()
-- WHERE o.status IN ('pending', 'draft')
--   AND o.created_at < NOW() - (:timeout_minutes * INTERVAL '1 minute')
--   AND NOT EXISTS (
--       SELECT 1 FROM payment_intent p
--       WHERE p.order_id = o.id::text
--         AND p.status IN ('paid', 'partial_refund', 'refunded')
--   );
-- COMMIT;

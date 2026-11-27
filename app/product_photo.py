"""店员手机拍照换商品图 / 拍照上新。

- 图片只接受 jpeg/png/webp/heic(heif)，单张 ≤ 10MB；
- 服务端按 EXIF 方向摆正后压缩到长边 1200px，另生成 360px 缩略图；
- 重新编码为 JPEG，不写入任何 EXIF（含 GPS）；
- 文件名用处理后内容的 sha256，存于
  <output_root>/product_photo/<yyyymm>/<sha>.jpg，经 /media/product_photo 访问；
- 换主图只把旧图标记 replaced，不删除文件，旧图可恢复；
- 商品卡读 inventory_batch.shelf_image_url，所以设主图时同步更新该商品
  （或指定变体）的封面批次，并把旧值记进 product_photo.previous_images。
"""

from __future__ import annotations

import hashlib
import io
import json
import logging
import re
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import JSONResponse
from PIL import Image, ImageOps, UnidentifiedImageError

from .deps import get_current_customer, require_staff_role

try:  # iPhone 默认 HEIC；pillow-heif 自带 libheif
    from pillow_heif import register_heif_opener

    register_heif_opener()
    _HEIF_OK = True
except Exception:  # noqa: BLE001 - 缺插件时明确报错，不影响 jpg/png/webp
    _HEIF_OK = False

logger = logging.getLogger(__name__)

PHOTO_ROUTE = "/media/product_photo"
PHOTO_DIRNAME = "product_photo"
MAX_UPLOAD_BYTES = 10 * 1024 * 1024
MAX_EDGE = 1200
THUMB_EDGE = 360
JPEG_QUALITY = 82
THUMB_QUALITY = 76
ALLOWED_FORMATS = frozenset({"JPEG", "PNG", "WEBP", "HEIF", "AVIF"})
_STAFF_ROLES = ("admin", "support", "staff")

router = APIRouter(tags=["product-photo"])

_REFERENCE_PREFIXES = ("/media/spider/", "/products/")
_PHOTO_PREFIX = PHOTO_ROUTE + "/"


@dataclass(frozen=True)
class ProcessedImage:
    main: bytes
    thumb: bytes
    sha256: str
    width: int
    height: int


def _clean_filename(name: str | None) -> str:
    base = (name or "").replace("\\", "/").rsplit("/", 1)[-1]
    base = re.sub(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+", "_", base).strip("._")
    return base[:120] or "photo"


def process_image(data: bytes) -> ProcessedImage:
    """校验 + 摆正 + 去 EXIF + 压缩 + 缩略图。失败抛 ValueError。"""
    if not data:
        raise ValueError("empty file")
    if len(data) > MAX_UPLOAD_BYTES:
        raise ValueError("file too large (max 10MB)")
    try:
        with Image.open(io.BytesIO(data)) as im:
            fmt = (im.format or "").upper()
            if fmt not in ALLOWED_FORMATS:
                raise ValueError(f"unsupported image type: {fmt or 'unknown'}")
            if fmt in ("HEIF", "HEIC") and not _HEIF_OK:
                raise ValueError("HEIC decoder unavailable")
            im.load()
            fixed = ImageOps.exif_transpose(im)
            if fixed.mode in ("RGBA", "LA", "P"):
                rgba = fixed.convert("RGBA")
                bg = Image.new("RGB", rgba.size, (255, 255, 255))
                bg.paste(rgba, mask=rgba.getchannel("A"))
                fixed = bg
            else:
                fixed = fixed.convert("RGB")
            fixed.thumbnail((MAX_EDGE, MAX_EDGE), Image.Resampling.LANCZOS)
            main_buf = io.BytesIO()
            fixed.save(main_buf, format="JPEG", quality=JPEG_QUALITY, optimize=True)
            width, height = fixed.size
            thumb = fixed.copy()
            thumb.thumbnail((THUMB_EDGE, THUMB_EDGE), Image.Resampling.LANCZOS)
            thumb_buf = io.BytesIO()
            thumb.save(thumb_buf, format="JPEG", quality=THUMB_QUALITY, optimize=True)
    except UnidentifiedImageError as exc:
        raise ValueError("not a valid image") from exc
    except ValueError:
        raise
    except Exception as exc:  # noqa: BLE001 - 损坏图片/解码异常统一 400
        raise ValueError(f"invalid image: {exc}") from exc

    main = main_buf.getvalue()
    return ProcessedImage(
        main=main,
        thumb=thumb_buf.getvalue(),
        sha256=hashlib.sha256(main).hexdigest(),
        width=width,
        height=height,
    )


def _photo_root(output_root: str | Path) -> Path:
    return Path(output_root) / PHOTO_DIRNAME


def store_image(output_root: str | Path, processed: ProcessedImage) -> tuple[str, str]:
    """落盘并返回 (url, thumb_url)。同名 hash 直接复用（内容寻址）。"""
    month = datetime.now(UTC).strftime("%Y%m")
    folder = _photo_root(output_root) / month
    folder.mkdir(parents=True, exist_ok=True)
    name = processed.sha256[:32]
    main_path = folder / f"{name}.jpg"
    thumb_path = folder / f"{name}_thumb.jpg"
    if not main_path.exists():
        main_path.write_bytes(processed.main)
    if not thumb_path.exists():
        thumb_path.write_bytes(processed.thumb)
    rel = f"{month}/{name}"
    return f"{PHOTO_ROUTE}/{rel}.jpg", f"{PHOTO_ROUTE}/{rel}_thumb.jpg"


def remove_stored_image(output_root: str | Path, url: str) -> None:
    """仅用于“文件刚写入但数据库事务失败”的补偿；正常换图不删旧文件。"""
    if not url.startswith(_PHOTO_PREFIX):
        return
    root = _photo_root(output_root).resolve()
    candidate = (root / url[len(_PHOTO_PREFIX) :]).resolve()
    if candidate.is_relative_to(root) and candidate.is_file():
        try:
            candidate.unlink()
        except OSError:
            logger.warning("cleanup photo file failed: %s", candidate)


def _row_to_dict(row: Any) -> dict[str, Any]:
    d = dict(row)
    for key in ("id", "product_id", "variant_id", "replaced_by"):
        if d.get(key) is not None:
            d[key] = str(d[key])
    for key in ("created_at", "updated_at", "deleted_at"):
        value = d.get(key)
        if value is not None and hasattr(value, "isoformat"):
            d[key] = value.isoformat()
    prev = d.get("previous_images")
    if isinstance(prev, str):
        try:
            d["previous_images"] = json.loads(prev)
        except json.JSONDecodeError:
            d["previous_images"] = {}
    return d


async def _cover_batches(conn: Any, *, product_id: str, variant_id: str | None) -> list[Any]:
    """取要更新的封面批次：每个在售变体按“可售量最大、最早创建”取一个。"""
    if variant_id:
        rows = await conn.fetch(
            """
            SELECT DISTINCT ON (ib.variant_id) ib.id, ib.shelf_image_url, ib.variant_id
            FROM inventory_batch ib
            WHERE ib.variant_id = $1 AND ib.deleted_at IS NULL AND ib.status = 'active'
            ORDER BY ib.variant_id,
                     GREATEST(ib.stock_qty - ib.reserved_qty, 0) DESC,
                     ib.created_at ASC
            """,
            variant_id,
        )
        return rows
    return await conn.fetch(
        """
        SELECT DISTINCT ON (ib.variant_id) ib.id, ib.shelf_image_url, ib.variant_id
        FROM product_variant pv
        JOIN inventory_batch ib ON ib.variant_id = pv.id
        WHERE pv.product_id = $1 AND pv.deleted_at IS NULL AND pv.status = 'active'
          AND ib.deleted_at IS NULL AND ib.status = 'active'
        ORDER BY ib.variant_id,
                 GREATEST(ib.stock_qty - ib.reserved_qty, 0) DESC,
                 ib.created_at ASC
        """,
        product_id,
    )


async def _apply_photo_to_batches(
    conn: Any, *, product_id: str, variant_id: str | None, url: str
) -> dict[str, str | None]:
    """把新图写到封面批次的 shelf_image_url/media_assets，返回旧值映射。"""
    previous: dict[str, str | None] = {}
    for row in await _cover_batches(conn, product_id=product_id, variant_id=variant_id):
        previous[str(row["id"])] = row["shelf_image_url"]
        await conn.execute(
            "UPDATE inventory_batch SET shelf_image_url = $2, media_assets = $3::jsonb, "
            "updated_at = NOW() WHERE id = $1",
            row["id"],
            url,
            json.dumps([url], ensure_ascii=False),
        )
    await conn.execute("UPDATE product SET updated_at = NOW() WHERE id = $1", product_id)
    return previous


async def _next_primary(conn: Any, product_id: str) -> Any:
    return await conn.fetchrow(
        "SELECT id, url, variant_id FROM product_photo "
        "WHERE product_id = $1 AND status = 'active' AND deleted_at IS NULL "
        "ORDER BY position, created_at LIMIT 1",
        product_id,
    )


async def _set_primary(conn: Any, *, product_id: str, photo_id: str) -> Any:
    photo = await conn.fetchrow(
        "SELECT id, url, variant_id FROM product_photo "
        "WHERE id = $1 AND product_id = $2 AND status <> 'deleted' AND deleted_at IS NULL",
        photo_id,
        product_id,
    )
    if photo is None:
        raise HTTPException(404, "photo not found")
    await _demote_primary(conn, product_id, photo_id)
    previous = await _apply_photo_to_batches(
        conn,
        product_id=product_id,
        variant_id=str(photo["variant_id"] or "") or None,
        url=str(photo["url"]),
    )
    # 已有记录优先: 同一张图第二次当主图时, 仍恢复到它第一次覆盖前的原图,
    # 避免 A→B→A 之后删 A 把封面还原成已删的 B。
    await conn.execute(
        "UPDATE product_photo SET is_primary = TRUE, status = 'active', "
        "previous_images = $3::jsonb || previous_images, updated_at = NOW() "
        "WHERE id = $1 AND product_id = $2",
        photo_id,
        product_id,
        json.dumps(previous, ensure_ascii=False),
    )
    return await conn.fetchrow("SELECT * FROM product_photo WHERE id = $1", photo_id)


async def _demote_primary(conn: Any, product_id: str, keep_id: str | None = None) -> None:
    # 只摘主图标记, 不改 status: 相册里的图仍可再次设为主图/被删除。
    await conn.execute(
        "UPDATE product_photo SET is_primary = FALSE, updated_at = NOW() "
        "WHERE product_id = $1 AND is_primary AND status = 'active' "
        "AND ($2::uuid IS NULL OR id <> $2::uuid)",
        product_id,
        keep_id,
    )


async def attach_image_record(
    pool: Any,
    *,
    product_id: str,
    variant_id: str | None,
    url: str,
    thumb_url: str | None,
    sha256: str,
    width: int,
    height: int,
    size_bytes: int,
    filename: str,
    actor: str,
    make_primary: bool = True,
    previous: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """把一张已落盘的图片挂到商品相册；make_primary 时同步前台封面。

    previous 为空时由 _apply_photo_to_batches 自行记录被覆盖的旧图；
    传货链路已改过批次时把旧值传进来即可。
    """
    async with pool.acquire() as conn:
        product = await conn.fetchrow(
            "SELECT id FROM product WHERE id = $1 AND deleted_at IS NULL", product_id
        )
        if product is None:
            raise HTTPException(404, "product not found")
        if variant_id:
            belongs = await conn.fetchval(
                "SELECT 1 FROM product_variant WHERE id = $1 AND product_id = $2 "
                "AND deleted_at IS NULL",
                variant_id,
                product_id,
            )
            if not belongs:
                raise HTTPException(400, "variant does not belong to product")
        async with conn.transaction():
            position = await conn.fetchval(
                "SELECT COALESCE(MAX(position), 0) + 1 FROM product_photo WHERE product_id = $1",
                product_id,
            )
            photo_id = await conn.fetchval(
                "INSERT INTO product_photo "
                "(product_id, variant_id, url, thumb_url, sha256, width, height, bytes, "
                "original_filename, position, created_by) "
                "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11) RETURNING id",
                product_id,
                variant_id,
                url,
                thumb_url,
                sha256,
                width,
                height,
                size_bytes,
                _clean_filename(filename),
                position,
                actor,
            )
            if make_primary:
                await _demote_primary(conn, product_id, str(photo_id))
                if previous is None:
                    previous = await _apply_photo_to_batches(
                        conn,
                        product_id=product_id,
                        variant_id=variant_id,
                        url=url,
                    )
                await conn.execute(
                    "UPDATE product_photo SET is_primary = TRUE, status = 'active', "
                    "previous_images = $3::jsonb, updated_at = NOW() "
                    "WHERE id = $1 AND product_id = $2",
                    photo_id,
                    product_id,
                    json.dumps(previous, ensure_ascii=False),
                )
            row = await conn.fetchrow("SELECT * FROM product_photo WHERE id = $1", photo_id)
    return _row_to_dict(row)


async def add_photo(
    pool: Any,
    *,
    output_root: str | Path,
    product_id: str,
    variant_id: str | None,
    file: UploadFile,
    make_primary: bool,
    actor: str,
) -> dict[str, Any]:
    """保存图片并入库；make_primary 时同步前台商品卡封面。"""
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large (max 10MB)")
    try:
        processed = process_image(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    url, thumb_url = store_image(output_root, processed)
    try:
        return await attach_image_record(
            pool,
            product_id=product_id,
            variant_id=variant_id,
            url=url,
            thumb_url=thumb_url,
            sha256=processed.sha256,
            width=processed.width,
            height=processed.height,
            size_bytes=len(processed.main),
            filename=file.filename or "photo",
            actor=actor,
            make_primary=make_primary,
        )
    except Exception:
        remove_stored_image(output_root, url)
        raise


async def list_photos(pool: Any, product_id: str) -> list[dict[str, Any]]:
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            "SELECT * FROM product_photo WHERE product_id = $1 "
            "ORDER BY is_primary DESC, (deleted_at IS NOT NULL), position, created_at DESC",
            product_id,
        )
    return [_row_to_dict(r) for r in rows]


async def set_photo_primary(pool: Any, *, product_id: str, photo_id: str) -> dict[str, Any]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            row = await _set_primary(conn, product_id=product_id, photo_id=photo_id)
    return _row_to_dict(row)


async def restore_photo(pool: Any, *, product_id: str, photo_id: str) -> dict[str, Any]:
    """把被替换/删除的旧图恢复为相册成员；若没有主图则同时设为主图。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            photo = await conn.fetchrow(
                "SELECT * FROM product_photo WHERE id = $1 AND product_id = $2",
                photo_id,
                product_id,
            )
            if photo is None:
                raise HTTPException(404, "photo not found")
            await conn.execute(
                "UPDATE product_photo SET status = 'active', deleted_at = NULL, "
                "updated_at = NOW() WHERE id = $1",
                photo_id,
            )
            has_primary = await conn.fetchval(
                "SELECT 1 FROM product_photo WHERE product_id = $1 AND is_primary "
                "AND status = 'active' AND deleted_at IS NULL",
                product_id,
            )
            if not has_primary:
                await _set_primary(conn, product_id=product_id, photo_id=photo_id)
            row = await conn.fetchrow("SELECT * FROM product_photo WHERE id = $1", photo_id)
    return _row_to_dict(row)


async def delete_photo(pool: Any, *, product_id: str, photo_id: str) -> dict[str, Any]:
    """软删除相册记录（文件保留）。删主图时自动把下一张设为主图并还原旧批次图。"""
    async with pool.acquire() as conn:
        async with conn.transaction():
            photo = await conn.fetchrow(
                "SELECT * FROM product_photo WHERE id = $1 AND product_id = $2 "
                "AND deleted_at IS NULL",
                photo_id,
                product_id,
            )
            if photo is None:
                raise HTTPException(404, "photo not found")
            await conn.execute(
                "UPDATE product_photo SET deleted_at = NOW(), is_primary = FALSE, "
                "status = 'deleted', updated_at = NOW() WHERE id = $1",
                photo_id,
            )
            previous = photo["previous_images"]
            if isinstance(previous, str):
                try:
                    previous = json.loads(previous)
                except json.JSONDecodeError:
                    previous = {}
            if photo["is_primary"]:
                # 先把该图覆盖过的批次还原成旧图，再让下一张成为主图。
                for batch_id, old_url in (previous or {}).items():
                    await conn.execute(
                        "UPDATE inventory_batch SET shelf_image_url = $3, "
                        "media_assets = $4::jsonb, updated_at = NOW() "
                        "WHERE id = $1 AND shelf_image_url = $2",
                        batch_id,
                        photo["url"],
                        old_url,
                        json.dumps([old_url] if old_url else [], ensure_ascii=False),
                    )
                nxt = await _next_primary(conn, product_id)
                if nxt is not None:
                    await _set_primary(conn, product_id=product_id, photo_id=str(nxt["id"]))
            row = await conn.fetchrow("SELECT * FROM product_photo WHERE id = $1", photo_id)
    return _row_to_dict(row)


async def reorder_photos(pool: Any, *, product_id: str, photo_ids: list[str]) -> list[dict]:
    async with pool.acquire() as conn:
        async with conn.transaction():
            for index, pid in enumerate(photo_ids, start=1):
                await conn.execute(
                    "UPDATE product_photo SET position = $1, updated_at = NOW() "
                    "WHERE id = $2 AND product_id = $3 AND deleted_at IS NULL",
                    index,
                    pid,
                    product_id,
                )
    return await list_photos(pool, product_id)


_IMAGE_SQL = "COALESCE(NULLIF(c.shelf_image_url,''), c.media_assets->>0, '')"
_REFERENCE_SQL = f"({_IMAGE_SQL} LIKE '/media/spider/%' OR {_IMAGE_SQL} LIKE '/products/%')"


def _is_reference(url: str | None) -> bool:
    return bool(url) and str(url).startswith(_REFERENCE_PREFIXES)


async def reference_products(
    pool: Any,
    *,
    scope: str = "all",
    limit: int = 50,
    offset: int = 0,
    search: str | None = None,
) -> dict[str, Any]:
    """在线且仍是参考图的商品 + 进度。

    scope=store 只列门店 99 SKU（含 STORE-* 批次）；all=全部在线商品。
    进度口径：
      - all：已换 x / (当前参考图 + 已换)；
      - store：已换 x / 98（PRICE_REPORT：99 SKU 中 98 个有参考图）。
    """
    if scope not in ("all", "store"):
        raise HTTPException(400, "scope must be all or store")
    extra: list[str] = []
    params: list[Any] = []
    if scope == "store":
        extra.append(
            "AND EXISTS (SELECT 1 FROM product_variant pv_s "
            "JOIN inventory_batch ib_s ON ib_s.variant_id = pv_s.id "
            "WHERE pv_s.product_id = p.id AND pv_s.deleted_at IS NULL "
            "AND ib_s.deleted_at IS NULL AND ib_s.batch_no LIKE 'STORE-%')"
        )
    if search and search.strip():
        params.append(f"%{search.strip()}%")
        extra.append(f"AND p.title ILIKE ${len(params)}")
    scope_sql = " ".join(extra)

    base = f"""
        WITH first_v AS (
            SELECT DISTINCT ON (pv.product_id) pv.product_id, pv.id AS variant_id
            FROM product_variant pv
            WHERE pv.deleted_at IS NULL AND pv.status = 'active'
            ORDER BY pv.product_id, pv.created_at, pv.id
        ), cover AS (
            SELECT DISTINCT ON (v.product_id) v.product_id, ib.id AS batch_id,
                   ib.batch_no, ib.shelf_image_url, ib.media_assets
            FROM first_v v
            JOIN inventory_batch ib ON ib.variant_id = v.variant_id
            WHERE ib.deleted_at IS NULL AND ib.status = 'active'
            ORDER BY v.product_id,
                     GREATEST(ib.stock_qty - ib.reserved_qty, 0) DESC,
                     ib.created_at
        )
        SELECT p.id, p.title, p.slug, pc.name AS category_name,
               c.batch_id, c.batch_no, c.shelf_image_url,
               COALESCE(NULLIF(c.shelf_image_url,''), c.media_assets->>0, '') AS image_url
        FROM product p
        LEFT JOIN product_category pc ON pc.id = p.category_id
        LEFT JOIN cover c ON c.product_id = p.id
        WHERE p.deleted_at IS NULL AND p.status = 'published'
          AND COALESCE(p.is_test, FALSE) = FALSE
          {scope_sql}
    """
    has_photo_sql = (
        "EXISTS (SELECT 1 FROM product_photo ph WHERE ph.product_id = p.id "
        "AND ph.is_primary AND ph.status = 'active' AND ph.deleted_at IS NULL)"
    )
    async with pool.acquire() as conn:
        rows = await conn.fetch(
            base + f" AND {_REFERENCE_SQL} "
            "ORDER BY p.updated_at DESC NULLS LAST, p.created_at DESC "
            f"LIMIT ${len(params) + 1} OFFSET ${len(params) + 2}",
            *params,
            limit,
            offset,
        )
        total_ref = await conn.fetchval(
            f"SELECT COUNT(*) FROM ({base} AND {_REFERENCE_SQL}) q", *params
        )
        replaced = await conn.fetchval(
            f"SELECT COUNT(*) FROM ({base} AND {has_photo_sql}) q", *params
        )
        store_total = await conn.fetchval(
            "SELECT COUNT(DISTINCT p.id) FROM product p "
            "JOIN product_variant pv ON pv.product_id = p.id "
            "JOIN inventory_batch ib ON ib.variant_id = pv.id "
            "WHERE p.deleted_at IS NULL AND p.status = 'published' "
            "AND pv.deleted_at IS NULL AND ib.deleted_at IS NULL "
            "AND ib.batch_no LIKE 'STORE-%'"
        )
        store_replaced = await conn.fetchval(
            "SELECT COUNT(DISTINCT p.id) FROM product p "
            "JOIN product_variant pv ON pv.product_id = p.id "
            "JOIN inventory_batch ib ON ib.variant_id = pv.id "
            "WHERE p.deleted_at IS NULL AND p.status = 'published' "
            "AND pv.deleted_at IS NULL AND ib.deleted_at IS NULL "
            "AND ib.batch_no LIKE 'STORE-%' AND " + has_photo_sql
        )
        items = []
        for row in rows:
            d = _row_to_dict(row)
            d["is_reference"] = _is_reference(d.get("image_url"))
            items.append(d)
    replaced_n = int(replaced or 0)
    ref_n = int(total_ref or 0)
    return {
        "scope": scope,
        "items": items,
        "progress": {
            "replaced": replaced_n,
            "total": 98 if scope == "store" else ref_n + replaced_n,
            "reference_remaining": ref_n,
            "store_replaced": int(store_replaced or 0),
            "store_total": int(store_total or 0),
        },
    }


async def _store_and_photo(
    *,
    conn: Any,
    output_root: str | Path,
    file: UploadFile,
    product_id: str,
    variant_id: str,
    actor: str,
) -> dict[str, Any]:
    raw = await file.read(MAX_UPLOAD_BYTES + 1)
    if len(raw) > MAX_UPLOAD_BYTES:
        raise HTTPException(413, "file too large (max 10MB)")
    try:
        processed = process_image(raw)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    url, thumb_url = store_image(output_root, processed)
    try:
        photo_id = await conn.fetchval(
            "INSERT INTO product_photo "
            "(product_id, variant_id, url, thumb_url, sha256, width, height, bytes, "
            "original_filename, is_primary, position, status, created_by) "
            "VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,TRUE,1,'active',$10) RETURNING id",
            product_id,
            variant_id,
            url,
            thumb_url,
            processed.sha256,
            processed.width,
            processed.height,
            len(processed.main),
            _clean_filename(file.filename),
            actor,
        )
        previous = await _apply_photo_to_batches(
            conn, product_id=product_id, variant_id=variant_id, url=url
        )
        await conn.execute(
            "UPDATE product_photo SET previous_images = $2::jsonb WHERE id = $1",
            photo_id,
            json.dumps(previous, ensure_ascii=False),
        )
    except Exception:
        remove_stored_image(output_root, url)
        raise
    return await list_photos_by_id(conn, photo_id)


async def list_photos_by_id(conn: Any, photo_id: Any) -> dict[str, Any]:
    row = await conn.fetchrow("SELECT * FROM product_photo WHERE id = $1", photo_id)
    return _row_to_dict(row)


async def create_photo_product(
    pool: Any,
    *,
    output_root: str | Path,
    file: UploadFile,
    title: str,
    category_id: str,
    price_cents: int,
    pricing_mode: str,
    actor: str,
) -> dict[str, Any]:
    """拍照上新：拍照 + 名称/分类/价格/计价方式 → 草稿商品（需人工发布）。"""
    from obase.uuid7 import uuid7

    name = (title or "").strip()
    if not name or len(name) > 80:
        raise HTTPException(400, "商品名称需为 1-80 个字符")
    if price_cents <= 0 or price_cents > 100_000_00:
        raise HTTPException(400, "价格无效")
    if pricing_mode not in ("piece", "weight"):
        raise HTTPException(400, "pricing_mode must be piece or weight")

    async with pool.acquire() as conn:
        category = await conn.fetchval(
            "SELECT name FROM product_category WHERE id = $1 AND deleted_at IS NULL",
            category_id,
        )
        if category is None:
            raise HTTPException(400, "分类不存在")
        location_id = await conn.fetchval(
            "SELECT id FROM stock_location WHERE status = $1 ORDER BY created_at LIMIT 1",
            "active",
        )
        if location_id is None:
            raise HTTPException(400, "没有可用库位")

        token = uuid7().replace("-", "")[-12:]
        product_id = uuid7()
        variant_id = uuid7()
        batch_id = uuid7()
        slug = f"photo-{token}"
        sku = f"PH-{token.upper()}"
        batch_no = f"PHOTO-{datetime.now(UTC).strftime('%Y%m%d%H%M%S')}-{token[:4]}"
        now_batch = {
            "retail": int(price_cents),
            "cost": int(price_cents * 0.6),
            "unit_price": int(price_cents) if pricing_mode == "weight" else None,
            "stock_unit": "gram" if pricing_mode == "weight" else "piece",
        }
        async with conn.transaction():
            await conn.execute(
                "INSERT INTO product (id, title, slug, description, category_id, status) "
                "VALUES ($1,$2,$3,$4,$5,'draft')",
                product_id,
                name,
                slug,
                "店员拍照新增，待补充资料后人工发布。",
                category_id,
            )
            await conn.execute(
                "INSERT INTO product_variant "
                "(id, product_id, sku_code, option_values, reference_price_cents, status, "
                "pricing_mode, unit_price_cents, price_basis_grams, weight_options, stock_unit) "
                "VALUES ($1,$2,$3,'{}'::jsonb,$4,'active',$5,$6,500,$7::jsonb,$8)",
                variant_id,
                product_id,
                sku,
                price_cents,
                pricing_mode,
                now_batch["unit_price"],
                json.dumps(
                    [{"label": "约1斤", "grams": 500}] if pricing_mode == "weight" else [],
                    ensure_ascii=False,
                ),
                now_batch["stock_unit"],
            )
            await conn.execute(
                "INSERT INTO inventory_batch "
                "(id, batch_no, variant_id, location_id, video_url, media_assets, "
                "cost_price_cents, retail_price_cents, stock_qty, inspection_status) "
                "VALUES ($1,$2,$3,$4,'','[]'::jsonb,$5,$6,0,'pending')",
                batch_id,
                batch_no,
                variant_id,
                location_id,
                now_batch["cost"],
                now_batch["retail"],
            )
            photo = await _store_and_photo(
                conn=conn,
                output_root=output_root,
                file=file,
                product_id=str(product_id),
                variant_id=str(variant_id),
                actor=actor,
            )
    return {
        "product_id": str(product_id),
        "variant_id": str(variant_id),
        "batch_id": str(batch_id),
        "title": name,
        "status": "draft",
        "category": category,
        "price_cents": price_cents,
        "pricing_mode": pricing_mode,
        "photo": photo,
    }


# ── HTTP 端点 ──────────────────────────────────────────────────────────


def _pool(request: Any) -> Any:
    from .routers import _pool as pool_of

    return pool_of(request)


def _output_root(request: Any) -> Path:
    return Path(request.app.state.config.output_root)


def _actor(principal: dict[str, Any]) -> str:
    return str(principal.get("claims", {}).get("email") or principal.get("email") or "staff")[:120]


@router.get("/admin/photo-ops/reference")
async def admin_reference_products(
    request: Request,
    scope: str = Query("all"),
    limit: int = Query(50, ge=1, le=200),
    offset: int = Query(0, ge=0),
    search: str | None = None,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    return await reference_products(
        _pool(request), scope=scope, limit=limit, offset=offset, search=search
    )


@router.get("/admin/photo-ops/products/{product_id}/photos")
async def admin_list_photos(
    product_id: str,
    request: Request,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    return {"items": await list_photos(_pool(request), product_id)}


@router.post("/admin/photo-ops/products/{product_id}/photos")
async def admin_upload_photo(
    product_id: str,
    request: Request,
    file: UploadFile = File(...),
    variant_id: str = Form(""),
    make_primary: bool = Form(True),
    principal: dict[str, Any] = Depends(require_staff_role(*_STAFF_ROLES)),
):
    return await add_photo(
        _pool(request),
        output_root=_output_root(request),
        product_id=product_id,
        variant_id=variant_id or None,
        file=file,
        make_primary=make_primary,
        actor=_actor(principal),
    )


@router.post("/admin/photo-ops/products/{product_id}/photos/{photo_id}/primary")
async def admin_photo_primary(
    product_id: str,
    photo_id: str,
    request: Request,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    return await set_photo_primary(_pool(request), product_id=product_id, photo_id=photo_id)


@router.post("/admin/photo-ops/products/{product_id}/photos/{photo_id}/restore")
async def admin_photo_restore(
    product_id: str,
    photo_id: str,
    request: Request,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    return await restore_photo(_pool(request), product_id=product_id, photo_id=photo_id)


@router.delete("/admin/photo-ops/products/{product_id}/photos/{photo_id}")
async def admin_photo_delete(
    product_id: str,
    photo_id: str,
    request: Request,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    return await delete_photo(_pool(request), product_id=product_id, photo_id=photo_id)


@router.post("/admin/photo-ops/products/{product_id}/photos/order")
async def admin_photo_order(
    product_id: str,
    request: Request,
    body: dict[str, Any],
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    photo_ids = body.get("photo_ids")
    if not isinstance(photo_ids, list) or not all(isinstance(i, str) for i in photo_ids):
        raise HTTPException(400, "photo_ids must be a list of strings")
    return {
        "items": await reorder_photos(_pool(request), product_id=product_id, photo_ids=photo_ids)
    }


@router.post("/admin/photo-ops/products")
async def admin_create_photo_product(
    request: Request,
    file: UploadFile = File(...),
    title: str = Form(...),
    category_id: str = Form(...),
    price_cents: int = Form(...),
    pricing_mode: str = Form("piece"),
    principal: dict[str, Any] = Depends(require_staff_role(*_STAFF_ROLES)),
):
    return await create_photo_product(
        _pool(request),
        output_root=_output_root(request),
        file=file,
        title=title,
        category_id=category_id,
        price_cents=price_cents,
        pricing_mode=pricing_mode,
        actor=_actor(principal),
    )


@router.get("/admin/photo-ops/categories")
async def admin_photo_categories(
    request: Request,
    _=Depends(require_staff_role(*_STAFF_ROLES)),
):
    async with _pool(request).acquire() as conn:
        rows = await conn.fetch(
            "SELECT id, name, slug FROM product_category WHERE deleted_at IS NULL ORDER BY name"
        )
    return [{"id": str(r["id"]), "name": r["name"], "slug": r["slug"]} for r in rows]


@router.get("/store/my-capabilities")
async def my_capabilities(
    request: Request,
    principal: dict[str, Any] = Depends(get_current_customer),
):
    """顾客侧能力位：店员邮箱账号, 或已经传过货的仓主, 才显示传货入口。

    仓主保持原有传货能力；普通顾客看不到入口 (避免误触拍照上架)。
    仓主只能覆盖自己上传的图, 由 /store/ingest 的服务端校验兜底。
    """
    async with _pool(request).acquire() as conn:
        row = await conn.fetchrow(
            'SELECT c.email, u.id AS staff_id FROM "customer" c '
            'LEFT JOIN "app_user" u ON lower(u.email) = lower(c.email) '
            "AND u.deleted_at IS NULL AND u.status = 'active' "
            "WHERE c.id = $1 AND c.deleted_at IS NULL",
            principal["customer_id"],
        )
        is_staff = bool(row and row["staff_id"])
        own_uploads = False
        if row is not None:
            own_uploads = bool(
                await conn.fetchval(
                    'SELECT 1 FROM "inventory_batch" ib '
                    'JOIN "product_variant" pv ON pv.id = ib.variant_id '
                    'JOIN "product" p ON p.id = pv.product_id '
                    "WHERE ib.deleted_at IS NULL AND p.deleted_at IS NULL "
                    "AND ib.shelf_image_url LIKE $1 LIMIT 1",
                    f"/media/agent_ingest/c_{principal['customer_id']}/%",
                )
            )
    return {"can_ingest": is_staff or own_uploads, "is_staff": is_staff}


async def customer_is_staff(pool: Any, customer_id: str) -> bool:
    async with pool.acquire() as conn:
        row = await conn.fetchrow(
            'SELECT 1 FROM "customer" c JOIN "app_user" u '
            "ON lower(u.email) = lower(c.email) AND u.deleted_at IS NULL "
            "AND u.status = 'active' WHERE c.id = $1 AND c.deleted_at IS NULL",
            customer_id,
        )
    return row is not None


def json_error(status_code: int, detail: str) -> JSONResponse:
    return JSONResponse(status_code=status_code, content={"detail": detail})

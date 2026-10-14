"""hemall CLI — 数据库迁移与运行时权限授予。

用法:
    python -m app.cli migrate          # 以 owner 角色执行 DDL (建表/加列/建索引)
    python -m app.cli grant-runtime    # 创建/更新 runtime 角色并授予 DML 权限
    python -m app.cli check-permissions  # 检查当前角色权限
"""

from __future__ import annotations

import asyncio
import sys
from urllib.parse import urlparse

import click
from obase.persistence.pool import PgPool

from .config import Settings


# ── SQL 定义 ───────────────────────────────────────────────────────────────

# 运行时角色名
RUNTIME_ROLE = "hemall_runtime"
# 迁移/管理员角色名 (连接时用的用户，通常是 postgres 或 hemall_owner)
OWNER_ROLE = "hemall_owner"

# 创建运行时角色并授予 DML 权限 (无 DDL)
GRANT_RUNTIME_SQL = f"""
DO $$
BEGIN
    -- 创建运行时角色 (无登录权限，仅用于 SET ROLE)
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{RUNTIME_ROLE}') THEN
        CREATE ROLE {RUNTIME_ROLE} NOLOGIN;
    END IF;
END
$$;

-- 授予运行时角色对 schema 的 USAGE 权限
GRANT USAGE ON SCHEMA public TO {RUNTIME_ROLE};

-- 授予运行时角色对所有现有表的 DML 权限
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO {RUNTIME_ROLE};

-- 授予运行时角色对所有现有序列的 USAGE/SELECT 权限
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO {RUNTIME_ROLE};

-- 设置默认权限：未来创建的表/序列自动授予运行时角色 DML 权限
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO {RUNTIME_ROLE};

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO {RUNTIME_ROLE};

-- 将运行时角色授予 owner 角色 (以便 SET ROLE); owner 角色不存在时跳过
-- (常见连接用户是 postgres 超级用户, 无需显式 GRANT 即可 SET ROLE)
DO $$
BEGIN
    IF EXISTS (SELECT 1 FROM pg_roles WHERE rolname = '{OWNER_ROLE}') THEN
        GRANT {RUNTIME_ROLE} TO {OWNER_ROLE};
    END IF;
END
$$;
"""

# 撤销运行时角色的 DDL 权限 (显式确保无 CREATE/DROP/ALTER)
REVOKE_DDL_SQL = f"""
-- 显式撤销 schema 级 DDL 权限 (防止通过 public schema 默认权限获得)
REVOKE CREATE ON SCHEMA public FROM {RUNTIME_ROLE};

-- 撤销库级 CREATE 权限 (REVOKE 不接受函数调用, 用 DO 块动态拼接库名)
DO $$
BEGIN
    EXECUTE 'REVOKE CREATE ON DATABASE ' || quote_ident(current_database()) || ' FROM {RUNTIME_ROLE}';
END
$$;

-- 确保运行时角色无法执行 DDL
-- 注意: 无法直接 REVOKE ALTER/DROP/TRUNCATE ON ALL TABLES，需在对象级控制
-- 这里通过不授予这些权限来实现最小权限原则
"""

# 检查权限的查询
CHECK_PERMISSIONS_SQL = f"""
SELECT
    r.rolname AS role_name,
    r.rolcanlogin AS can_login,
    r.rolcreaterole AS can_create_role,
    r.rolcreatedb AS can_create_db,
    has_database_privilege(r.oid, current_database(), 'CREATE') AS db_create,
    has_schema_privilege(r.oid, 'public', 'CREATE') AS schema_create,
    has_table_privilege(r.oid, 'public.customer', 'SELECT') AS customer_select,
    has_table_privilege(r.oid, 'public.customer', 'INSERT') AS customer_insert,
    has_table_privilege(r.oid, 'public.customer', 'UPDATE') AS customer_update,
    has_table_privilege(r.oid, 'public.customer', 'DELETE') AS customer_delete,
    has_table_privilege(r.oid, 'public.customer', 'TRUNCATE') AS customer_truncate,
    has_table_privilege(r.oid, 'public.customer', 'TRIGGER') AS customer_trigger,
    has_table_privilege(r.oid, 'public.customer', 'REFERENCES') AS customer_references
FROM pg_roles r
WHERE r.rolname IN ('{OWNER_ROLE}', '{RUNTIME_ROLE}', current_user)
ORDER BY r.rolname;
"""


async def _get_pool(dsn: str) -> PgPool:
    """创建临时连接池用于 CLI 操作。"""
    pool = await PgPool.create(
        name="hemall-cli",
        dsn=dsn,
        min_size=1,
        max_size=2,
    )
    return pool


async def _run_migrate(dsn: str) -> None:
    """执行数据库迁移 (DDL) - 需要 owner 权限。"""
    pool = await _get_pool(dsn)
    try:
        async with pool.acquire() as conn:
            # 执行 bootstrap 中的 schema 初始化逻辑。
            # apply_runtime_role=False: 迁移进程连接的是 owner, DDL 之后不切换
            # 运行时角色 (切换是 API 容器启动时做的事)。
            from app.bootstrap import init_db

            settings = Settings(pg_dsn=dsn)
            await init_db(settings, apply_runtime_role=False)
        click.echo("✅ Migration completed successfully")
    finally:
        await pool.close()


async def _run_grant_runtime(dsn: str) -> None:
    """创建/更新 runtime 角色并授予 DML 权限。"""
    pool = await _get_pool(dsn)
    try:
        async with pool.acquire() as conn:
            # 先执行授权
            await conn.execute(GRANT_RUNTIME_SQL)
            # 再撤销 DDL 权限
            await conn.execute(REVOKE_DDL_SQL)
        click.echo(f"✅ Runtime role '{RUNTIME_ROLE}' granted DML permissions")
        click.echo(f"   Owner role '{OWNER_ROLE}' can SET ROLE {RUNTIME_ROLE}")
    finally:
        await pool.close()


async def _run_check_permissions(dsn: str) -> None:
    """检查当前连接的角色权限。"""
    pool = await _get_pool(dsn)
    try:
        async with pool.acquire() as conn:
            rows = await conn.fetch(CHECK_PERMISSIONS_SQL)
            click.echo("\nRole Permissions:")
            click.echo("-" * 120)
            for row in rows:
                click.echo(f"Role: {row['role_name']}")
                click.echo(f"  can_login: {row['can_login']}, can_create_role: {row['can_create_role']}, can_create_db: {row['can_create_db']}")
                click.echo(f"  db_CREATE: {row['db_create']}, schema_CREATE: {row['schema_create']}")
                click.echo(f"  customer SELECT: {row['customer_select']}, INSERT: {row['customer_insert']}, UPDATE: {row['customer_update']}, DELETE: {row['customer_delete']}")
                click.echo(f"  customer TRUNCATE: {row['customer_truncate']}, TRIGGER: {row['customer_trigger']}, REFERENCES: {row['customer_references']}")
    finally:
        await pool.close()


def _resolve_dsn(settings: Settings) -> str:
    """解析有效 DSN，优先使用 effective_pg_dsn。"""
    return settings.effective_pg_dsn


@click.group()
def cli() -> None:
    """hemall 管理命令行工具。"""
    pass


@cli.command()
def migrate() -> None:
    """以 owner 角色执行数据库迁移 (DDL: 建表/加列/建索引)。

    需要连接用户拥有 CREATE 权限 (通常是 postgres 或 hemall_owner)。
    生产环境建议通过一次性迁移容器执行，API 容器使用 runtime 角色。
    """
    settings = Settings()
    dsn = _resolve_dsn(settings)
    click.echo(f"Running migrations with DSN: {_mask_dsn(dsn)}")
    asyncio.run(_run_migrate(dsn))


@cli.command()
def grant_runtime() -> None:
    """创建/更新 hemall_runtime 角色并授予 DML 权限 (无 DDL)。

    此命令需要以超级用户或拥有 GRANT 权限的用户执行。
    执行后，API 容器应使用 SET ROLE hemall_runtime 运行。
    """
    settings = Settings()
    dsn = _resolve_dsn(settings)
    click.echo(f"Granting runtime permissions with DSN: {_mask_dsn(dsn)}")
    asyncio.run(_run_grant_runtime(dsn))


@cli.command()
def check_permissions() -> None:
    """检查当前数据库角色的权限矩阵。"""
    settings = Settings()
    dsn = _resolve_dsn(settings)
    click.echo(f"Checking permissions with DSN: {_mask_dsn(dsn)}")
    asyncio.run(_run_check_permissions(dsn))


def _mask_dsn(dsn: str) -> str:
    """脱敏 DSN 用于日志输出。"""
    try:
        parsed = urlparse(dsn)
        if parsed.password:
            masked = parsed._replace(netloc=f"{parsed.username}:***@{parsed.hostname}:{parsed.port or 5432}")
            return urlparse(masked.geturl()).geturl().replace(parsed.geturl(), masked.geturl())
        return dsn
    except Exception:
        return "<invalid-dsn>"


if __name__ == "__main__":
    cli()
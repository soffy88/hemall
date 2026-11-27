-- Hemall PostgreSQL 最小权限运行时角色
--
-- 仅生成，不在本次修复中执行。上线时由 DBA 以 owner 角色执行一次，然后让
-- API 连接池在 DDL 完成后 SET ROLE hemal_runtime（现有 bootstrap 已支持）。
-- 迁移/DDL 仍使用 owner；运行时角色无 LOGIN、无 CREATE、无 TRUNCATE。

BEGIN;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'hemall_runtime') THEN
        CREATE ROLE hemal_runtime NOLOGIN;
    END IF;
END
$$;

ALTER ROLE hemal_runtime NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION;

GRANT USAGE ON SCHEMA public TO hemal_runtime;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO hemal_runtime;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO hemal_runtime;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO hemal_runtime;
ALTER DEFAULT PRIVILEGES IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO hemal_runtime;

REVOKE CREATE ON SCHEMA public FROM hemal_runtime;
REVOKE TRUNCATE, REFERENCES, TRIGGER ON ALL TABLES IN SCHEMA public FROM hemal_runtime;

DO $$
BEGIN
    EXECUTE 'REVOKE CREATE ON DATABASE ' || quote_ident(current_database())
        || ' FROM hemal_runtime';
END
$$;

-- owner 连接用户必须能切换到运行时角色。将下面 <OWNER_ROLE> 替换为实际
-- 迁移账号（例如 hemal_owner），不要使用 postgres 超级用户作为长期 API 账号。
-- GRANT hemal_runtime TO <OWNER_ROLE>;

COMMIT;

-- 验证（只读）：
-- SELECT r.rolname, r.rolcanlogin, r.rolsuper, r.rolcreatedb, r.rolcreaterole
-- FROM pg_roles r WHERE r.rolname IN ('hemall_runtime', '<OWNER_ROLE>');
-- SELECT has_schema_privilege('hemall_runtime', 'public', 'CREATE') AS can_create_schema;
-- SELECT has_table_privilege('hemall_runtime', 'public.customer', 'TRUNCATE') AS can_truncate;

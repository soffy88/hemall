-- Hemall Production Database Initialization Script
-- This script runs on first container start

-- Enable required extensions
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";

-- Set timezone
ALTER DATABASE hemall_prod SET timezone TO 'Asia/Shanghai';

-- Create initial schema (if not using migrations)
-- Note: This is a placeholder. Actual migrations should be handled by Alembic or similar tool.

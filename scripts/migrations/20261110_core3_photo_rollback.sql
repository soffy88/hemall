-- 回滚 20261110 core3。旧代码不读 product_photo；回滚后可安全保留数据。
-- 本脚本只删本轮新增的表/索引（会丢失相册历史记录，但商品批次上的图不变，
-- 上传文件保留在 hemall_output/product_photo 卷内，不物理删除）。
BEGIN;
DROP TABLE IF EXISTS product_photo;
COMMIT;

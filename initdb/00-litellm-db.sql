-- LiteLLM 代理用自己的库存 virtual key 与 spend 记录，与业务表分开。
-- 只在 pgdata 卷首次初始化时执行；已有卷请手动：
--   docker compose exec postgres psql -U cowork -c "CREATE DATABASE litellm;"
CREATE DATABASE litellm;

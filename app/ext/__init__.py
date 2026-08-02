"""app.ext — hemall 原生内建扩展域。

零搜索、零耗材、零抽成、全自动微仓网格零售底座。Batch (批次) 是最高维度的
交易实体，彻底剥离 SPU 定价——products/variants 只是极薄的展示层描述，
价格/库存/溯源都挂在 inventory_batches 上。

复用 obase 的工程支柱 (Trail/BaseConfig/build_result/compute_fingerprint/
PgPool/ProviderRegistry)，不重新发明这些跟具体业务无关的机制。

分层对齐 3O Paradigm SPEC v1.0：
    schema.py  — obase 层：DDL (幂等建表)
    oprim.py   — oprim 层：原子操作 (数据库原语 + 领域专属原语)
    omodul/    — omodul 层：端到端业务事务 (四大支柱，一函数一文件)
    (oskill / oservi 层留待后续 phase)
"""

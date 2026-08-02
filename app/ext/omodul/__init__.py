"""app.ext.omodul — hemall 扩展层元功能：核心业务事务。

一函数一文件，对齐 platform/3O/omodul 包自己的组织方式。严格遵守
(config, input_data, output_dir) 签名 + {"status": "completed"|"failed", ...}
返回契约；omodul 之间禁止裸调。

已实现 (SPEC v1.0 §4 全部 15 个 + v2.0 §5 引擎描述隐含的 1 个)：
    §4.1 批次与供应链
        create_inventory_batch — 批次入库
        create_crowd_intent    — C2B 逆向集单
        mark_batch_for_disposal — 过期批次销毁
        batch_settlement       — 售罄结清供应商货款
    §4.2 交易与锁单
        add_line_item_to_cart      — 加车 (强绑定 batch_id)
        cart_shipping_method_set   — 锁定三档运费契约
        complete_checkout          — 结账 (硬锁库存 + 价格校验 + 扣款 + 建单)
        process_subscription       — 收会员费，开通购买权限
    §4.3 物理流转与无言售后
        confirm_batch_pick      — 拣货确认，转出库 + 记计件工资
        tote_deposit_and_refund — 循环筐押金扣除/秒退
        process_drop_return     — 回收桶扫码退货秒退
        commission_new_location — 新微仓节点注册
    §4.4 去中心化分润结算
        dispatch_labor_payment  — 聚合结算大妈计件工资
        dispatch_host_dividend  — 宿主场地分润结算
        execute_ambient_replenishment — 环境式自动补货 (预测见底 + 到期自动
            扣款 + 推单至最近微仓，用 oskill.predict_household_burn_rate
            + oprim.math_haversine_distance)
    v2.0 §4 全部 7 个 + §5 引擎描述隐含的 1 个 (execute_liability_routing_workflow)
        claim_origin_workflow — 果农 PWA 供应商注册
        execute_slashing_workflow — 斩仓处罚 (3 倍罚金 + 冻结)
        report_phantom_stock_workflow — 幽灵库存清零 + 秒退 + 记损耗账本
        execute_ambient_intake_workflow — 顶棚 CV 信号自动点亮微仓
        execute_peer_delivery_workflow — 邻居代送确认 (释放 Tote + 悬赏结算)
        process_credit_gated_rma_workflow — 轻量客诉仲裁 (不需要 VLM 判损)
        generate_crushing_offer_workflow — 竞对小票比价狙击，生成限时暴击订单
        execute_liability_routing_workflow — 全自动仲裁执行末端 (§4.3 文字
            描述之外，§5 autonomous_triage_engine 明确点名调用；不重复实现
            oprim.vlm_assess_damage / oskill.evaluate_claim_credibility——
            那两步是引擎自己做的，见 oservi.py)，instant 分支按损坏类型判责，
            liable_party="supplier" 扣 escrow，="location" 记 location_loss_ledger
    v6.0 §4 全部 2 个 + 1 个 SPEC 未写出但标题隐含要求的空白补齐
        process_cloud_franchise_claim_workflow — 抖音粉丝云加盟认领 (1 公里
            空间冲突检测，纯 haversine，非 PostGIS)
        bind_digital_lord_contract_workflow — 数字领主永久契约册封 (点火成
            功时把节点从 pending_hardware 促活成 active)
        record_douyin_conversion_workflow — 抖音转化落账 (SPEC §4 标题写"每
            一笔分润的强制落库"，但给出的两个函数都没有真正写 conversion
            log；这个 omodul 补上"匹配契约 → 算分润 → 落库"这一步，领主税
            按订单落在哪个节点判定，跟请求带的 douyin_uid 无关，雇佣兵悬赏
            则需要 douyin_uid 才能归因)
"""

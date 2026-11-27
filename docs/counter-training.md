# 柜台识别训练 harness

状态：**照片未到之前不训模型。** 当前线上 `counter_frame 0`、
`counter_recognition_event 0`，磁盘无 `counter_dataset` 目录，
400 张实拍照片 0 张未到。本模块只提供训练架子 + 评估门禁，
不含任何真模型、真权重、真照片。

## 数据集规范

依据《柜台识别实拍照片规范 v1》（2026-11-04）：10 个 SKU，
每 SKU 40 张（30 训练 + 10 测试），共 400 张；测试集单独拍、单独记，
上传时 note 字段写 `test`；机位、灯光、背景全程固定。

| # | 商品 | 计价基准 | variant_id |
| --- | --- | --- | --- |
| 1 | 上海青 | 400g | 5b29ccf4-e785-44cb-8b7f-ba1ed30ac2dc |
| 2 | 土豆 | 1000g | fa69b838-90b7-4632-b1e0-d8c15d4e34a5 |
| 3 | 胡萝卜 | 500g | 12d7ad64-d009-404a-9a38-52f7a71ce0a7 |
| 4 | 西红柿 | 500g | 005d0a96-2ac9-411e-b401-88db94361043 |
| 5 | 黄瓜 | 500g | 5292049f-2939-40a1-abb1-f86fc2f02887 |
| 6 | 西兰花 | 300g | 92b1af28-046c-4900-a610-cc8e066bddfd |
| 7 | 猪五花肉 | 500g | 17e35059-c999-42de-a5ce-5693cb66c8da |
| 8 | 冷鲜鸡胸肉 | 500g | 743c0abb-8f45-4312-93a8-01eb1b55d292 |
| 9 | 鲈鱼 | 500g | 54c4498a-9e82-4dce-9cab-71dac54906cf |
| 10 | 红富士苹果 | 1000g | 3d2912e4-5b82-498d-9972-04ee7873eb13 |

上传接口：`POST /store/counter/dataset-photos`，落盘
`<output_root>/counter_dataset/<sku>/` + `manifest.json`（每条记录含
`file`、`sku`、`variant_id`、`note`、`sha256` 等）。训练标签以 manifest
的 `variant_id` 为准，目录名 sku 只作报告展示，不编映射。

> **待解决（照片上传前必须先定）：SKU 目录名冲突。** 规范说“sku 填
> 标题全称”（上海青等中文），但上传接口用的 `sanitize_sku` 只接受
> ASCII 字母数字 + `_ . -`，中文 sku 会直接 400。照片到了之后二选一：
> （a）拍摄方按双方约定的 ASCII 代号填 sku（如拼音）；（b）另批接口
> 改动放宽白名单。本 harness 按磁盘实际目录名读，不挑名字，但训练
> 标签始终以 manifest 的 `variant_id` 为准，所以无论选哪种都不用改
> harness。

## 选型

- **分类器：最近质心**（`app/counter_training/model.py`）。每类求特征
  均值，预测按质心距离排序。理由：CPU 秒级（400 张图训练 < 1 分钟
  量级）、零新依赖（只用 Pillow + 标准库；Pillow 已是项目依赖）、
  确定性（同一批照片永远同一模型）、模型是可读 JSON（无 pickle，
  无任意代码执行风险）。分数是距离 softmax 的排序置信度，不是校准
  概率，只用于前 3 候选排序。
- **特征：88 维**（`app/counter_training/features.py`）：8x8 灰度
  （64）+ 每通道 8 档颜色直方图（24）。柜台散货第一版用颜色 + 轮廓
  灰度做基线；如果它在实拍测试集上过不了门禁，门禁会拦住注册，
  下一步是换更强的模型（例如用已在依赖里的 faiss-cpu/onnxruntime
  做嵌入检索），而不是调低门禁。
- **不引 GPU 依赖、不引付费 API、不走云端**：训练在店内电脑离线跑，
  不按次收费。

## 训练命令

数据集在服务器 `<output_root>/counter_dataset/`（与上传接口同一目录；
默认 `output_root` 为 `./var/omodul_output`，生产值以部署配置为准）：

```bash
python -m app.counter_training.train \
  --data <output_root>/counter_dataset \
  --out <模型路径>/counter-centroid-v1.json
```

- 报告默认写在模型同名 `.eval.md`（可用 `--report` 另指）。
- 可选 `--variants-file <白名单.json>`：从库里导出 variant UUID 列表
  再核一道（上传时接口已核对过一次，不给也行）。
- 断点续跑：中断后加 `--resume` 重跑，特征缓存
  （`<模型同名>.features.json`，按照片 sha256 索引）命中已提过的照片，
  不再重复解码；缓存删了也能完整重跑。
- 退出码：`0` 门禁通过；`2` 无训练数据 / 不可训练；`3` 门禁不通过
  （模型与报告仍保存，但模型不可注册）。

## 空数据行为

数据集目录不存在，或可用训练照片为 0 张时，训练明确报错退出
（stderr 印 `无训练数据：…`，退出码 2），不生成假模型、不写假评估
报告。无测试集（note=test 的照片 0 张）时训练可跑、评估无法验证，
门禁直接不通过（退出码 3）。

## 评估门禁

位置：`app/counter_training/evaluate.py`（`TOP1_MIN` / `TOP3_MIN`）、
注册复核：`app/counter_training/model.py`（`CentroidModel.eval_passed`）。

- 留出测试集（note=`test` 的照片）算 top-1 / top-3 准确率；
- **门禁：top-1 >= 90% 且 top-3 >= 98% 才算通过**；
- 不通过的模型绝不注册：模型文件里嵌的评估摘要标 `gate_passed=false`，
  注册加载时直接拒绝；门禁阈值不可调低，不过就补拍 / 纠正错判集中的
  SKU 后重训。
- 报告格式见 `docs/counter-training-eval-template.md`（数据集构成、
  top-1/top-3、每 SKU 明细、门禁结论）。

## 注册步骤

当前注册点保持 **dormant**（只留接口与测试，未接启动流程）：

1. 照片齐了、训练跑完、评估报告门禁**通过**（三者缺一不可）。
2. 模型文件放服务器固定位置（建议与数据集同级
   `<output_root>/counter_models/`，落点由部署时定），环境变量
   `COUNTER_MODEL_PATH` 指向它。
3. 启动流程里调一次 `register_trained_recognizer()`
   （`app/counter_training/registry.py`）：加载时复核模型内嵌的评估
   摘要——文件合法 + `gate_passed` 为真 + top-1/top-3 数字重新核对过
   阈值 + 测试集张数 > 0，缺一个就返回 False、保持 NullRecognizer。
   **第 3 步的启动接线须另批，本 PR 不做。**
4. 验证：调 `POST /store/counter/recognize` 应返回非空候选 +
   `model_configured=true`；再跑一轮确认口子抽查。

未配模型时保持现有行为：识别端点返回 200 空候选 +
`model_configured=false`，调用方退回拍照点选。

## 照片到了之后的操作清单

1. 核对张数：10 个 SKU 各 40 张（30 训练 + 10 测试）是否齐了；
   先解决上面“SKU 目录名冲突”待定项再上传。
2. 上传：商家账号调 `POST /store/counter/dataset-photos`，`sku`、
   `variant_id`（上表）、`note`（训练填形态如 单件/堆放/换角度，
   测试集填 `test`）按规范填；废片（糊 / 暗 / 过曝 / 出画 / 有其他
   商品）不传。
3. 抽查落盘：服务器上看 `<output_root>/counter_dataset/<sku>/`
   各目录张数 + `manifest.json` 条数对不对得上。
4. 导出 variant 白名单（可选）：从库里导出 10 个 variant UUID 成
   JSON 列表，供 `--variants-file` 再核一道。
5. 跑训练（服务器上，命令见上）；先看 stdout 的警告——被跳过的照片
   要逐条认（manifest 缺失 / sha 不符 / 不可读都不是小事）。
6. 读评估报告（`.eval.md`）：看 top-1/top-3、每 SKU 明细、最常见错判。
7. 门禁通过 → 按“注册步骤”走（模型落点 + 环境变量 + 启动接线另批）；
   门禁不通过 → 补拍错判集中的 SKU（或纠正标注）后重训，绝不调低门禁。
8. 注册后验证识别端点 + 确认口子抽查；柜台仍是“识别给前 3 候选、
   店员点确认”，达标前不自动确认。

## 测试

`tests/test_counter_training.py`：纯单元测试，不碰 DB。fixture 是代码
现场生成的纯色小图（仅验证 pipeline 机械结构），绝不放进
`counter_dataset/`，绝不用来声称训出模型。

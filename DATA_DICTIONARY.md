# EarWise采集系统 · 数据字段字典（schema_version 1.0）

每个 session 表示一个被试、一个 day、一个 round 的独立尝试，只有 `eeg_raw.csv`、`labels.csv`、`config.json` 三个正式结果文件。当前协议 `earwise-3day-2round-4trial-v2` 共 3 天，每天 2 轮，每轮 4 个 trial 和 4 次量表，开头仅一次 30 秒基线。第 1 轮条件为 attention → relax → attention → relax，第 2 轮为 relax → attention → relax → attention。`mode=simulation` 的模拟记录位于 `simulation_data`；`mode=real` 的真实记录位于 `data`。两类数据不能合并当作同一实验来源。

软件版本为 `1.1.0`，数据字段 `schema_version` 保持 `1.0`；素材清单 manifest schema 为 `2.0`。旧版每轮两 trial 的记录不迁移、不覆盖，也不计入当前协议的既往尝试；分析时必须结合 `experiment_protocol_id` 与 `trials`，不能仅凭相同 day/round 将新旧轮次混为一组。

CSV 使用 UTF-8、固定表头、标准双引号转义和 `.` 小数点。不适用或未知值为空字段；JSON 对应值为 `null`。时间均含单位，UTC 字符串带时区。三文件的 `session_id` 一致。

## eeg_raw.csv

每行是一个实际收到并成功解析的双通道 33 字节帧；不补样本、不补零成八通道，不用滤波或平滑值覆盖原始数据。

| 字段 | 类型 / 单位 | 含义 |
|---|---|---|
| `session_id` | 字符串 | 本轮尝试唯一标识 |
| `sample_row_index` | 整数，从 0 开始 | 本文件连续保存行号；不是设备采样编号 |
| `stream_epoch_id` | 整数 | 连续流区段标识；断连或歧义区间后换段 |
| `notification_id` | 整数 | 主机接到的 BLE 通知编号 |
| `frame_in_notification` | 整数，从 0 开始 | 本次通知解出的帧顺序；跨通知拼帧归完成重组的通知 |
| `device_seq` | 整数 0–255 | 原始 8 位设备序号 |
| `received_at_utc` | ISO 8601 | 主机接收 UTC 时刻 |
| `received_monotonic_ns` | 整数 / 纳秒 | 同一后端进程的单调接收时刻 |
| `elapsed_ms` | 数值 / 毫秒 | 相对正式采集起点的主机接收时间 |
| `channel_0_raw`、`channel_1_raw` | 整数 / ADC counts | 大端、有符号 24 位原值，范围 −8388608 至 8388607 |
| `channel_0_uv`、`channel_1_uv` | 可空数值 / μV | 仅 `uv_verified=true` 时按本次快照 `uv_scale` 换算，默认留空 |
| `frame_status` | 字符串 | `normal`、`duplicate`、`sequence_ambiguous` 等；可解码重复帧仍保存 |
| `original_frame_hex` | 66 个十六进制字符 | 原始 33 字节帧，便于追溯，不另存 `.bin` |

同一次 BLE 通知可能解出多个帧，接收时刻可以相同。接收时间不等于设备采样时间。`channel_0/1` 始终代表物理原始通道，左右耳含义查本次 `config.json.channel_mapping`，不依赖界面卡片顺序。

## labels.csv

统一追加事件表：阶段、播放、评分、质量、异常在同一文件。按接收顺序追加，但映射后的前端事件时间可能与文件顺序不同。

| 字段 | 类型 / 单位 | 含义 |
|---|---|---|
| `session_id`、`event_id` | 字符串 | 会话 ID、唯一事件 ID |
| `event_type` | 字符串 | 见下方事件表 |
| `event_time_utc` | ISO 8601 | 事件统一 UTC 时刻 |
| `event_monotonic_ns` | 可空整数 / 纳秒 | 后端统一时基；前端事件使用校时映射值 |
| `elapsed_ms` | 可空数值 / 毫秒 | 相对正式开始的事件时间 |
| `server_received_monotonic_ns` | 可空整数 / 纳秒 | 后端处理/收到该事件的时间，供审计 |
| `client_event_id` | 可空字符串 | 前端事件去重 ID |
| `client_performance_ms` | 可空数值 / 毫秒 | 浏览器原始 performance 时刻 |
| `sync_uncertainty_ms` | 可空数值 / 毫秒 | 往返校时误差估计；不是光学测得的呈现精度 |
| `last_sample_row_index` | 可空整数 | 后端处理事件时最近收到的 EEG 行号；不是精确刺激起始样本 |
| `stream_epoch_id` | 可空整数 | 当前连续流区段 |
| `stage` | 可空字符串 | `baseline/attention/relax/transition/questionnaire/video_buffering` |
| `trial_id` | 可空字符串 | 唯一 trial ID；同轮重复条件的两个 trial 仍有不同 ID，评分据此关联刚结束的视频 |
| `trial_order` | 可空整数 1–4 | 本轮 trial 顺序，每个 trial 对应一次量表 |
| `condition` | 可空字符串 | `attention` 或 `relax`，与当前阶段分开表达 |
| `video_id` | 可空字符串 | 稳定素材 ID，例如 `attention_01`、`relax_01` |
| `video_order_in_trial` | 可空整数 1 | 每个 trial 只有一个视频 |
| `media_position_ms` | 可空数值 / 毫秒 | 浏览器报告的视频当前位置 |
| `attention_score` | 可空整数 1–5 | 仅 attention 的 `RATING_SUBMITTED` 填写 |
| `relax_score` | 可空整数 1–5 | 仅 relax 的 `RATING_SUBMITTED` 填写 |
| `confidence_score` | 可空整数 1–5 | 对刚提交程度评分的确信度 |
| `details_json` | 可空 JSON 字符串 | 质量快照、原因、解析错误、时序审计等扩展信息 |

应使用 CSV 解析器读取，再对 `details_json` 调用 JSON 解析，不要按逗号手工切割整行。问卷阶段的 EEG 是 `questionnaire`，不会因为量表评价 attention/relax 而自动归入该刺激阶段。

| 事件组 | 事件 | 含义 |
|---|---|---|
| 会话 | `SESSION_START/SESSION_END` | 正式采集范围，结束详情记录状态及原因 |
| 阶段 | `STAGE_START/STAGE_END` | 重建实际阶段区间，使用 `[开始,结束)` |
| 基线 | `BASELINE_START/BASELINE_END` | 每轮一次 30 秒基线 |
| trial | `TRIAL_START/TRIAL_END` | 首次实际播放与正常播放结束；缓冲恢复不重复开启 trial |
| 视频 | `VIDEO_PLAYING/VIDEO_ENDED/VIDEO_WAITING/VIDEO_PAUSED/VIDEO_ERROR` | 实际媒体事件及位置；等待/缓冲不当作持续观看 |
| 量表 | `QUESTIONNAIRE_START/RATING_SUBMITTED` | 评分通过 trial_id 关联，重试不重复记分 |
| 设备/连续性 | `BLE_DISCONNECTED/BLE_RECONNECTED/DATA_TIMEOUT/DATA_GAP/SEQUENCE_ANOMALY` | 设备事件、流中断和序号歧义；按实际发生记录 |
| 解析 | `PARSE_ERROR` | 格式错误字节计数；没有实现 CRC 验证 |
| 质量 | `QUALITY_SNAPSHOT` | 每秒及结束时记录窗口计数与指标 |
| 校时 | `CLOCK_SYNC` | 控制页面更新的 RTT 中点锚点、偏移和误差依据 |
| 异常结束 | `SESSION_ABORTED/SESSION_INTERRUPTED/SAVE_ERROR` | 主动中止、异常中断、保存错误；随后尽力写入会话结束 |

进程崩溃恢复追加的 `SESSION_INTERRUPTED` 只知道恢复时的 UTC；不会伪造上一进程的单调时间，相关字段留空。

### QUALITY_SNAPSHOT 的 details_json

| 字段 | 含义 |
|---|---|
| `window_start_monotonic_ns/window_end_monotonic_ns` | 统计窗口边界 |
| `window_seconds/actual_window_seconds` | 配置窗口 / 已累积实际窗口秒数 |
| `unique_frames/duplicate_frames/lost_frames` | 窗口唯一帧、重复帧、可信区段推定缺帧数 |
| `saturated_0_frames/saturated_1_frames` | 各物理通道达到原始阈值的唯一帧数 |
| `saturation_denominator` | 两通道各自有效独立样本分母；同帧都含两路 |
| `loss_denominator` | 唯一帧 + 推定缺帧 |
| `saturation_0_pct/saturation_1_pct` | 各通道饱和率百分比；无新鲜数据为 null |
| `loss_pct` | 两通道共享帧丢包率估计百分比；未知缺口/中断时 null |
| `unknown_gap/unknown_gap_count/unknown_gap_reasons` | 窗口内未知缺口状态、次数、原因 |
| `data_status/data_insufficient` | waiting/receiving/interrupted，及窗口数据是否不足 |
| `last_frame_monotonic_ns/last_received_age_seconds` | 最近帧时刻及距今秒数 |
| `saturation_lower_threshold/saturation_upper_threshold` | 实际使用的 ADC counts 阈值 |
| `stream_epoch_id/algorithm_version` | 区段 ID 和算法版本 |

丢包率为 `lost_frames / (unique_frames + lost_frames) × 100%`；重复帧不增加分母。饱和率为各通道 `saturated_*_frames / saturation_denominator × 100%`。短时序号估计不能识别整圈丢失；未知不是 0。质量提示不自动剔除数据。

## config.json

配置中的实验参数在开始时冻结；运行阶段和 trial 完成状态随过程持久化，结束时补充摘要，不依赖日后可能变动的项目设置。

| 字段或分组 | 内容 |
|---|---|
| `schema_version/software_version/python_version/platform` | 三文件格式、软件和运行环境版本；当前数据字段 schema 1.0、软件 1.1.0 |
| `experiment_protocol_id` | 当前为 `earwise-3day-2round-4trial-v2`；区分旧版两 trial 协议，避免仅按 day/round 混用历史记录 |
| `dependency_versions` | 运行时 Tornado、Bleak 实际安装版本；完整开发环境见项目 requirements-lock.txt |
| `session_id/subject_id/day/round` | 本次身份，subject_id 为保留前导零的字符串，day 为 1–3，round 为 1–2 |
| `attempt_number/retry_of_session_id` | 第几次尝试及关联前次 session |
| `mode` | `real` 或 `simulation` |
| `session_status` | `recording/completed/aborted/interrupted/save_failed` |
| `started_at_utc/ended_at_utc/started_monotonic_ns/ended_monotonic_ns` | 起止 UTC 和进程单调时间，异常退出可能部分缺失 |
| `termination_reason/current_stage` | 终止原因与最后阶段 |
| `baseline_seconds/baseline_instruction` | 30 秒和“静息30s” |
| `trials` | 本轮四个 trial 的实际顺序（trial_order 1–4）、唯一 trial_id、condition、video、是否开始/完成/提交量表 |
| `trials[].video` | `video_id/path/filename/url/duration_seconds/sha256/codecs`；sha256 对原文件字节计算。`size_bytes/mtime_ns/ctime_ns` 记录文件状态，用于识别预检后的素材变化 |
| `questionnaires` | 本次程度/确信度题目及两端标签，选择值为整数 1–5 |
| `audio_confirmed` | 操作人已在本页面确认视频声音可听见、音量合适 |
| `target_name/service_uuid/notify_uuid/write_uuid/commands` | BLE 目标及候选或已核对命令，保留大小写 |
| `protocol` | 帧长度、字节边界、序号、原始通道偏移、位序和 CRC 验证状态 |
| `nominal_sample_rate/validations` | 名义采样率及命令、采样率、饱和定义验证标志 |
| `channel_mapping` | channel_0/1 对应左右及 verified；未确认值 null |
| `adc_bits/candidate_adc_gain/uv_scale/uv_unit/uv_verified` | ADC 位数、候选增益、候选换算及验证状态；候选不等于实测 |
| `window_seconds/saturation_ratio/sequence_max_delta/sequence_trust_seconds/algorithm_versions` | 质量窗口、阈值与连续性判据 |
| `data_timeout_seconds/page_heartbeat_seconds/video_timeout_seconds` | 数据、页面、视频中断门槛 |
| `queue_max_items/queue_max_age_seconds/quality_refresh_seconds` | 有界写入队列和质量记录周期 |
| `clock_sync/timing` | RTT 中点校时锚点、误差相关信息、区间规则和软件时标限制 |
| `verification_notes` | 未完成的硬件验证说明 |
| `files` | 正式结果三个文件名 |
| `summary` | 实际行数、事件数、接收/唯一/重复/推定缺帧/歧义计数、量表提交数等结束摘要 |

评分的唯一权威来源为 `labels.csv` 的 `RATING_SUBMITTED`，`trials` 只标记提交状态。当前协议正常完成时恰好有四条评分，逐一对应四个不同的 `trial_id`。分析前检查 `session_status`：`completed` 才是已通过软件保存完整性校验的正常全轮；其他状态保留的是未完成或保存异常记录。任何状态都不能替代人工实验质量判断。

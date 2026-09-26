# EarWise采集系统 · 数据字段字典（schema_version 2.0）

每个 session 表示一个被试、一个 round 的独立采集尝试，只有 `eeg_raw.csv`、`labels.csv`、`config.json` 三个正式结果文件。当前协议 `earwise-3round-4trial-randomstart-v4` 共 3 轮，每轮 4 个 trial（2 个 attention、2 个 relax）和 4 次量表，开头仅一次 30 秒基线。随机决定首个条件，之后交替，因此只有 attention → relax → attention → relax 和 relax → attention → relax → attention 两种顺序。三轮合计 3 次基线、12 个 trial（6 个 attention、6 个 relax）和 12 份量表。可按每天一轮安排实验，轮次不代表日历日期，也不要求连续三天采集。`mode=simulation` 的模拟记录位于 `simulation_data`；`mode=real` 的真实记录位于 `data`。两类数据不能合并当作同一实验来源。

软件版本为 `1.3.1`，新会话数据字段 `schema_version` 为 `2.0`；素材清单 manifest schema 为 `4.0`，输入配置 `config/settings.json` 的 schema 仍为 `1.0`。界面、API、素材清单、CSV 和 JSON 统一使用 `round=1–3`；新会话不含 `day` 字段。保存路径为 `data/sub_001/round_01/<session_id>/`（模拟时根目录为 `simulation_data`），所选轮次决定 `round_01/02/03`，不再有 `day_XX` 层。所有旧协议数据均不迁移、不覆盖，也不计入当前协议的既往尝试；只有遗留未完成会话会按原格式追加中断收尾，旧表头不变。分析时必须结合 `schema_version`、`experiment_protocol_id` 与 `trials` 区分新旧记录。

CSV 使用 UTF-8、固定表头、标准双引号转义和 `.` 小数点。不适用或未知值为空字段；JSON 对应值为 `null`。时间均含单位，UTC 字符串带时区。三文件的 `session_id`、`subject_id`、`round` 一致；两份 CSV 的前三列依次为 `session_id,subject_id,round`。

## eeg_raw.csv

每行是一个实际收到并成功解析的双通道 33 字节帧；不补样本、不补零成八通道，不用滤波或平滑值覆盖原始数据。

| 字段 | 类型 / 单位 | 含义 |
|---|---|---|
| `session_id` | 字符串 | 本轮尝试唯一标识 |
| `subject_id` | 字符串 | 被试编号，保留前导零及原字母大小写 |
| `round` | 整数 1–3 | 本次采集所属轮次，与目录、事件和 config.json 一致 |
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

同一次 BLE 通知可能解出多个帧，接收时刻可以相同。接收时间不等于设备采样时间。`channel_0/1` 始终代表物理原始通道，左右耳含义查本次 `config.json.channel_mapping`，不依赖界面卡片顺序。映射未确认时，两个映射值保持 `null`，仍正常保存两路原始数据，不推断左右耳。当前名义采样率由实验负责人确认为每通道 250 Hz；它不等于每秒 BLE 通知次数，也不表示软件已实测确认。μV 换算未验证时保持空字段，不用候选比例代替已确认的物理单位。

## labels.csv

统一追加事件表：阶段、播放、评分、质量、异常在同一文件。按接收顺序追加，但映射后的前端事件时间可能与文件顺序不同。

| 字段 | 类型 / 单位 | 含义 |
|---|---|---|
| `session_id` | 字符串 | 本轮尝试唯一标识 |
| `subject_id` | 字符串 | 被试编号，保留前导零及原字母大小写 |
| `round` | 整数 1–3 | 本次采集所属轮次；所有事件均明确标记轮次 |
| `event_id` | 字符串 | 唯一事件 ID |
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
| `schema_version/software_version/python_version/platform` | 三文件格式、软件和运行环境版本；当前数据字段 schema 2.0、软件 1.3.1 |
| `experiment_protocol_id` | 当前为 `earwise-3round-4trial-randomstart-v4`；区分所有旧协议，避免把旧 day/round 记录直接当作新 round 记录 |
| `dependency_versions` | 运行时 Tornado、Bleak 实际安装版本；完整开发环境见项目 requirements-lock.txt |
| `session_id/subject_id/round` | 本次身份，subject_id 为保留前导零的字符串，round 为 1–3；新会话没有 day 字段 |
| `plan_id` | 预检计划的 SHA-256 标识，绑定协议、被试、轮次及四个 trial 的实际计划与素材元数据；开始请求须携带并通过一致性检查 |
| `randomization` | 首条件分配依据，包含 `method/seed/first_condition/retry_policy`，详见下文 |
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
| `nominal_sample_rate/validations` | 实验负责人确认名义采样率为 250 Hz；命令、采样率实测、饱和定义的验证标志如实保存，未验证不阻止采集 |
| `channel_mapping` | channel_0/1 对应左右及 verified；未确认值 null、verified=false，不阻止保存原始数据 |
| `adc_bits/candidate_adc_gain/uv_scale/uv_unit/uv_verified` | ADC 位数、候选增益、候选换算及验证状态；候选不等于实测 |
| `window_seconds/saturation_ratio/sequence_max_delta/sequence_trust_seconds/algorithm_versions` | 质量窗口、阈值与连续性判据 |
| `data_timeout_seconds/page_heartbeat_seconds/video_timeout_seconds` | 数据、页面、视频中断门槛 |
| `queue_max_items/queue_max_age_seconds/quality_refresh_seconds` | 有界写入队列和质量记录周期 |
| `clock_sync/timing` | RTT 中点校时锚点、误差相关信息、区间规则和软件时标限制 |
| `verification_notes` | 未完成的硬件验证说明 |
| `files` | 正式结果三个文件名 |
| `summary` | 实际行数、事件数、接收/唯一/重复/推定缺帧/歧义计数、量表提交数等结束摘要 |

评分的唯一权威来源为 `labels.csv` 的 `RATING_SUBMITTED`，`trials` 只标记提交状态。当前协议正常完成时恰好有四条评分，逐一对应四个不同的 `trial_id`。分析前检查 `session_status`：`completed` 才是已通过软件保存完整性校验的正常全轮；其他状态保留的是未完成或保存异常记录。任何状态都不能替代人工实验质量判断。

左右耳映射、命令、采样率和 ADC 饱和定义的验证标志只说明参数核对状态，不再作为开始采集的门槛。手动发送小写 `b`、接收到有效数据或完成整轮采集，都不会自动把这些标志改成 `true`。`validations.commands_verified` 仍控制连接时是否自动发送配置命令；为 `false` 时可在联调区手动发送 `b` 启动数据流。`validations.sample_rate_verified=false` 表示尚未独立实测采样率，不否定负责人提供的 250 Hz 名义值。`validations.saturation_verified=false` 时饱和率仍按配置阈值估计，供质量提示使用；`uv_verified=false` 时 CSV 的两个 μV 字段仍为空。分析时应保留并使用本次快照中的这些状态，而不是将 `completed` 解释为所有硬件参数均已验证。

### randomization 与实际播放计划

| 字段 | 当前规则 |
|---|---|
| `method` | `sha256-subject-round-first-condition-v1`，按协议、被试和轮次确定可复现的伪随机首条件 |
| `seed` | 将 `[experiment_protocol_id, 规范化被试编号, round]` 以无多余空格、不转义中文的 JSON 序列化并编码为 UTF-8 后计算 SHA-256，保存完整 64 位十六进制摘要；被试编号先去首尾空格，再作 `casefold()`，保留前导零 |
| `first_condition` | `seed` 的最后一位十六进制数为偶数时取 `attention`，奇数时取 `relax`，后续三个 trial 交替 |
| `retry_policy` | `reuse-subject-round`，同协议、同被试、同一轮沿用首条件和顺序 |

同一被试同一轮在重复预检、刷新页面、重启服务或重采时不会重新抽取首条件；仅改变编号的字母大小写也不会改变分配。界面和会话保存的 `subject_id` 仍保留原字母大小写与前导零。不同被试或轮次可能得到不同结果，但不保证各条件人数严格各半。该方法不随机同条件内的视频顺序。

Round 1 使用 attention_01/02，Round 2 使用 attention_03/04，Round 3 使用 attention_05/06；relax_01/02 每轮各使用一次。每个条件中的两个视频按编号先后出现。素材清单 schema 4.0 的 `trial_order` 提供条件内素材顺序，`config.json.trials[].trial_order` 则是按首条件编排后的实际呈现顺序 1–4；分析和事件关联应使用会话快照的实际顺序。

`plan_id` 不等于 `seed`：前者绑定规范化被试编号、轮次、协议及完整的四 trial 计划（包含素材元数据），后者只决定首条件。预检返回 `plan_id`，开始采集时重新生成计划并核对该标识，防止正式执行的顺序或素材与已检查计划不一致。更换素材后应重新预检；既有 session 的快照保持原样。

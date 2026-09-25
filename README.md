# EarWise采集系统

本项目实现中文本地采集页面、耳机蓝牙预览、30 秒基线、每轮四个视频 trial、四次主观量表以及每次尝试独立的三文件保存。实验共 3 个 round，每轮包含 2 个 attention 和 2 个 relax trial，随机决定首个条件，之后交替。可按每天一轮安排实验，系统按 Round 1–3 标识进度，不要求固定或连续的日历日期。全部实验合计 3 次基线、12 个 trial（6 个 attention、6 个 relax）和 12 份量表。基线引导已按实验负责人确认设置为 **“静息30s”**。

当前软件版本为 **1.3.0**，实验协议为 `earwise-3round-4trial-randomstart-v4`。此版本按实验负责人最新要求，将原来的天数统一改为轮次：界面、请求参数、素材清单、保存目录、CSV 标签及会话快照都使用 `round=1–3`，新记录不再含 `day` 字段。原始需求文档保留为历史来源；轮次数、trial 数和条件顺序以本说明及当前配置为准。

左右耳对应关系尚未确认。当前正式模式会显示通道 0/1，并阻止开始正式采集；模拟模式可用于操作练习和软件验收。软件不通过 EEG 推算专心评分，评分全部由被试选择。

## 安装与启动

首次使用时，在 Windows 上克隆仓库并进入项目目录：

```powershell
git clone https://github.com/yuuuhhhh/EarWise--.git
Set-Location -LiteralPath '.\EarWise--'
```

也可以在 GitHub 下载 ZIP，解压后进入包含 `setup.bat` 的项目目录。双击 **`setup.bat`**，脚本会检测或安装 Python 3.12、创建项目独立的 `.venv`，并安装固定版本的项目依赖；首次配置需要联网。已有可用环境会复用，已有但损坏的 `.venv` 不会被自动删除或覆盖。

一键配置面向 **Windows 10/11 64 位（x64）**。没有可用 Python 3.12 时，从 [Python 官方网站](https://www.python.org/downloads/release/python-31210/) 下载 3.12.10 安装程序，验证发布者数字签名后安装到当前用户目录，不要求管理员权限、不修改全局 PATH。安装位置为 `%LOCALAPPDATA%\Programs\EarWise\Python312`；安装日志保存在 `work/setup/`。已有 Python 3.12 x64 会直接复用。脚本按 `requirements-lock.txt` 安装完整固定依赖，并检查包兼容性、应用导入、配置和当前协议所需的全部 8 个视频；自检不会连接耳机或开始实验。

配置失败时窗口会保留具体错误。网络恢复后可以直接重跑；损坏或不兼容的 `.venv` 需要先自行重命名保留，再运行脚本创建新环境。高级用法：`powershell.exe -NoProfile -ExecutionPolicy Bypass -File .\setup.ps1 -PythonPath 'C:\路径\python.exe'` 指定 Python 3.12 x64；添加 `-CheckOnly` 可仅检查，不安装。

配置完成后双击：

- `start-simulation.bat`：模拟模式，数据只写入 `simulation_data/`，页面明显标注“模拟”。
- `start.bat`：真实耳机模式，正式数据写入 `data/`。连接失败不会自动切换为模拟数据。

浏览器访问 <http://127.0.0.1:8765>。再次双击同一模式的启动脚本时，系统会核对本项目正在运行的服务并重新打开它的网页，不另起采集服务；若原服务使用自定义端口，会打开原来的地址。若两个启动脚本的模式不同，会明确提示当前模式和地址，不会自动切换或中断已有服务。如浏览器未能自动打开，可复制启动窗口显示的地址手动访问。

关闭网页或重复启动的提示窗口不会停止原服务。需要完全退出或切换模拟/真实模式时，在原来运行采集服务的启动窗口按 `Ctrl+C`；进行中的会话会按中断收尾，再运行所需的启动脚本。每次只运行一个服务，不要同时运行原参考蓝牙脚本，以免抢占耳机。

也可以直接使用项目虚拟环境，不必激活环境、不必修改全局 PowerShell 执行策略：

```powershell
Set-Location -LiteralPath '.\EarWise--'
& .\.venv\Scripts\python.exe -m app.main --simulate
# 真机模式（默认模式）
& .\.venv\Scripts\python.exe -m app.main
```

端口占用时改为 `--port 8766`，再访问对应地址；启动脚本同样接受 `-Port 8766`。服务只绑定 `127.0.0.1`，不需要云端账号。启动脚本使用自身所在目录定位工程，因此可从其他工作目录调用。`.bat` 的执行策略参数只作用于该次 PowerShell 进程，不更改系统设置。

项目使用 Python 3.12，开发验收环境为 Python 3.12.5。直接依赖在 `requirements.txt` 固定为 `tornado==6.5.2`、`bleak==1.1.1`；完整版本见 `requirements-lock.txt`。虚拟环境不会随仓库分发，换一台电脑时重新运行 `setup.bat` 即可。启动脚本检查并使用项目自己的环境，不要求手动激活 `.venv`。

仓库当前版本包含协议所需的 8 个实验视频：`attention_video/01.mp4`–`06.mp4` 和 `relax_video/01.mp4`–`02.mp4`。不再使用的 attention_07–12、relax_03–04 已从当前版本的 Git 跟踪中移除；原开发目录中的文件保留并忽略，新克隆不需要这些素材。`data/`、`simulation_data/`、`.venv/` 和 `work/` 已从 Git 排除，采集结果只留在本机。根目录的 `bci-serve_get_data_origin_from_uuid_h21292_print_with_save.py` 是历史参考脚本，不是 EarWise 启动入口；它额外使用 NumPy，未纳入本系统运行依赖。原始需求文档作为设计来源保留，其中的历史目录不影响部署。

## 一次 round 的操作

1. 输入被试编号（`001` 会保留前导零），选择 Round 1–3。检查计划后，系统显示本轮四个视频的实际顺序、各自量表及当前协议下的既往尝试，可选任意合法 round 补采。
2. 完成素材预检、声音确认和设备连接，检查最近收到 EEG 及质量状态。浏览器会检查当前协议的全部 8 个素材；本轮还会计算视频 SHA-256，写入本次快照。
3. 点击“开始 30 秒基线采集”。此时建立唯一 session 并开启正式落盘；此前预览不写入正式文件。基线根据服务端单调时钟计满 30 秒，显示“静息30s”。
4. 等待本轮第一个视频完整播放；自动播放被浏览器阻止时，点击开始播放。视频按原速、原音轨播放，不提供跳过或拖动。
5. 每个视频后选择程度、确信度两个 1–5 整数并提交。系统按本轮计划交替播放下一个条件的视频；完成第四个视频后的第四次量表，才结束本轮。
6. 等待保存校验；只有显示“已完成，数据已保存”才算正常完成。结果页列出本次目录和三个文件。

每轮的条件顺序从 **attention → relax → attention → relax** 和 **relax → attention → relax → attention** 中确定一种，不允许连续相同条件。每个视频后均填写一次量表。每轮仅在开始时有一次 30 秒基线，第 2 与第 3 个 trial 之间没有额外基线。基线到视频的过渡、缓冲、量表期间继续保存 EEG。

首个条件采用按“协议版本、被试编号、轮次”确定的可复现伪随机分配；分配依据中的被试编号去除首尾空格并忽略字母大小写，保留前导零。同一被试同一轮在重复检查计划、刷新页面、重启服务或重新采集时，均沿用相同顺序；不会通过反复检查重新抽取。不同被试或不同轮次可能得到不同起始条件，但不保证各条件人数严格各半。预检返回 `plan_id`，开始请求必须带回相同计划标识；若计划或素材发生变化，需要重新检查。

当前协议下已有完成记录需要明确确认重采，每次尝试使用新目录，不覆盖旧数据。所有旧协议的数据（包括以 day 标识的版本）继续保留，但都不计入当前协议的完成记录或重采依据；分析时按 `experiment_protocol_id` 区分。

正式阶段保持控制页面打开。刷新、关闭控制页、页面心跳丢失、蓝牙断连、数据超时或持续播放错误会中断该次尝试；重新连接不会续写已结束会话。需要重采时重新开始一个 round，并重新执行 30 秒基线。明确“中止本轮”会记录 `aborted`；保存失败会记录 `save_failed`，不会显示正常完成。

## 正式实验前的硬件核对

修改 `config/settings.json` 后重启服务，现有会话快照不会随项目配置变化。只在实际核对相应参数后修改验证标志：

| 设置 | 当前值 | 启用前操作 |
|---|---|---|
| `channel_mapping` | 两路均为 `null`，`verified=false` | 查接线资料或实机核对，将 `channel_0/channel_1` 分别设为 `left/right`，对应无误后设 `verified=true` |
| `commands` | `["b"]`（小写） | 参考脚本候选原始模式命令；核对实际原始流启动行为后设置 `validations.commands_verified=true` |
| `nominal_sample_rate` | 250 Hz | 核对实际采样率和 BLE 批量到达行为后设置 `validations.sample_rate_verified=true` |
| `saturation_ratio` | 0.98 | 核对 ADC 削顶、特殊状态值以及阈值算法，随后设置 `validations.saturation_verified=true` |
| `uv_scale`、`uv_verified` | 0.0240405、false | 未核对增益和物理单位时保持 false，两个 μV 字段为空；不影响保存 ADC counts |
| `baseline_instruction` | 静息30s | 已由实验负责人确认；不擅自增加睁闭眼或注视点要求 |

四项正式门槛是左右映射、命令、采样率和饱和定义。μV 换算可保持未验证。连接真实耳机时，未经验证的候选命令不会自动发送。联调区可显式发送参考脚本中的 `S`、`b`、`R`、`I`；按钮含义和停止硬件数据流行为未获得实机验证，不能把某个字节猜成停止命令。退出时释放蓝牙连接。

必须用真实耳机分别完成一次 attention 起始和一次 relax 起始的四 trial 流程，检查原始两通道数据、事件、视频音量、问卷归属及最终三文件；本次软件开发不代表这些硬件验收已经通过。

## 配置与素材

`config/video_manifest.json` 使用 manifest schema `4.0`，包含 3 轮 × 4 个 trial，共 12 个 trial 的素材分配，仅使用 `round` 标识轮次，不含 `day`。每个 trial 恰好一个视频，每轮两个 attention 和两个 relax。attention 每轮依次使用两个新视频，relax_01–02 跨三轮复用。素材清单中的 `trial_order` 用于确定同条件内的视频顺序；实际的 1–4 播放顺序由本轮首个条件决定，以预检计划和会话快照为准。

| Round | attention 视频（按条件内顺序） | relax 视频（按条件内顺序） |
|---|---|---|
| 1 | attention_01、attention_02 | relax_01、relax_02 |
| 2 | attention_03、attention_04 | relax_01、relax_02 |
| 3 | attention_05、attention_06 | relax_01、relax_02 |

例如 Round 1 的两种可能顺序是 `attention_01 → relax_01 → attention_02 → relax_02` 或 `relax_01 → attention_01 → relax_02 → attention_02`。只随机首个条件，同条件内的视频顺序不再随机；每项之后都有对应量表。

同一轮重复出现的条件使用不同的 `trial_id`，量表依据该 ID 关联各自视频。启动和预检会校验完整映射，缺失或错配会报出具体文件，不以临时示例素材替代。新生成三文件的数据字段 schema 为 `2.0`，与素材清单 schema 分开记录；输入配置 `config/settings.json` 的 schema 仍为 `1.0`。

当前使用的 8 个原始视频均已读取 MP4 元数据；时长如下，播放不截断、不循环、不变速。`av01` 是 AV1，`avc1` 是 H.264；所有视频带 `mp4a` 音轨。实际可解码情况由运行浏览器的素材检查确认，元数据读出不等于完整播放验收。

| 视频 | 时长（秒） | 视频编码 |
|---|---:|---|
| attention_01 | 272.687 | AV1 |
| attention_02 | 294.367 | H.264 |
| attention_03 | 313.567 | AV1 |
| attention_04 | 355.107 | H.264 |
| attention_05 | 261.067 | AV1 |
| attention_06 | 213.824 | H.264 |
| relax_01 | 300.002 | H.264 |
| relax_02 | 300.004 | H.264 |

其他主要默认参数：最近 5 秒质量窗口；每秒质量快照；1 秒未收到有效数据则中断；页面心跳超时 3 秒；持续视频异常超时 5 秒；写入队列上限 20000 条、排队超过 10 秒报告保存失败。CSV 缓冲区每秒刷新，每 5 秒同步到磁盘，并在阶段边界与收尾时同步；意外断电可能丢失尚未同步的尾部数据。实际 BLE 批量到达可能影响这些工程初值，需要实机确认。

共享丢包率使用 8 位帧序号估计，左右通道共用。默认只在间隔小于 0.5 秒且前向序号差不大于 32 的可信段推算缺帧。序号正常回绕不会计丢包；重复帧不进入分母；乱序、复位、长时间无数据等歧义区间标记未知。8 位序号无法证明没有整圈丢失，质量值不能作为硬件级精确丢包测量。饱和率由未经滤波的 ADC counts 单独计算，未验证时明确按配置阈值估计；无数据和未知值不显示成 0%。

## 输出和时间含义

```text
data/ 或 simulation_data/
└─ sub_001/round_01/<session_id>/
   ├─ eeg_raw.csv
   ├─ labels.csv
   └─ config.json
```

目录按所选轮次使用 `round_01`、`round_02` 或 `round_03`，新目录中没有 `day_XX` 层。`config.json` 保存 `subject_id` 和实际 `round`（1–3），不再保存 `day`。`eeg_raw.csv` 与 `labels.csv` 的每行都在 `session_id` 后直接记录 `subject_id`、`round`，便于合并分析时保留被试与轮次身份。每个正常建立并收尾的 session 恰好三个文件。CSV 为 UTF-8，空值留空；`config.json` 包含实际生效的参数、随机分配依据 `randomization`、计划标识 `plan_id`、素材摘要和结束状态。磁盘故障时可能无法完成全部文件，应以错误提示和已保留内容为准。项目配置、虚拟环境、测试和程序诊断均不属于会话三文件，不能放入该目录。字段解释见 `DATA_DICTIONARY.md`。

`eeg_raw.csv` 保存主机接收事实，接收时刻不是设备物理采样时刻，连续行号不是无缺失生理样本编号。`labels.csv` 记录阶段边界、前端播放事件及四次评分；前端通过往返校时映射到后端单调时间，仍是软件时间标记，未测量屏幕物理呈现延迟。阶段区间按 `[开始,结束)` 解释，不能直接把前端 `performance.now()` 与后端时钟相减。

程序意外退出后，下次启动只把遗留 `recording` 会话恢复为 `interrupted`，保留已有数据，不把已终止状态改成完成。旧协议记录不会改名、迁移或补写新字段；仅对遗留未完成会话进行必要的中断收尾，并保持它原有的 CSV 表头及数据格式。突然断电仍可能丢失尚未持久化的尾部。

## 测试与排查

```powershell
Set-Location -LiteralPath '.\EarWise--'
& .\.venv\Scripts\python.exe -m unittest discover -s tests -v
```

测试使用临时目录和模拟帧，不接触真实耳机。全系统测试覆盖输入和素材、帧解码、质量统计、随机起始后的两种交替流程、计划一致性、写入故障及本地 HTTP/WebSocket 传输。实际结果和浏览器验证详见 [TEST_REPORT.md](TEST_REPORT.md)；模拟通过不能代替硬件验收。已安装的全部依赖版本另存于 `requirements-lock.txt`。

- **已连接但无 EEG：**检查耳机是否处于原始模式；先完成命令核对，不把蓝牙连接成功当作可采集。
- **AV1 视频预检失败：**检查浏览器能否解码相应原始视频。系统会禁止开始，避免采集中途跳过素材；不要直接覆盖原视频以规避问题。
- **开始按钮不可用：**按页面逐项处理硬件验证、完整素材预检、声音确认、近期数据和时钟同步提示。
- **保存失败：**保留失败目录，检查磁盘可写性和容量；修复后新建尝试，不修改旧结果伪装完成。
- **中途刷新或关闭页面：**该次会话按中断处理，返回页面查看状态，确认重采时建立新 session。

设备层使用 Bleak 通知与写特征 API，见 [Bleak Client 文档](https://bleak.readthedocs.io/en/stable/api/client.html)；实时页面使用 [Tornado WebSocket](https://www.tornadoweb.org/en/stable/websocket.html)。程序模块分别为 `configuration.py`（配置/素材）、`protocol.py`（帧解码）、`quality.py`（质量统计）、`device.py`（蓝牙/模拟设备）、`controller.py`（流程）、`recorder.py`（落盘）及 `web/`（页面）。

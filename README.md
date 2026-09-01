# 本地声音工坊

面向 Windows 10/11 的本地文字转语音、零样本声音克隆和 GPT-SoVITS V2ProPlus 微调桌面程序。界面、模型推理、转写和训练均在本机运行；除首次下载组件外，不向外部服务发送文字、音频或使用统计。

## 当前实现

- PySide6 原生中文工作台，包含 AI 翻唱、文字生成、我的声音、训练声音、设置与全局任务中心。
- AI 翻唱工作台已实现歌曲导入、项目内不可变源文件副本、SHA-256 校验、流式波形、LRC 歌词、歌曲权利声明、UVR5 人声/伴奏分离、五轨时间线、单轨试听、Mute/Solo、同步 Seek、取消、缓存与重开恢复。
- Singing Model 能力层已接入独立锁定的 RVC v2/RMVPE 运行时协议：授权 VoiceProfile 可训练并验证歌唱模型，已分离 Vocal 可生成带 `ai_generated` 标识的 AI Vocal 资产，模型、索引、输入与输出均记录 SHA-256 并支持缓存和取消。
- 导入音频和麦克风录音统一保存为 `SourceAsset`；SHA-256 去重、解码、声道、响度、削波和长静音检查，原始文件永不覆盖。
- 声音配置可在尚无参考转写时保存；状态、素材、零样本参考、数据集快照及 GPT/SoVITS 检查点持久化，并同步刷新三个页面。
- 中文/英文混合长文本安全分段、可恢复任务记录、WAV 分段和 WAV/320 kbps MP3 合并输出。
- “数据与训练”默认直接使用声音库素材，经单声道标准化、可选人声分离/降噪、VAD、ASR、人工校对后冻结不可变快照；录音仅用于补充。
- 每次数据准备使用独立 `preparation_id`，训练特征与冻结快照 ID/哈希强绑定；每次新训练使用独立运行、日志和检查点目录，不会静默恢复旧训练。
- 持久化 `TrainingWorkflow` 和 `DatasetDraft`；每个阶段原子落盘，重启后由用户点击“继续上次任务”，不会自动占用 GPU。
- 训练结果保存为 `ModelVersion`。候选模型通过真实加载及固定中文/中英混合 WAV 验证后自动启用，旧活动版本始终保留并可回退。
- 60 秒训练门槛只统计“已人工确认、文本非空、纳入训练且无质量问题”的切片，不统计原文件总时长。
- 真实临时试听调用 GPT-SoVITS 并用 QMediaPlayer 自动播放，只写入 `%LOCALAPPDATA%\LocalVoiceStudio\cache\preview` 的 WAV，不生成 MP3。
- stdin/stdout JSON Lines GPU 工作进程；`health`、`load_profile`、`synthesize`、`prepare_dataset`、`train`、`separate_song`、`cancel`、`shutdown` 命令。
- 固定 GPT-SoVITS 提交 `d523079fc05d9a8028d6085bffe4a2757c32abb6` 与 V2ProPlus 推理接口。
- 私有 Python 3.11、PyTorch 2.7.1+cu128、FFmpeg 和模型的一键安装/修复，不修改系统 PATH。
- SQLite 任务/设置索引；可迁移 `project.json` 和 `raw/processed/datasets/checkpoints/exports` 项目结构。

## 直接运行界面

当前电脑已有 Python 3.10 和 PySide6，可在 PowerShell 中运行：

```powershell
$env:PYTHONPATH = "src"
& "C:\Users\cruelworld\AppData\Local\Programs\Python\Python310\python.exe" -m local_voice_studio
```

打开“设置”，点击“安装/修复本地引擎”。默认从 ModelScope 下载，适合中国大陆网络；也可在 PowerShell 中选择其他来源：

```powershell
.\scripts\bootstrap_runtime.ps1 -DataRoot "$env:LOCALAPPDATA\LocalVoiceStudio" -Source HF
```

安装会下载数 GB 运行环境和模型。完成后检测报告必须同时显示：

- Torch `2.7.1+cu128`
- GPU `RTX 5070 Ti`
- 架构 `sm_120`
- 真实 CUDA 张量测试通过

旧的 Torch 2.6.0+cu124 即使显示 CUDA 可用也不能在该显卡上生成或训练。

## 使用流程

### AI 翻唱歌曲预处理

1. 在“AI 翻唱”导入本地 WAV、MP3 或 FLAC；程序复制源歌曲到 `covers/<cover_id>/source/`，原文件永不修改。
2. 可载入同名 LRC 或手动选择 LRC。波形按 FFmpeg 流式解码，最多保存 6000 个峰值。
3. 点击“分离人声 / 伴奏”，确认歌曲处理与使用权利声明后选择 UVR5。普通分离 stem 标记为 `separated`，不是 AI 翻唱成品。
4. 完成后可在原曲、原唱人声、伴奏三轨之间独立试听，使用 Mute/Solo 并同步 Seek；AI 人声和最终混音保持“未就绪”。
5. manifest、stem、歌词和波形均保存在当前项目，重开后自动恢复。源文件、模型和输出哈希一致时直接复用分离缓存。

当前尚未实现：最终自动混音、RoFormer、自动歌词、逐字歌词与 AI 翻唱成品导出。RVC 训练/推理仅在独立运行时及其必需模型资产完成安装、校验后可用；未就绪时不会生成假文件。

### 声音训练与文字生成

1. 在“一键训练”填写声音名称并确认授权，拖入一批音频或选择整个文件夹。
2. 点击“开始自动处理”；程序自动质检、去重、优化、标准化、切片并本地识别文字。
3. 页面默认只显示异常片段。修正后点击“确认并训练”，所有合格片段会一次性写入人工确认状态；不足 60 秒会直接提示还差多少秒。
4. 程序自动冻结快照、准备特征、开始全新训练并生成两条固定验证试听；验证成功后自动启用新版本，失败时保留旧版本。
5. 在“我的声音”可试听、追加素材、重新训练或回退旧版本；在“一键生成”输入文字即可同时得到 WAV 和 320 kbps MP3。

新版目录结构如下：

```text
processed/<profile_id>/runs/<preparation_id>/{normalized,separated,denoised,segments}
datasets/working/<profile_id>/<preparation_id>/{asr,preparation.json}
datasets/<snapshot_id>/{audio,dataset.list,manifest.json}
%LOCALAPPDATA%/LocalVoiceStudio/training/<profile_id>/<snapshot_sha256>/features
%LOCALAPPDATA%/LocalVoiceStudio/training/<profile_id>/<snapshot_sha256>/runs/<training_run_id>
checkpoints/<profile_id>/<training_run_id>
covers/<cover_id>/{manifest.json,source,stems,lyrics,waveform}
```

旧版 `project.json` 会无损迁移到项目 schema 4，并增加稳定 `project_uid`、训练步骤结果和持久化生成记录；冻结快照仍保持 schema 2 和原有哈希算法。旧绝对路径快照会把仍可找到的音频复制进快照 `audio` 目录并改写为相对路径，缺失文件会明确报错。缺失的授权记录不会被自动伪造。

> MP3 转成 WAV 不会恢复已经丢失的信息。带配乐、混响或他人声音的素材应先清理并逐段试听，训练数据优先使用无配乐的原始 WAV/FLAC。

## 测试与打包

```powershell
python -m pytest
& "C:\Users\cruelworld\AppData\Local\Programs\Python\Python310\python.exe" scripts\smoke_ui.py
.\scripts\build.ps1 -SkipInstaller
```

安装 Inno Setup 6 后运行 `.\scripts\build.ps1`，安装程序输出到 `dist\installer`。卸载只删除应用本体，默认保留 `%LOCALAPPDATA%\LocalVoiceStudio` 和“文档”中的项目、模型、录音及输出。

## 数据与安全

- 声音配置必须确认说话人本人或明确授权。
- 工作进程不监听端口，不提供局域网或公网 API。
- 原始音频不覆盖；声音素材复制到项目 `raw`，歌曲副本复制到 `covers/<cover_id>/source`。
- 未确认本人或授权使用的 `VoiceProfile` 在翻唱目标声音选择器中不可选。歌曲权利确认只是用户声明，VoiceStudio 不声称替用户取得歌曲、录音或发行授权。
- `content_origin` 仅使用 `original`、`separated`、`ai_generated`；第二阶段没有 AI 音频输出，UVR5 stem 不会被错标为 AI 生成。
- 公开源码仓库不包含真人声音；`参考声音/` 与常见私钥格式由预推送检查阻止提交。
- 运行时资产、依赖和模型按版本化 SHA-256 清单验证；模型仅允许 PyTorch 受限反序列化，不回退到 `weights_only=False`。
- 开发构建会明确标记为未签名；正式 Release 必须通过 Authenticode 签名校验，并生成 SHA256SUMS、CycloneDX SBOM 和构建证明。
- 同一时间只运行一个 GPU 任务；取消训练会终止完整子进程树。
- 当前不提供实时变声、账号、云同步或移动端。

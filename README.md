# 本地声音工坊

面向 Windows 10/11 的本地文字转语音、零样本声音克隆和 GPT-SoVITS V2ProPlus 微调桌面程序。界面、模型推理、转写和训练均在本机运行；除首次下载组件外，不向外部服务发送文字、音频或使用统计。

## 当前实现

- PySide6 原生中文界面：生成语音、声音库、数据与训练、任务历史、设置。
- 导入音频和麦克风录音统一保存为 `SourceAsset`；SHA-256 去重、解码、声道、响度、削波和长静音检查，原始文件永不覆盖。
- 声音配置可在尚无参考转写时保存；状态、素材、零样本参考、数据集快照及 GPT/SoVITS 检查点持久化，并同步刷新三个页面。
- 中文/英文混合长文本安全分段、可恢复任务记录、WAV 分段和 WAV/320 kbps MP3 合并输出。
- “数据与训练”默认直接使用声音库素材，经单声道标准化、可选人声分离/降噪、VAD、ASR、人工校对后冻结不可变快照；录音仅用于补充。
- 每次数据准备使用独立 `preparation_id`，训练特征与冻结快照 ID/哈希强绑定；每次新训练使用独立运行、日志和检查点目录，不会静默恢复旧训练。
- 候选 GPT/SoVITS 检查点及 A/B 状态写入项目；重启后可继续验收，只有用户接受后才成为默认模型。
- 60 秒训练门槛只统计“已人工确认、文本非空、纳入训练且无质量问题”的切片，不统计原文件总时长。
- 真实临时试听调用 GPT-SoVITS 并用 QMediaPlayer 自动播放，只写入 `%LOCALAPPDATA%\LocalVoiceStudio\cache\preview` 的 WAV，不生成 MP3。
- stdin/stdout JSON Lines GPU 工作进程；`health`、`load_profile`、`synthesize`、`prepare_dataset`、`train`、`cancel`、`shutdown` 命令。
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

1. 在“声音库”导入 `参考声音` 文件夹并保存声音配置；不需要先逐个填写长文件转写。
2. 在“数据与训练 → 使用声音库音频”选择配置和素材，点击“使用所选音频准备训练数据”。
3. 逐段试听、修正 ASR 文本并勾选“人工确认”，再冻结数据集快照。
4. 已确认且通过质检的切片达到 60 秒后，准备训练特征并开始训练；少于 10 分钟会提示继续补录，推荐 30–60 分钟。
5. 训练完成后生成零样本/微调后 A/B 样本，确认后才能设为默认检查点。
6. 在“生成语音”可先“生成试听”，满意后再正式输出 WAV 与 320 kbps MP3。

新版目录结构如下：

```text
processed/<profile_id>/runs/<preparation_id>/{normalized,separated,denoised,segments}
datasets/working/<profile_id>/<preparation_id>/{asr,preparation.json}
datasets/<snapshot_id>/{audio,dataset.list,manifest.json}
%LOCALAPPDATA%/LocalVoiceStudio/training/<profile_id>/<snapshot_sha256>/features
%LOCALAPPDATA%/LocalVoiceStudio/training/<profile_id>/<snapshot_sha256>/runs/<training_run_id>
checkpoints/<profile_id>/<training_run_id>
```

旧版 `project.json`、参考片段和遗留 `datasets/current/slice-input` 会在首次启动新版时迁移到 schema 2；旧绝对路径快照会把仍可找到的音频复制进快照 `audio` 目录并改写为相对路径，缺失文件会明确报错。缺失的授权记录不会被自动伪造。

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
- 原始音频不覆盖，导入时复制到项目 `raw` 目录。
- 同一时间只运行一个 GPU 任务；取消训练会终止完整子进程树。
- 第一版不提供实时变声、账号、云同步或移动端。

# 更新日志 / Changelog

本文件记录「本地声音工坊」（LocalVoiceStudio）每个发布版本的用户可见变更。
格式遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，版本号遵循[语义化版本](https://semver.org/lang/zh-CN/)。

## [1.0.0] - 2026-09-04

### 新增（相对 0.3.0）

- **AI 翻唱完整工作流（Phase 4）**
  - 歌曲导入 → 不可变源文件副本 + SHA-256 校验 → 流式波形与 LRC 歌词 → 歌曲权利声明
  - UVR5 人声/伴奏分离（可选离线 RoFormer 引擎），五轨时间线、实时多源试听、Mute/Solo、同步 Seek、取消
  - AI Vocal 生成（RVC v2），带音量/音色/混响/门限/均衡器后期处理、自动音准（Autotune）、RMVPE 音高建议、去混响预置
  - Quick Mixer：AI 人声 + 伴奏 + 可选原唱人声，48 kHz 立体声混音归一化、防削波、原子发布
  - 导出 WAV / 320 kbps MP3，绝不静默覆盖文件，同时生成 `.voicestudio.json` 侧车（AI 生成标识、权利声明、声音/模型、混音参数、输出 SHA-256）
  - 分离/人声清理/AI Vocal/混音/导出全流程取消支持与中断恢复

- **SenseVoice 自动歌词（Phase 5.2）**
  - 一键从歌曲提取带时间戳歌词（LRC），支持中/英/日等语言
  - 歌词手动编辑与工作流步骤指示器

- **产品可靠性（Phase 5.3）**
  - 分离/AI Vocal/混音/导出六态阶段生命周期（pending/running/completed/cancelled/failed/interrupted）
  - 翻唱工程清单原子保存 + 自动 `.bak` 备份，损坏时自动回滚
  - Worker 崩溃检测（FailedToStart 立即清空挂起请求）、歌词转写子进程可取消
  - CUDA 显存不足 / 磁盘空间不足错误自动翻译为可操作提示
  - 非 ASCII（中文）Windows 用户名路径可靠性

- **歌唱引擎（Phase 3.1）**
  - 独立锁定的 RVC v2 / RMVPE / HuBERT 运行时，隔离 Python 3.11 环境
  - 正式「一键训练」：SourceAsset 快照 → RVC 训练 → Index 校验 → 真实推理验证
  - 声音授权记录（本人/授权使用）与模型可信哈希登记

### 变更

- 全部功能在本地运行，不向外部服务发送文字、音频或使用统计（无遥测）
- 保持 Worker 单任务 JSONL IPC 与严格字段白名单，客户端不能指定受控输入或模型路径

### 修复

- RVC 子进程取消响应性、训练取消清理、生成音频伪影校验
- 导出探针可取消与音频契约收紧
- 翻唱工程清单损坏不再丢失工程（自动回滚备份）

### 说明

- 安装器不捆绑大模型；运行时组件（私有 Python / PyTorch / GPT-SoVITS / 分离模型）通过带 SHA-256 校验、断点续传与重试的引导脚本按清单下载
- 卸载时默认保留用户数据（项目、声音、录音与导出）；模型、项目与输出均位于安装目录之外

[1.0.0]: https://github.com/cuppur/VoiceStudio/releases/tag/v1.0.0

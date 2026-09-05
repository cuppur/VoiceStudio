# VoiceStudio v1 产品化实施计划

更新日期：2026-09-05

## 目标

在现有 Python + PySide6 桌面架构上，忠实实现 `VoiceStudio_Full_UI_Prototype_v4.html` 的页面结构、橙白视觉层级和交互语义，并将所有正式控件连接到真实的本地 AI 音频能力。HTML 仅作为设计基准，不作为运行时实现。

## 基线与 Git 策略

- 当前工作区原有修改已在 `C:\Users\cruelworld\Desktop\codex\VoiceStudio-baseline-20260905` 做了补丁和未跟踪文件备份。
- 不执行破坏性 reset/checkout，不覆盖用户项目、模型或训练数据。
- 先验证当前修改和测试，再将确认后的最新基线整理到 `main`；后续阶段只在 `main` 上通过 checkpoint commit 推进。
- 每个阶段完成后运行相关测试、真实启动和目标工作流，失败则先修复；若实际代码证明原计划不合理，更新本计划后继续。

## 阶段

### Phase 0：基线冻结

任务：确认未提交修改、运行现有测试、启动应用、检查 GPU/运行时/模型资产，建立可恢复 checkpoint。

依赖：无。可并行：只读环境和许可证检查。

验收：工作区边界明确，当前测试与启动证据记录，`main` 基线可恢复。

### Phase 1：统一领域模型

任务：统一 SongProject、VoiceProfile、Take、Asset、ModelVersion、CacheArtifact、ExportRecord 和 Job 语义；保持旧项目/Worker 兼容。

修改范围：现有 models/storage/cover/singing 数据层及迁移测试。

依赖：Phase 0。不可与共享模型迁移并行。

验收：旧工程可读，新工程可保存，多 Take 和 VoiceProfile 双能力可表达。

### Phase 2：Asset/Cache/SeparationEngine

任务：统一波形、歌词、分离、转换、后处理、试听、混音缓存；AI 翻唱和音频分离共用 SeparationEngine。

依赖：Phase 1。可并行：缓存测试、分离后端适配、manifest 校验。

验收：缓存命中/失效/重建正确，不重复分离，取消不发布半成品。

### Phase 3：GPU Job Scheduler

任务：统一 queued/running/cancelled/failed/recoverable/succeeded 状态，阶段断点、取消、恢复、显存检查和 Worker 事件。

依赖：Phase 1、2。主代理协调 Worker 协议和 GPU 生命周期。

验收：真实任务抽屉、取消穿透、失败恢复、重启恢复、无页面级模型生命周期。

### Phase 4：AI 翻唱真实 Pipeline

任务：Analyze → Waveform → Separation → Lyrics → Voice Conversion → Post Process → Mix → Export；支持局部试听、Take 和 A/B。

依赖：Phase 2、3。可并行：歌词 UI、音轨显示、导出 manifest 测试。

验收：一键真实生成，缺模型时真实禁用并解释，不使用假结果。

### Phase 5：PySide6 主 Shell 与 HTML v4 UI

任务：顶部导航、左歌曲库、中央多轨、右 Inspector、底部播放器、GPU 任务抽屉、橙白主题、波形/歌词/Take/A-B/简单高级模式。

依赖：Phase 1、3、4。可并行：只读视觉组件和 DPI 验证。

验收：1280×720、1440×900、1920×1080 及 100/125/150% DPI 布局稳定；所有控件真实有效。

### Phase 6：其他产品页面

任务：文字生成、我的声音、训练声音、音频分离，接入真实 GPT-SoVITS、RVC、质量检测、训练和 SeparationEngine。

依赖：Phase 1、2、3。可并行：页面 UI 与非共享测试。

验收：能力状态、依赖状态、任务、取消、失败、恢复均真实可见。

### Phase 7：导出、最近工程、设置

任务：导出历史、WAV/MP3、重新导出、输出目录、工程恢复、GPU/CUDA/模型/缓存/目录设置。

依赖：Phase 3、4、5、6。

验收：导出 manifest/SHA-256 正确，正常退出自动恢复，异常退出可选择恢复。

### Phase 8：启动器、诊断、打包和发布验收

任务：开发版 `.bat/.ps1`、诊断模式、日志、防重复启动、PyInstaller `VoiceStudio.exe`、运行资产安装/修复入口。

依赖：全部产品能力稳定后执行；启动器检测不偷偷下载大型组件。

验收：开发版一键启动、发布版双击运行、诊断报告完整、干净环境安装和端到端流程通过。

## 公共验收规则

每个阶段都必须：

1. 运行相关 unit/integration tests；
2. 执行 `git diff --check`；
3. 实际启动 PySide6；
4. 执行本阶段真实工作流；
5. 检查日志、退出码和产物；
6. 修复问题；
7. 建立 checkpoint commit。

## 端到端验收

启动 → 创建/打开工程 → 导入歌曲 → 选择 VoiceProfile → 自动分析/分离/歌词 → AI 翻唱 → 局部试听 → 新 Take → A/B → 混音 → WAV/MP3 导出 → 关闭 → 重启 → 恢复工程 → 播放历史结果，并验证文字生成、声音、训练、分离、导出、设置和 GPU 任务中心。

## 风险与调整原则

- 当前未提交修改可能混合了前序阶段产物，必须先确认再整理。
- 单 GPU 初期采用串行调度，优先稳定、取消和恢复。
- 任何模型/运行时不可用都必须走依赖门禁，禁止伪造成功。
- 如果现有代码或测试证明某个模型、缓存、Worker 或 UI 设计不合理，以实际证据为准修改本计划、实现和测试，不保留明显错误设计。

# VoiceStudio HTML 工作台 — 交付状态

更新：2026-09-08。界面已从 PySide6 原生控件切换为 QtWebEngine 渲染的 HTML v4 原型，经典界面与专用测试已移除。

## 运行

```powershell
$env:PYTHONPATH = "src"
& "C:\Users\cruelworld\AppData\Local\Programs\Python\Python310\python.exe" -m local_voice_studio
```

打包版：`dist\LocalVoiceStudio\LocalVoiceStudio.exe`（onedir，含 `_internal\local_voice_studio\ui\web\assets`）。
构建：`.\scripts\build.ps1 -SkipInstaller`（已实测通过，打包版 `--smoke-test` 退出码 0）。

## 架构

```
src/local_voice_studio/ui/web/
  assets/index.html        # 原型逐字节副本 + 两行 script
  assets/studio-bridge.js  # 连接 QWebChannel、渲染真实数据、设置页、离线音频播放
  assets/studio-pages.js   # 各页工作流（cover/tts/voices/training/separator/exports/engine）
  shell.py                 # WebStudioWindow：QWebEngineView + QWebChannel + WorkerClient
  bridge.py                # 协议层：动作路由、参数校验、事件分发
  data.py                  # 只读状态快照
  offline.py               # 离屏校验/测试用的离线 Worker 替身
  services/                # headless 业务服务（不 import QtWidgets）
    base.py cover.py tts.py voices.py training.py exports.py engine.py
```

业务规则只写一遍：服务层复用 `CoverApplicationService`、`JobCoordinator`、`TrainingWorkflowController`、`WorkerClient`、`StudioStore`；页面只负责展示与确认。

## 已接入的真实功能

| 页面 | 能力 |
| --- | --- |
| AI 翻唱 | 导入歌曲、歌曲权利声明确认、UVR5/RoFormer 分离、RMVPE 变调建议、人声清理、AI 人声生成（Pitch）、快速混音（AI/伴奏/原唱）、生成最终混音、导出 WAV/MP3/both + provenance sidecar、取消 |
| 文字生成 | 声音选择、文本与字数/时长预估、语速/停顿、两步 `load_profile`→`synthesize`、进度、取消、结果登记与历史、页面内试听 |
| 我的声音 | 卡片与详情、参考音频试听、重命名、归档、TTS/歌唱模型版本切换、导入模型（不静默启用） |
| 训练声音 | 素材导入（文件对话框 + 真实扫描/去重/质检）、素材列表、开始自动处理、数据准备进度、片段确认弹层（含异常片段）、确认并训练、草稿/中断恢复、歌唱模型训练（≥3 分钟门槛） |
| 音频分离 | 选择歌曲、UVR5/RoFormer 模式、权利确认、分离进度与结果 |
| 导出中心 | 真实扫描翻唱导出与生成记录、打开文件/文件夹、安全清理试听与波形缓存 |
| 设置 | 主题/密度/语言/开关持久化、工程/模型/缓存目录选择、引擎健康与完整性校验、一键安装/修复引擎（实时日志） |
| 任务中心 | 真实任务历史与进度、任务浮层取消、输出目录打开 |

事件协议：`job.started/progress/result/error/cancelling/cancelled`、`training.*`、`voices.changed`、`songs.changed`、`engine.install.*`。

## 原型有、后端没有（已隐藏）

隐藏方式统一为原型自带的 `.hidden` 类，不修改 HTML 结构：

- 翻唱页：效果预设（推荐/自然/高相似度/高稳定/强唱感）、RVC 高级参数（Index Rate / Protect / RMS Mix / F0 方法）、Take 版本条、A/B 同步对比、20 秒选区试听与局部渲染、"音色强度"、"细节保留"、"缺失时自动分离"、"自动音高校正"。
- 文字生成页：情绪按钮组（自然/温柔/活泼/沉稳/开心/低语）、音高滑块、输出开关卡片（自动响度标准化 / 保留生成记录）。
- 训练页：训练质量下拉（标准/快速/高质量）。
- 分离页："极致·多轨"模式（后端只有 UVR5 与 RoFormer）。
- 导出页：FLAC 与 M4A 格式选项（后端只输出 WAV/MP3）。

未接入的动作（如批量队列 `batch.enqueue`）统一返回「该功能尚未接入新界面」，不伪造成功。

## 验证证据

- **视觉**：`assets/index.html` 与原型除两行脚本外逐字符相同（测试断言）；Chromium 渲染两者像素 0 差异（1440×900 截图字节相同）。
- **布局**：`scripts/compare_webengine_layout.py` 在 Chromium 与 QtWebEngine 中测量八页 `getBoundingClientRect`：7 页逐值完全相同，cover 页仅 2 处 1px 子像素舍入。
- **交互**：`tests/helpers/*_probe.py` 在真实 QtWebEngine 中点击并断言 —— 导航切页、任务抽屉、高级参数、导入弹层、Take/预设切换、权利声明→分离方式→发送 `separate_song`、TTS 生成发送 `load_profile`、训练页发送 `prepare_dataset`。
- **服务层**：`tests/test_web_services.py` 覆盖校验、两步流程、事件、取消、错误、混音滑块→dB、训练门槛、模型导入不静默启用。
- **真实链路（本机）**：3:29 真实歌曲 → UVR5 分离成功（进度 5→100%，vocals/instrumental + SHA-256 落盘）；素材导入 418.8 秒（含分段）→ 数据准备完成 → 60 个片段、合格 312.6 秒 → 人工确认通过 → 冻结快照 → GPT-SoVITS 训练启动（GPU 显存约 2.4 GB）；打包版启动退出码 0。
- **回归**：全量 `pytest` 通过（exit 0）；应用 `--smoke-test` 退出码 0。

## 已知限制

1. **ASR 模型缓存（已修复）**：FunASR 按 ModelScope id 解析 `iic/SenseVoiceSmall` 与 `iic/speech_fsmn_vad_zh-cn-16k-common-pytorch`，而安装脚本把模型放在 `models/lyrics`，两者不通 → 每次运行都会联网补全并在慢网络下长时间阻塞（实测卡在 57 MB 的 `model.pt.incomplete`）。修复方式：`python scripts/link_asr_models.py` 把本地已装模型硬链接进 ModelScope 缓存，之后 SenseVoice 与 FSMN-VAD 在 **11 秒**内加载完成。换机或重装引擎后需再执行一次。
2. **真实训练/生成**：数据准备已在真机跑通到切片与识别；完整训练（GPT-SoVITS 微调）与随后由该声音驱动的 TTS 生成、AI 人声转换耗时较长，其验证结果见下方"真实链路"小节的最新记录。
3. Windows 上无法抓取 QtWebEngine 的实时画面（`grab()` 空、`grabWindow` 损坏、`PrintWindow` 只返回首次合成帧），像素证据来自同一 Chromium 内核的浏览器渲染，引擎侧用布局与计算样式断言。
4. 深色主题、界面密度等 Qt 侧外观偏好不再影响 HTML 壳（原型只有暖橙白一套配色）。

## 清理记录

已删除：经典 Qt 界面模块（main_window/pages/simple_pages/cover_page/html_pages/dashboard/widgets/preview/recording/export_rows/prototype_icons/studio_widgets/audio/resources）、其专用测试与脚本、旧日志、旧界面截图、`build/` 中间产物、`__pycache__`。
保留：`VoiceStudio_Full_UI_Prototype_v4.html`（视觉基线，测试引用）、`docs/screenshots/web-shell`、`docs/ui-baseline`、`diagnostics`、`release-metadata`。

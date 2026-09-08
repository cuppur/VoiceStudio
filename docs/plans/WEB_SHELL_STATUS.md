# WebEngine HTML 壳（复刻 VoiceStudio_Full_UI_Prototype_v4.html）

更新：2026-09-08。本文件记录实现、证据与未完成项，不是完成声明。

## 目标与决定

用户要求把桌面程序的界面做成 `VoiceStudio_Full_UI_Prototype_v4.html` 的样子，且“前端页面完全复刻”。
Qt Widgets + QSS 无法复刻 CSS grid、box-shadow、渐变等原型效果，因此采用：

- **QtWebEngine 直接渲染同一份原型 HTML/CSS**，视觉与原型 1:1；
- **QWebChannel 桥接**真实本地数据与操作，逐步接回重操作；
- 旧的 Qt 原生壳保留，`--ui-qt` 可回退。

## 结构

| 文件 | 作用 |
| --- | --- |
| `src/local_voice_studio/ui/web/assets/index.html` | 原型文件的逐字节副本，仅追加一行 `<script src="studio-bridge.js">` |
| `src/local_voice_studio/ui/web/assets/studio-bridge.js` | 前端适配层：连接桥、渲染真实数据、路由动作；浏览器打开时立即返回，原型演示行为不变 |
| `src/local_voice_studio/ui/web/shell.py` | `WebStudioWindow`：QWebEngineView + QWebChannel + WorkerClient |
| `src/local_voice_studio/ui/web/bridge.py` | `StudioBridge`：`bootstrap()` / `invoke(action, payload)` / `event(name, payload)` |
| `src/local_voice_studio/ui/web/data.py` | `StudioSnapshot`：只读真实状态快照（项目、歌曲、声音、任务、导出、设置、引擎、存储） |
| `tests/test_web_shell.py` | 壳与桥接的合约测试 |
| `scripts/capture_web_baseline.py` | 用 Chromium 截取八页视觉基线 |
| `scripts/capture_web_visual.py` | 用 QtWebEngine 加载壳（静态/实时）用于诊断 |

## 桥接协议

- `bootstrap() -> str`：返回 `{"ok":true,"data":{app,project,projects,songs,voices,tasks,exports,generations,engine,storage,settings}}`。
- `invoke(action, payload_json) -> str`：返回 `{"ok":bool,"message":str,"data":{...}}`。
- `event(name, payload_json)`：Python → 页面推送（`project.changed`、`songs.changed`、`worker.state`、`worker.ready`、`worker.event`）。

已接入的动作：`app.refresh`、`project.activate/create/open/reveal`、`song.import/select`、`file.reveal/open`、`settings.set/save/pick_directory`。
未接入的动作一律返回明确文案「该功能尚未接入新界面（可用 --ui-qt 打开经典界面）」，前端也会以同样文案提示，不伪造成功。

### 路径边界（独立复核后加固）

页面传入的路径不再直接使用：

- `project.activate/open/reveal` 必须落在 `paths.projects_root` 内（`ensure_within`），仅凭目录里存在 `project.json` 不足以被接受；
- `file.reveal/open` 必须落在 `projects_root`、`models_root`、`cache_root`、`runtime_root` 或 `engine_root` 之内，否则返回「路径不在 VoiceStudio 管理的目录内」；`data_root` 本身不在允许列表内，因此页面无法通过桥打开本机数据库文件；
- `song.import` 只在**页面直传路径**时限制在上述目录内；用户通过原生文件对话框选择时仍可导入任意位置的音频（明确的用户手势）；
- `settings.set/save` 只接受 `ui.*`、`generation.*`、`smart_optimization`、`default_output_dir`；`paths.*` 一律拒绝，根目录重定向只能走 `settings.pick_directory`（原生目录选择器）；
- 越界写入、越界打开、越界导入与设置键注入均有对应测试（`tests/test_web_shell.py`，15 项）。

### 性能

`bootstrap()` 首次实现会调用 `verify_install_manifest()`（对已安装的每个文件做 SHA-256），在真实数据目录上实测 **5.94 秒**，界面会白屏数秒。现已拆分为：

- `bootstrap()` 只做轻量检查（私有解释器、FFmpeg、清单文件是否存在），实测 **0.005 秒**；
- 完整哈希校验改为 `engine.verify` 动作，在 `QThreadPool` 线程执行，完成后通过 `engine.verified` 事件回填设置页，不阻塞界面。

## 视觉证据

- `assets/index.html` 与原型文件除追加的一行脚本外**逐字符相同**，由 `tests/test_web_shell.py::test_shell_assets_are_a_verbatim_prototype_copy` 断言。
- Chromium 渲染 `assets/index.html` 与渲染原型文件**逐像素相同**：1440×900 截图均为 241501 字节，像素差异 `0/1296000`。
- QtWebEngine 内读取的计算样式与原型 CSS 一致：`.topbar` 78px、`.transport` 68px、`.cover-layout` 三列、`.card` 圆角 17px、`body` 背景 `rgb(246,243,239)`、`.hero-title` 22px/820。
- WebEngine 视口为 1440×900，页面内容宽 1404（1440 − 2×18px 内边距），歌曲库列宽 242px —— 与原型三栏 `242px / 1fr / 320px` 完全一致。
- 真实数据渲染后同一断言集仍成立（见 `test_shell_renders_prototype_pages_with_real_data`）。
- **八页布局逐值对照**：`scripts/compare_webengine_layout.py` 在 Chromium 与 QtWebEngine 中分别测量八页的 `getBoundingClientRect`（视口、顶栏、播放器、页面布局容器、卡片数量与每张卡片的位置尺寸）。结果：**7 页逐值完全相同**；cover 页右栏两张卡片存在 2 处 **1px 子像素舍入**差异（两张卡片总高相同，只是余数分配不同）。真实数据接入后布局结构不变，唯一差异来自真实列表行数少于原型演示数据。
- **交互复刻**：同一探针在页面内实际点击并断言原型自身的交互仍然生效 —— 导航切页（tts 显示 / cover 隐藏、再切回）、GPU 任务抽屉开合、高级参数展开、导入弹层开合、Take 版本切换、效果预设切换。
- 八页基线图：`docs/screenshots/web-shell/*-1440x900.png`（Chromium 生成，可人眼验收）。

**限制**：Windows 上无法可靠抓取 QtWebEngine 的合成画面（`QWebEngineView.grab()` 只得到背景，`QScreen.grabWindow` 得到损坏画面）。因此 WebEngine 侧的证据是计算样式/几何断言，像素证据来自同一 Chromium 内核的浏览器渲染。真实 Windows 逐按钮点击与 100%/125%/150% 缩放仍未验证。

**WebEngine 真实渲染的像素对比**：

- `scripts/capture_web_visual.py`（默认走 `printToPdf` + PyMuPDF）可以拿到 WebEngine 自己渲染的图像。与浏览器基线做 12×6 网格平均色差，最大 21/255、绝大多数单元低于 5；逐像素严格相等不成立（打印光栅化与屏幕渲染的抗锯齿差异，集中在文字边缘与 range 滑块）。
- `scripts/capture_webengine_printwindow.py` 用 Win32 `PrintWindow(PW_RENDERFULLCONTENT)` 抓取真实窗口。但 Chromium 使用独立视觉树，窗口 DC 只在首次合成时更新，因此该抓取只能得到**第一次合成的帧**（切页后仍返回首页画面），不能用于逐页像素对比。
- 因此逐页一致性由上面的**布局逐值对照**与计算样式断言证明，而不是逐页像素相等。

**测试隔离**：QtWebEngine 不能在 pytest 进程内运行（其子进程会让解释器在会话结束后不退出）。渲染断言由 `tests/helpers/web_shell_probe.py` 在独立子进程中完成并 `os._exit(0)`，`tests/test_web_shell.py` 解析其 `VS_PROBE` 输出。

**已知行为差异**：Web 壳不读取 `ui.theme`（深色工作室外观），HTML 原型本身只有暖橙白一套配色；经典壳（`--ui-qt`）仍按既有偏好应用明暗主题。`--ui-preview` 不再初始化 QtWebEngine。

## 运行方式

```powershell
$env:PYTHONPATH = "src"
& "C:\Users\cruelworld\AppData\Local\Programs\Python\Python310\python.exe" -m local_voice_studio
```

- 默认启动 HTML 壳（QtWebEngine）。
- `--ui-qt` 启动经典 Qt 原生壳。
- `--ui-preview` 仍为隔离视觉预览（原生控件）。

## 尚未接入新界面的功能

分离、AI 翻唱渲染、文字生成、训练启动、任务取消、批量队列、真实波形/歌词计算、模型版本切换。
这些操作在经典壳（`--ui-qt`）中仍然可用，且在新界面中点击时会得到明确提示而不是静默失败。

## 打包

`LocalVoiceStudio.spec` 与 `scripts/build.ps1` 已加入 `src/local_voice_studio/ui/web/assets` 的 `add-data`（并补齐了 `ui/resources/prototype`，此前只有 spec 有）。打包后的实际运行未在本轮验证。

注意：仓库中的 `dist/` 和根目录的 `VoiceStudio-一键启动.exe` 都是本次改动之前的旧构建，**不含 HTML 壳资源**；需要重新执行 `scripts/build.ps1 -SkipInstaller`（或完整 `scripts/build.ps1`）才会包含新界面。源码方式运行不受影响。

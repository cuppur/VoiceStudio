# v1.0.0 发布检查清单 / Release Checklist

本清单汇总 v1.0.0 发布前需要人工在真实环境验证的验收项（自动化无法覆盖的部分），
以及已在自动化中覆盖、仅需抽查的项。每项标注验证方式。

## 1. 构建与安装（Phase 6.0）

| 项 | 方式 | 状态 |
|----|------|------|
| 版本统一 1.0.0（pyproject / `__version__` / 安装器） | `test_core.py::test_package_version_matches_project_version` 自动化 | ✅ 自动化 |
| onedir 打包 + EXE smoke | CI `package-smoke` | ✅ 自动化 |
| 安装器编译（Inno Setup 6） | 本地 `scripts/build.ps1` 完整构建（PS 5.1 兼容）→ `LocalVoiceStudio-Setup-1.0.0.exe`（57.6 MB）+ 一键启动（6.2 MB） | ✅ 已本地验证 |
| 安装 / 修复 / 卸载（Inno 默认 Repair 流程） | 真机双击 Setup，走 Install → 再次运行走 Repair/Uninstall | 人工 |
| 卸载默认保留用户数据 | 卸载后检查 `%LOCALAPPDATA%\LocalVoiceStudio` 仍在 | 人工 |
| 卸载可选删除全部数据 | 卸载时选「是」，确认项目/声音/录音/导出被删除 | 人工 |
| 安装器不捆绑大模型 | 安装后 `%LOCALAPPDATA%\Programs\LocalVoiceStudio` 大小（应远小于模型体积）；模型由运行时引导下载 | 人工 |
| 运行时下载 SHA256 + 断点续传 + 重试 | `scripts/bootstrap_runtime.ps1`（`curl -C - --retry 5` + `Get-FileHash` 校验） | 代码审查 + 真机断网重试 |
| Authenticode 签名（EXE + Setup + 卸载器） | `scripts/sign_release.ps1` / `build.ps1 -Release -CertificateThumbprint` | 发布时执行 |
| 发布文件：Setup exe + SHA256SUMS + licenses + README + CHANGELOG | `scripts/create_release_metadata.ps1`（生成 `SHA256SUMS.txt` + `sbom.cdx.json` + `sbom.spdx`） | ✅ 已对真实安装包验证 |
| SBOM（CycloneDX JSON + SPDX 2.3 tag-value） | 同上；需先安装 `pip install "cyclonedx-bom>=5,<7" lib4sbom`（cyclonedx-py 无 SPDX 输出，脚本用 lib4sbom 转换） | ✅ 已对真实安装包验证 |
| 升级测试（旧版 0.x → 1.0.0） | 真机先装旧版再装新版，确认项目/数据保留 | 人工 |

## 2. 兼容矩阵（Phase 6.1）

| 项 | 方式 | 状态 |
|----|------|------|
| Windows 10/11 x64 | 真机矩阵 | 人工（发布前抽查至少 Win10 + Win11 各一台） |
| NVIDIA RTX 30/40/50 系列（开发机 RTX 5070 Ti） | 真机 `设置 → 重新检测`，确认 CUDA 张量测试通过 | 人工 |
| CPU 回退（无 NVIDIA GPU / 驱动异常） | 真机禁用 GPU 或设 `force_cpu`，确认可运行且提示明确 | 人工 |
| 驱动检测（CUDA 不可用提示） | `engine.py gpu_health()` 已给出中文可操作提示 | 代码审查 + 抽查 |
| DPI 100 / 125 / 150 % | 真机切换缩放后检查布局无裁切（高 DPI 标志 + PassThrough 已启用） | 人工 |
| 分辨率 1280×720 – 2560×1440 | 真机切换分辨率检查（主窗口 `setMinimumSize(1280, 720)`，默认 1440×900） | 人工（已有 1280×720 / 1440×900 截图基线） |
| 低显存（8 GB） | 真机 8 GB 卡跑分离/推理，确认可完成或给出显存不足提示 | 人工 |

## 3. 安全与合规（Phase 6.2）

| 项 | 方式 | 状态 |
|----|------|------|
| 声音授权门禁（本人/授权使用） | `consent_confirmed` + 授权记录校验 | ✅ 自动化（`test_phase31_ui`、pipeline） |
| 歌曲权利声明（处理与使用权利） | `CoverProject.attest_rights` + 文本哈希 | ✅ 自动化 |
| 导出发布权利确认 | `publication_rights_acknowledged` 强制 | ✅ 自动化 |
| AI 内容标签 + 侧车 | `.voicestudio.json`（AI 生成标识、模型、哈希） | ✅ 自动化（`test_phase411_*`） |
| 导出默认命名 `歌曲名_AI_VoiceStudio` | `ExportDialog` 默认文件名 | ✅ 自动化（E2E） |
| `torch.load weights_only=True` | 产品路径全用受限加载；`rvc_bridge.py` 唯一 `weights_only=False` 仅限受控训练 staging 且带注释 | ✅ 代码审查 |
| 路径穿越防护 `ensure_within` | worker 所有路径入口 | ✅ 自动化 |
| Worker IPC 正向白名单 + 字段白名单 | `protocol.COMMANDS` / `PAYLOAD_FIELDS` | ✅ 自动化 |
| 默认无遥测 | 无网络上报代码路径（仅引擎/模型下载） | ✅ 代码审查 |

## 4. 端到端与可靠性（Phase 5.3 / 6.3）

| 项 | 方式 | 状态 |
|----|------|------|
| E2E 流程 A（导入→分离→AI Vocal→混音→导出） | `test_phase63_e2e.py::test_e2e_cover_full_flow_a` | ✅ 自动化 |
| E2E 流程 B（取消 → `cancelled` 状态持久化） | `test_e2e_cover_cancel_marks_stage_cancelled` | ✅ 自动化 |
| 压力测试（完整流程 ×5） | `test_e2e_cover_stress_five_runs` | ✅ 自动化 |
| 崩溃恢复（六态 + 中断恢复 + 清单损坏回滚） | `test_phase53_reliability.py`（7 项） | ✅ 自动化 |
| Worker 崩溃检测（FailedToStart 清空挂起） | `test_phase31_ui.py` | ✅ 自动化 |
| 重启/崩溃注入（真机） | 真机分离中途杀进程 → 重启 → 提示中断并可重试 | 人工 |
| 真实 GPU 全流程 E2E（非 mock） | 真机按 A/B/C/D 各跑一遍（需引擎与模型就绪） | 人工（发布前） |

## 5. UI 质量（Phase 6.3）

| 项 | 方式 | 状态 |
|----|------|------|
| 无障碍名称（主导航、设置、cover 主操作） | `test_phase31_ui.py::test_key_controls_expose_accessible_names` | ✅ 自动化 |
| UI 测试覆盖率 ≥ 90% | 当前 `--cov=local_voice_studio.ui` 实测 **53%** | ⚠️ **P2 延后**（任务卡约定 P2 可延后但须记录；核心状态机/契约已由 E2E 覆盖，剩余为控件渲染分支） |
| 中文/英文错误提示 | 全站中文提示；`_friendly_error` 兜底 | ✅ 自动化（部分） |

## 6. 发布产物顺序（v1.0.0）

1. 真机跑完本清单 1–4 的人工项，全部通过。
2. `scripts/build.ps1 -Release -CertificateThumbprint <SHA1>`（需本机 Inno Setup + SignTool + 签名证书）。
3. `scripts/create_release_metadata.ps1 -Artifact dist\installer\LocalVoiceStudio-Setup-1.0.0.exe` 生成 SHA256SUMS + SBOM。
4. 上传发布文件：Setup exe、SHA256SUMS.txt、sbom.cdx.json、sbom.spdx、THIRD_PARTY_NOTICES.md、README.md、CHANGELOG.md。
5. 打 tag `v1.0.0` 并发布 GitHub Release。

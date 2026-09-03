# 第三方组件声明

本地声音工坊本身采用 MIT 许可证。应用按需安装或调用以下第三方组件：

- GPT-SoVITS，MIT License，固定源码版本 `d523079fc05d9a8028d6085bffe4a2757c32abb6`。
- PyTorch / torchaudio，BSD-style license。
- PySide6 / Qt，LGPLv3/GPLv3/commercial 多重许可；本项目使用动态链接的 LGPL 组件。
- FFmpeg，具体许可取决于 conda-forge 构建；安装清单应随最终发布包一并保留。
- Miniforge/Conda，BSD-3-Clause 及各包自身许可证。
- MelBand RoFormer ONNX 模型，MIT License；转换版本固定为 `60cb6b4b97e41b42f7ff16c2e386f47a8cc7e50a`，原始模型版本固定为 `ac9b0614ab3cd7f77219e18ba494dfd93956c348`。

模型、ASR、降噪和伴奏分离权重应从其官方来源下载，并保留下载包附带的许可证文件。声音素材只能在说话人本人或明确授权的范围内使用。

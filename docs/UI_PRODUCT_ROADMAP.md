# VoiceStudio 产品开发路线

此文档由 UI 原型中的成熟度评估移出，不作为用户功能页面。

- P0：源素材与转换输出质检、真峰值保护；单 GPU 任务调度、阶段输出保存、取消、失败重试与重启续跑。
- P1：局部重做与接缝处理、音高与时间精修、EQ/齿音/压缩/混响、响度一致 A/B；分轨交付、参数清单、防覆盖与备份。
- P2：一首歌多声音批量生产、缓存复用与优先级。

验收须真实处理长音、高音、齿音、静音、重伴奏素材；原型中的状态、测量与音频处理仍为演示。

参考：[RVC CLI](https://github.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/blob/main/docs/en/cli.md)、[UVR WebUI](https://github.com/varaslaw/ultimatevocalremover-webui)、[Applio 模型安装](https://docs.applio.org/getting-started/installing-inference-models/)。

# Third-Party Notices

VoiceStudio itself is licensed under the MIT License in [`LICENSE`](LICENSE). This notice covers third-party runtime components identified from the current manifest, lock files, bootstrap script, and runtime imports. `NOT VERIFIED` is a release blocker. Source-code licenses are never used as a substitute for model-weight licenses.

## Verified source and runtime components

| Component | Version / commit | License | Source | Use |
|---|---|---|---|---|
| GPT-SoVITS source | `d523079fc05d9a8028d6085bffe4a2757c32abb6` | MIT | [pinned LICENSE](https://raw.githubusercontent.com/RVC-Boss/GPT-SoVITS/d523079fc05d9a8028d6085bffe4a2757c32abb6/LICENSE) | Isolated TTS/training engine |
| RVC WebUI source | `8f2fdbf483955f924b4c87ab34919170d0b704ed` | MIT | [pinned LICENSE](https://raw.githubusercontent.com/RVC-Project/Retrieval-based-Voice-Conversion-WebUI/8f2fdbf483955f924b4c87ab34919170d0b704ed/LICENSE) | Isolated RVC v2 engine |
| PyTorch | `2.7.1+cu128` | BSD-3-Clause | [pinned LICENSE](https://raw.githubusercontent.com/pytorch/pytorch/v2.7.1/LICENSE) | Tensor runtime |
| torchaudio | `2.7.1+cu128` | BSD-2-Clause | [pinned LICENSE](https://raw.githubusercontent.com/pytorch/audio/v2.7.1/LICENSE) | Torch audio operators |
| FunASR source | `1.4.0` | MIT | [FunASR LICENSE](https://raw.githubusercontent.com/modelscope/FunASR/main/LICENSE) | Local ASR bridge |
| SenseVoiceSmall model family | pinned ModelScope revisions: model/config/tokenizer/`am.mvn` `70514a3da51f1160f51d18449dab6128bbd4928b`; tokens `43d0ed61231c41f8393fa347b838a1f6e2d264f6` | Apache-2.0 (official ModelScope model card) | [official model card](https://www.modelscope.cn/models/iic/SenseVoiceSmall) | Local lyric ASR |
| FSMN-VAD model family | pinned ModelScope revisions: model/configuration `662fc7a38813d81305085696d59eb5b1141a204a`; config `25f6f166b917e3062ab09c9f7c9fce5e3f708360`; `am.mvn` `f2001ab650c1a6a6f14beb4b26288d0f57529278` | Apache-2.0 (official ModelScope model card) | [official model card](https://www.modelscope.cn/models/iic/speech_fsmn_vad_zh-cn-16k-common-pytorch) | VAD segmentation |
| ONNX Runtime / GPU | `1.28.0` | MIT | [pinned LICENSE](https://raw.githubusercontent.com/microsoft/onnxruntime/v1.28.0/LICENSE) | RoFormer ONNX inference |
| Transformers | `4.50.0` | Apache-2.0 | [pinned LICENSE](https://raw.githubusercontent.com/huggingface/transformers/v4.50.0/LICENSE) | Model/tokenizer support |
| librosa | `0.10.2` | ISC | [pinned LICENSE](https://raw.githubusercontent.com/librosa/librosa/0.10.2/LICENSE.md) | Audio analysis |
| soundfile | `0.14.0` | BSD-3-Clause | [pinned LICENSE](https://raw.githubusercontent.com/bastibe/python-soundfile/0.14.0/LICENSE) | WAV I/O |
| NumPy | `1.26.4` | BSD-3-Clause | [pinned LICENSE](https://raw.githubusercontent.com/numpy/numpy/v1.26.4/LICENSE.txt) | Numerical arrays |
| SciPy | `1.17.1` | BSD-3-Clause | [pinned LICENSE](https://raw.githubusercontent.com/scipy/scipy/v1.17.1/LICENSE.txt) | Resampling/signal processing |
| Qt for Python / PySide6 | `6.9.2` from `pyproject.toml`; local Python 3.10 import verified; PySide6, Essentials, Addons, and Shiboken6 distributions all `6.9.2` | Package metadata: `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only`; commercial terms are also documented by the bundled `LicenseRef-Qt-Commercial.txt` | [Qt licensing](https://doc.qt.io/qt-6/licensing.html); local metadata under `C:\Users\cruelworld\AppData\Local\Programs\Python\Python310\Lib\site-packages\` | Desktop UI, multimedia, and Qt plugins |
| Miniforge installer | `26.3.2-3`, SHA-256 `14a8635465b5190537ddad6286746ffebbc55a1ed2a7bb14a506595fe3191e1e` | BSD-3-Clause for installer; bundled packages retain their own licenses | [pinned LICENSE](https://raw.githubusercontent.com/conda-forge/miniforge/26.3.2-3/LICENSE) | Private Windows Python bootstrap |

## Binaries, engines, and model weights

| Component / asset | Version / revision | License | Source | Use |
|---|---|---|---|---|
| FFmpeg / ffprobe | Local binaries `8.1.2-full_build-www.gyan.dev`, SHA-256 `ad8f211bc894755e0061c55ab280ae00e8d3d4f15a8cc4372b24cfa247b5942e` / `9df3b0b5275e830961df6d94e1f7a71121a7abd5ff708e9fec8a0b6084a55015`; `--enable-gpl --enable-version3 --enable-static` and many GPL codec flags verified with `-buildconf` | GPL-3.0 per Gyan's build page; exact archive license bundle/source offer is NOT VERIFIED in the release tree | [FFmpeg license](https://ffmpeg.org/doxygen/7.0/md_LICENSE.html); [Gyan build terms](https://www.gyan.dev/ffmpeg/builds/) | Probe, convert, mix, cleanup, export |
| UVR5 HP2 weight | `HP2_all_vocals.pth`, SHA-256 `39796caa5db18d7f9382d8ac997ac967bfd85f7761014bb807d2543cc844ef05` | NOT VERIFIED for exact weight | [pinned archive](https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/0c47645e02a7bc3688d7b263b0042c81e3cd82cd/uvr5_weights.zip) | Optional separation |
| GPT-SoVITS pretrained archive | revision `0c47645e02a7bc3688d7b263b0042c81e3cd82cd`, SHA-256 `66274394318cbf134b78d0d5aeeccb73e96f5d43cf6876ac43560a972cb1f3fc` | NOT VERIFIED for contained weights | [pinned archive](https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/0c47645e02a7bc3688d7b263b0042c81e3cd82cd/pretrained_models.zip) | Pretrained inference/training |
| G2PW model | same pinned revision, SHA-256 `46292be0374a49308069233cd5c147ae4c41806558e4781a2467a31a4d8099da` | NOT VERIFIED for exact weight | [pinned archive](https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/0c47645e02a7bc3688d7b263b0042c81e3cd82cd/G2PWModel.zip) | Chinese G2P |
| NLTK data | same pinned revision, SHA-256 `eb3ec26ace3f9ccbb08a6d333e26f0941c47e230ece0717dc992bdb7e99808dd` | NOT VERIFIED for bundled archive | [pinned archive](https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/0c47645e02a7bc3688d7b263b0042c81e3cd82cd/nltk_data.zip) | Text preprocessing data |
| Open JTalk dictionary | `1.11`, same pinned revision, SHA-256 `fe6ba0e43542cef98339abdffd903e062008ea170b04e7e2a35da805902f382a` | NOT VERIFIED for redistributed archive | [pinned archive](https://huggingface.co/XXXXRT/GPT-SoVITS-Pretrained/resolve/0c47645e02a7bc3688d7b263b0042c81e3cd82cd/open_jtalk_dic_utf_8-1.11.tar.gz) | Text processing |
| MelBand RoFormer ONNX weight | conversion `60cb6b4b97e41b42f7ff16c2e386f47a8cc7e50a`; source `ac9b0614ab3cd7f77219e18ba494dfd93956c348`; SHA-256 `64a4f3bee48fbe7d971b23875adc924ed004c3533f49672592641dddc0f6f561` | MIT, with attribution listed by model card | [pinned model card](https://huggingface.co/smank/mel-band-roformer-vocals-onnx/tree/60cb6b4b97e41b42f7ff16c2e386f47a8cc7e50a) | Optional high-quality separation |
| RVC HuBERT assets | revision `e6d0c1a17da07c33557852f9dfa2bd44cc75737d`; model SHA-256 `cc8c20f4b90a520757260197a3ff2505705a7adbd20ad9eeaa4e1a9b38442ef5` | Repository metadata says MIT; exact artifact redistribution authorization NOT VERIFIED | [pinned repository](https://huggingface.co/lj1995/VoiceConversionWebUI/tree/e6d0c1a17da07c33557852f9dfa2bd44cc75737d) | RVC HuBERT features |
| RVC RMVPE weight | same pinned revision; SHA-256 `6d62215f4306e3ca278246188607209f09af3dc77ed4232efdd069798c4ec193` | NOT VERIFIED for exact weight | [pinned repository](https://huggingface.co/lj1995/VoiceConversionWebUI/tree/e6d0c1a17da07c33557852f9dfa2bd44cc75737d) | F0 extraction |
| RVC v2 generator/discriminator weights | same pinned revision; hashes in manifest | NOT VERIFIED for exact weights | [pinned repository](https://huggingface.co/lj1995/VoiceConversionWebUI/tree/e6d0c1a17da07c33557852f9dfa2bd44cc75737d) | RVC v2 initialization |

## Distribution obligations and blockers

- Retain applicable license texts and copyright notices in every release package. Qt obligations depend on shipped modules and selected LGPL/GPL/commercial terms.
- Miniforge's BSD-3-Clause installer notice does not clear the independent licenses of packages installed into its environment; the final SBOM must enumerate them.
- The exact FFmpeg executable must be identified by version, build source, SHA-256, and GPL enablement.
- The current lock origin is conda-forge `ffmpeg-8.0.1-gpl_hb2d76f6_914.conda`; it does not describe the installed Gyan `8.1.2` binaries. Resolve this mismatch before release.
- Every redistributed model weight needs its own explicit license/model-card evidence. If unavailable, the asset must be removed from release scope or the release remains blocked.
- User songs, voices, lyrics, and trained checkpoints require separate rights review.

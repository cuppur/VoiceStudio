# Gate E1 License Audit

Audit scope: current `main` at `5e3bfc62a166b2576ab7d00a4225cffe3c66a2e9`, using `manifests/runtime-assets-v1.json`, `locks/requirements-win-cu128.lock`, `locks/conda-win-64.lock`, `scripts/bootstrap_runtime.ps1`, and runtime imports. Audit date: 2026-09-05.

## Result

**FAIL — release blocker.** `THIRD_PARTY_NOTICES.md` records the discovered runtime components and authoritative sources. Licensing is not closed because several redistributed model archives/weights lack authoritative license evidence for the exact pinned artifact, the installed FFmpeg binaries do not match the locked conda artifact and their Gyan license bundle/source offer is not retained, the PySide6 commercial-versus-open-source distribution choice is not evidenced, and the final product SBOM is not yet evidenced. These are explicitly `NOT VERIFIED`; no source-code license has been applied to weights.

## Evidence inventory

| Area | Repository evidence | Outcome |
|---|---|---|
| GPT-SoVITS | Manifest commit `d523079fc05d9a8028d6085bffe4a2757c32abb6`; bootstrap downloads matching archive | MIT verified from pinned upstream LICENSE |
| RVC | Manifest commit `8f2fdbf483955f924b4c87ab34919170d0b704ed`; isolated install | MIT source verified; weights separate |
| PyTorch / torchaudio | Lock and bootstrap assert `2.7.1+cu128` | BSD-3-Clause / BSD-2-Clause verified |
| Qt / PySide6 | `pyproject.toml` pins `PySide6==6.9.2`; local Python 3.10 imports version `6.9.2`; PySide6/Essentials/Addons/Shiboken6 metadata all expose LGPL/GPL choices and a commercial license reference; built output contains Qt DLLs/plugins but no separate Qt license bundle was found | Version verified; selected distribution terms and shipped-license evidence remain NOT VERIFIED release blocker |
| FFmpeg / ffprobe | Local `C:\Users\cruelworld\AppData\Local\LocalVoiceStudio\tools\*.exe` reports `8.1.2-full_build-www.gyan.dev`, SHA-256s recorded in notices, and `--enable-gpl --enable-version3 --enable-static`; conda lock points to `https://conda.anaconda.org/conda-forge/win-64/ffmpeg-8.0.1-gpl_hb2d76f6_914.conda` | Actual binary is GPL-enabled and does not match lock; Gyan's build page says Windows full builds are GPLv3, but the exact archive license bundle/source offer is NOT VERIFIED release blocker |
| Miniforge | Manifest pins `26.3.2-3` and SHA-256; bootstrap checks Authenticode | Installer BSD-3-Clause verified; bundled package closure pending |
| UVR5 | Optional HP2 installed-file pin and archive pin | Exact weight license NOT VERIFIED |
| RoFormer | Manifest pins conversion/source revisions and SHA-256 | MIT and attribution verified from pinned model card |
| GPT-SoVITS pretrained/G2PW/NLTK/Open JTalk | Manifest pins revisions and hashes | Exact redistributed archive/contained-file licenses NOT VERIFIED |
| HuBERT / RMVPE / RVC pretrained | Manifest pins HF revision and per-file hashes | Weight-specific closure remains NOT VERIFIED |
| FunASR / SenseVoice / FSMN-VAD | Code requires local model and VAD dirs; manifest now contains six SenseVoice and four FSMN-VAD per-file pins, each with revision, URL, size, SHA-256, and license field | FunASR source MIT and both official ModelScope cards Apache-2.0 verified; final installed-file evidence and SBOM still pending |

## Local PySide6 and Qt evidence

`pyproject.toml` pins `PySide6==6.9.2`. The explicit interpreter `C:\Users\cruelworld\AppData\Local\Programs\Python\Python310\python.exe` imports PySide6 and Shiboken6 successfully and reports PySide6 `6.9.2`. The local site-packages metadata reports `LGPL-3.0-only OR GPL-2.0-only OR GPL-3.0-only` for `PySide6`, `PySide6_Essentials`, `PySide6_Addons`, and `shiboken6`. Each package contains `licenses/LicenseRef-Qt-Commercial.txt`; this file documents commercial Qt terms and is not evidence that a commercial entitlement exists.

The built output contains these Qt modules/DLLs: `Qt6Core`, `Qt6Gui`, `Qt6Widgets`, `Qt6Network`, `Qt6Multimedia`, `Qt6MultimediaWidgets`, `Qt6OpenGL`, `Qt6Pdf`, `Qt6Qml`, `Qt6QmlMeta`, `Qt6QmlModels`, `Qt6QmlWorkerScript`, `Qt6Quick`, `Qt6Svg`, and `Qt6VirtualKeyboard`, plus the corresponding PySide6 bindings and plugins. No separate PySide6/Qt license bundle was found inside `dist/LocalVoiceStudio/_internal`; the release package must carry the applicable texts and notices.

The upstream pinned-weight repository was also checked directly: its pinned `README.md` is only 24 bytes and no separate `LICENSE` file is listed. The repository metadata says `mit`, but that metadata does not identify the licenses of each archive's contained weights, so the GPT-SoVITS pretrained/G2PW/UVR5 entries remain `NOT VERIFIED`.

For the open-source route, Qt's official terms require compliance with LGPLv3 for the LGPL modules and GPLv3 for modules available only under GPL. The current build is dynamically deployed as separate Qt DLLs, which supports the required replaceability model, but release evidence must document how recipients can replace those DLLs with compatible modified versions and must avoid static linking. If the commercial route is selected, a valid Qt commercial license and its terms must be retained with the release record.

## Local FFmpeg and lock evidence

`C:\Users\cruelworld\AppData\Local\LocalVoiceStudio\tools\ffmpeg.exe` and `ffprobe.exe` both report `8.1.2-full_build-www.gyan.dev`, are static, and report `--enable-gpl --enable-version3` plus GPL codec flags. Their SHA-256 values are recorded in `THIRD_PARTY_NOTICES.md`. `locks/conda-win-64.lock` instead resolves `https://conda.anaconda.org/conda-forge/win-64/ffmpeg-8.0.1-gpl_hb2d76f6_914.conda`. The conda-forge feedstock distinguishes GPL and LGPL variants and identifies the package license accordingly ([feedstock](https://github.com/conda-forge/ffmpeg-feedstock), [artifact listing](https://anaconda.org/conda-forge/ffmpeg/files?version=8.0.0)); therefore the installed binary is not proven to be the locked artifact.

## Closure required before PASS

1. After the lyrics supply-chain agent finishes, re-audit every SenseVoice and FSMN-VAD file in the final manifest diff and retain the official ModelScope card evidence.
2. Replace the FFmpeg binary with the locked conda artifact or update the lock to the exact shipped Gyan artifact; retain the GPL license texts, attribution, and corresponding source offer for the selected build.
3. For PySide6 6.9.2, choose and evidence either LGPL/GPL distribution compliance or a valid commercial Qt license; ship the applicable license texts and notices. Because the application uses separate Qt DLLs, preserve dynamic replaceability: do not statically link Qt, allow replacement of the Qt libraries with compatible modified versions, and document the relink path for recipients.
4. Obtain explicit license terms for each GPT-SoVITS pretrained, G2PW, UVR5, HuBERT, RMVPE, and RVC pretrained weight. If unavailable, remove the asset from release scope or keep the release blocked.
5. Generate the product SBOM from the final installed environment so Miniforge/Conda and pip transitive licenses are represented.

## Recheck protocol

Compare the final manifest and bootstrap after the lyrics change, ensure every manifest asset has a notice row, and confirm no runtime asset downloads outside pinned paths. Any missing license, revision, or source remains `NOT VERIFIED` and blocks release.

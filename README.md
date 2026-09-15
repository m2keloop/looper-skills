# Looper Skills

个人实用 AI Agent Skills 集合，持续添加和完善。

## Skills

| Skill | 简介 |
| --- | --- |
| [ncm-convert](./ncm-convert/SKILL.md) | 使用 ncmdump 将网易云音乐 `.ncm` 文件转换为 MP3 或 FLAC，支持单文件和文件夹批量转换。 |
| [photo-style-batch](./photo-style-batch/SKILL.md) | 根据提示词批量转换照片，支持递增归档、双画面裁切、匿名输出和元数据清除。 |

### ncm-convert

自动检测系统并选择程序，询问待转换文件或文件夹的路径，然后执行转换并检查结果。默认保留原文件，结果保存到原目录；支持递归处理、指定输出目录、大写 `.NCM` 文件和输出重名检查。

随附程序支持 macOS Apple Silicon、Linux x86-64 和 Windows x64。输出为 MP3 还是 FLAC 取决于源文件内的音频格式。

使用示例：

> 用 $ncm-convert 帮我转换音乐。

程序来自 [taurusxin/ncmdump](https://github.com/taurusxin/ncmdump)，许可证见 [LICENSE.ncmdump.txt](./ncm-convert/bin/LICENSE.ncmdump.txt)。

### photo-style-batch

将一个固定原图文件夹中的照片逐张按提示词转换，并为每轮创建不可覆盖的编号目录。它也可以从上下双画面海报中确定性裁切出指定分区。

敏感照片可使用隐私模式：归档不保存完整提示词、绝对路径、原文件名、风格标签或时间；输出使用匿名编号，裁切默认移除 EXIF、GPS、XMP、IPTC 与缩略图元数据。图片交给生成服务前，Skill 还要求先创建去元数据的临时副本。

使用示例：

> 用 $photo-style-batch 把这个文件夹的照片做成黑白粗蜡笔风，并用隐私模式单独归档。

## 使用方式

每个 Skill 独立放在同名文件夹中，包含 `SKILL.md` 及所需资源。将整个 Skill 文件夹复制到所用 Agent 的技能目录；Codex 可放在 `~/.codex/skills/` 下，然后在对话中调用对应 Skill。

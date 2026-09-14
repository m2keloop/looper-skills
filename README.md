# Looper Skills

个人实用 AI Agent Skills 集合，持续添加和完善。

## Skills

| Skill | 简介 |
| --- | --- |
| [ncm-convert](./ncm-convert/SKILL.md) | 使用 ncmdump 将网易云音乐 `.ncm` 文件转换为 MP3 或 FLAC，支持单文件和文件夹批量转换。 |

### ncm-convert

自动检测系统并选择程序，询问待转换文件或文件夹的路径，然后执行转换并检查结果。默认保留原文件，结果保存到原目录；支持递归处理、指定输出目录、大写 `.NCM` 文件和输出重名检查。

随附程序支持 macOS Apple Silicon、Linux x86-64 和 Windows x64。输出为 MP3 还是 FLAC 取决于源文件内的音频格式。

使用示例：

> 用 $ncm-convert 帮我转换音乐。

程序来自 [taurusxin/ncmdump](https://github.com/taurusxin/ncmdump)，许可证见 [LICENSE.ncmdump.txt](./ncm-convert/bin/LICENSE.ncmdump.txt)。

## 使用方式

每个 Skill 独立放在同名文件夹中，包含 `SKILL.md` 及所需资源。将整个 Skill 文件夹复制到所用 Agent 的技能目录；Codex 可放在 `~/.codex/skills/` 下，然后在对话中调用对应 Skill。

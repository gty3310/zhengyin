# 更新日志（Changelog）

本项目遵循 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，
版本号遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## [0.1.0] - 2026-11-04

### 新增

- `zhengyin convert`：古文 → TTS 安全文本的确定性转换。定音优先级为
  学习层 ＞ 用户拼音锁定 ＞ 词组表最长匹配 ＞ 通假（义项为键）＞ 破读
  （仅已核条目可自动落字）＞ 审音表异读（例句语境匹配）＞ 同音安全替字；
  原文从不改动，输出可附逐字转换清单（`--changes`，可审计）与逐句拼音
  对照（`--with-pinyin`）。
- 问答回路：定不了的字不猜——写出 `questions.json` 并以退出码 2 结束；
  用户经 Agent 作答后以 `--answers` 回填，答案须通过发音校验（读音含
  目标音且默认读音即目标音）才写入本地学习层并继续转换；同语境再遇
  直接套用。
- `zhengyin data fetch / status`：从 zhengyin-dict 的 GitHub Release
  下载字典数据库并按 manifest 的 sha256 校验；数据与代码分仓，
  CLI 本身不打包字典数据。
- `zhengyin lookup <字>`：查一个字的全部读音、默认读音、通假条目
  （含义项）与破读条目，供 Agent 答疑时引用。
- `zhengyin learn export`：把学习层导出为 zhengyin-dict 的 tongjia
  格式 JSONL，供提 PR 回流字典仓。
- 字形变体归一：繁/简/异体（如 說/説/说）在查表时按同一字处理
  （归一表派生自 Unicode Unihan，见 README 数据来源一节）。
- 通假在默认读音下的语境门：通假条目本质是特定语境的用法，原字处于
  默认读音时，仅当条目例句与当前句子语境吻合才自动换本字，其余保留
  原字，避免常见字被冷僻通假误伤；特殊（非默认）读音下则按义项规则
  替换或提问。
- 提问触发收紧：无任何定音依据时，仅当有破读条目、非默认读音下存在
  多本字竞争、或条目例句与语境吻合等「特殊用法证据」才提问，不因
  字典里偶有一条冷僻通假就打断转换。

[0.1.0]: https://github.com/gty3310/zhengyin/releases/tag/v0.1.0

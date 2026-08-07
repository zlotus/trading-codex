# 项目文档

本目录保存跨设备、跨会话继续开发所需的项目上下文。

- [`context.md`](context.md)：稳定的项目目标、工作流、架构和长期约束。
- [`implementation-plan.md`](implementation-plan.md)：分阶段实施计划和验收门槛。
- [`progress.md`](progress.md)：简洁的当前状态和交接快照。
- [`decisions/`](decisions/README.md)：已接受的长期技术及产品决策，以及决策依据。
- [`cloudflare-zero-trust-deployment.md`](cloudflare-zero-trust-deployment.md)：
  通过本机现有 Cloudflare Tunnel 和 Access 安全发布到互联网。
- [根目录 README](../README.md)：环境安装、本地开发、构建和运行说明。

仓库规则和外部规范具有约束力；代码、测试和配置描述已经实现的行为；本目录
负责解释长期上下文与当前方向，不取代这些事实来源。

## 文档语言

项目文档、操作指南和交接说明以后以简体中文为主。代码标识符、命令、API
字段、协议名称和必要的上游专有名词保留原文，以免翻译造成歧义。现有英文
文档在后续实质性修改时逐步翻译，不为单纯改写语言制造大范围文档变动。

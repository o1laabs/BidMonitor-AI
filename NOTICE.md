# NOTICE

## 上游

本仓库是 [`zhiqianzheng/BidMonitor-AI`](https://github.com/zhiqianzheng/BidMonitor-AI)
的 fork，用于个人研究与学习。

## ⚠️ 许可证状态

**上游仓库没有提供任何许可证文件**（GitHub API 返回 `license: null`，
文件树中不存在 LICENSE / COPYING）。

**这意味着版权默认保留（All rights reserved）。** 除 GitHub 服务条款允许的
平台内 fork 行为外，本项目未从上游获得任何明示授权 —— 不包含分发、商用、
或再发布的权利。

因此，本 fork：

- 仅用于**个人学习与研究**
- **不主张任何授权**，也不将上游代码重新授权给任何人
- 若你打算使用这份代码，**请自行联系上游作者取得许可**

如果上游后续补充了许可证，以该许可证为准。

## 本 fork 的改动

见 `CHANGES.md`。核心改动是安全相关：移除了硬编码的第三方 API 中转端点，
防止使用者的业务数据在不知情的情况下被发送到第三方服务器。

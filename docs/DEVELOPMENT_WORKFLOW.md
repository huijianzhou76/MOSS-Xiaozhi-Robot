# MOSS-Xiaozhi-Robot Development Workflow

本仓库采用 **main + feature branch + Pull Request** 的开发流程。

## Branch policy

- `main`：稳定主分支，只接受经过检查的 Pull Request。
- `feature/<name>`：新功能开发，例如 `feature/tts-engine`、`feature/home-assistant`。
- `fix/<name>`：缺陷修复。
- `chore/<name>`：工程配置、文档、CI 等维护工作。

## Standard flow

1. 从最新 `main` 创建新分支。
2. 所有功能代码只提交到该分支。
3. 完成代码检查、构建和测试。
4. 创建 Pull Request，目标分支为 `main`。
5. PR 通过后再合并到 `main`。
6. 后续开发重新从最新 `main` 创建新分支，避免长期堆叠分支。

## Project rules

- 不直接在 `main` 上开发新功能。
- 不将第三方无明确许可证的源码直接复制进仓库。
- 保留上游 `moss-xiaozhi` 的 MIT License 与版权声明。
- 新增第三方依赖时，应记录来源、许可证和用途。
- 硬件控制必须提供安全边界、参数限制和失败恢复机制。
- AI/Agent 层不直接输出任意 GPIO 或系统命令，需通过受控工具或硬件适配层执行。

## Initial roadmap

计划按照独立分支逐步推进：

`feature/tts-engine` → `feature/moss-voice` → `feature/home-assistant` → `feature/mcp-gateway` → `feature/moss-agent` → `feature/vision` → `feature/hardware-integration`

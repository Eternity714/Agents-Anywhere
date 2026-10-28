# Codex 无项目会话 Implementation Plan

> **For agentic workers:** Use executing-plans to implement this plan task-by-task.

**Goal:** 将 Codex 明确无项目的导入会话统一显示为「未分组」，保留历史、cwd 和用户归档。

**Architecture:** Connector 读取当前实例 Codex Home 中 App 的明确归属记录；原生非空项目 ID 可确认已归属，原生 null 单独不足以确认无项目。Server 持久保存三态观察，统一处理所有元数据写入。归属确认与 Timeline 检查点独立；Android 复用可空 `projectId` 补齐未分组和分页。

**Tech Stack:** Python、FastAPI、PostgreSQL、Alembic、Kotlin / Compose。

**Spec:** 用户本次请求及选择；上游 issue #360、PR #400 / #413 仅作参考，限定实现 Codex 无项目会话。

## Global Constraints

- 字段缺失和非法值表示未知，不能当作明确无项目；不根据目录名或前缀猜测。
- 只接受 Codex 的布尔归属观察；连接器、Runtime 绑定仍必须通过现有校验。
- 已由 AA 创建并明确选择项目的会话保留用户选择；导入会话可在原生归属变化后重新分组。
- 归属与 cwd 分离，IPC / 旧 Connector 的缺字段更新不能恢复错误项目；迁移不猜测历史归属。
- 不删除项目、会话或附件；按有效新观察逐条修正已有导入会话。

### Task 1: Connector 与 Server 归属观察

**Files:** Connector `sessions/project_membership.py`、`sessions/reader.py`、`runtime.py`、Runtime 协议的元数据确认钩子和 `server/runtime_sync.py`；Server 通知入口、session repository、schema 与新增迁移 `v2_45.py`。

**Interfaces:** `SessionMeta.metadata.nativeProjectAssigned: bool`；省略表示未知。数据库 `sessions.native_project_assigned` 可空整数（NULL / 0 / 1）。客户端继续使用已有 `projectId: string | null`。

- [x] 补充并运行失败回归：App 无项目登记、项目分配、冲突、native null、字段缺失、非法值、损坏及无权限状态。
- [x] reader 从当前实例读取明确归属，文件最大 8 MiB；不推断输出目录，不把 native null 当作无项目。
- [x] 仅归属变化也发送元数据；成功 ingest 后确认实际上传的观察，失败重试，覆盖刷新与 Timeline 准备交错的竞态。
- [x] Server 保存观察，新增导入和已有 snapshot / upsert 共用决策：false 返回空项目，true 按 cwd 分组，未知继承；平台会话保留选择。
- [x] 使用真实独立 PostgreSQL 测试库验证旧会话纠正、重复同步、重新归入项目、归档保留、身份隔离和新迁移；修复未版本化数据库重复加列问题。

### Task 2: Android 与交付验证

**Files:** Android `SidebarSelectors.kt`、`HomeProjectComponents.kt`、中英文 strings；`docs/api/realtime.md`、发布说明、CHANGELOG。

- [x] 增加 Android selector 回归和未分组入口，复用设备、Runtime、归档与置顶规则，并接通全局分页。Android 测试因缺少 Java 未执行。
- [x] 更新可选元数据契约、迁移、发布说明和 CHANGELOG；状态格式未知时保留最后有效分组。
- [x] 完成两轮独立代码审查及并发复现，修复全部发现。
- [x] Connector 新回归和实例绑定 32 项通过；全量 1201 通过、25 失败、71 跳过，失败项均在原代码基线中复现。Server 项目测试 18 项通过，迁移及其他相关测试已执行；保留实际失败及未完成检查说明。
- [x] 工作区改动完成审阅，用户已要求暂不处理既有测试失败，并授权提交、合并到 main 和推送；真实安装、远程 Runtime 和 Android 真机交互尚未验证。

## 验证记录

- Connector 新回归与实例绑定：`uv run --no-sync pytest -q tests/test_codex_projectless.py tests/test_runtime_instance_binding.py`，32 通过。
- Connector 全量：1201 通过、25 失败、71 跳过；与原工作区全量失败列表比对，没有新增失败（原工作区另有 1 项 SDK 进程清理偶发失败）。
- Server 项目可见性：18 通过；新增迁移及未版本化数据库升级测试通过。先前项目唯一性、架构与完整迁移检查中 129 项通过，发现的两个失败均已修复并定向复验。
- 额外 API 检查运行到 27 项后，在既有 `test_sessions_list_uses_cursor_pages_and_archive_filter` 的创建会话请求挂起，已中止；不宣称完整 Server 套件通过。
- DSH `corepack yarn check`：类型、构建和产物校验通过；222 项测试中 186 通过、36 失败，包括 Windows URL 解析、短路径比较、symlink 权限等限制，未修改 DSH 实现。
- 新增和相关文件静态检查与 `git diff --check` 通过；全仓库保留既有 lint 问题。
- Android Gradle 检查因 `JAVA_HOME` 未配置且无 Java 可执行程序而无法运行。

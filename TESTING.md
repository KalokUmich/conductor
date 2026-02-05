# Testing Guide / 测试指南

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

This document provides comprehensive testing instructions for the Conductor project.

### 📋 Test Overview

| Component | Test Type | Tests | Coverage |
|-----------|-----------|-------|----------|
| Backend | Unit + Integration | 104 | Full API coverage |
| Extension | Manual | N/A | UI + functionality |
| End-to-End | Manual | N/A | Full workflow |

---

### 🔧 Backend Testing

#### Prerequisites

```bash
# Activate virtual environment
source .venv/bin/activate  # Linux/Mac
# or: .venv\Scripts\activate  # Windows
```

#### Run All Tests

```bash
cd backend
pytest tests/ -v
```

Expected output:
```
============================= 104 passed in 1.5s ==============================
```

#### Run Specific Test Modules

```bash
# Chat tests
pytest tests/test_chat.py -v

# Agent tests (MockAgent)
pytest tests/test_mock_agent.py -v

# Policy tests
pytest tests/test_policy.py -v

# Audit tests
pytest tests/test_audit.py -v

# Summary tests
pytest tests/test_summary.py -v
```

#### Test Coverage Report

```bash
pytest tests/ --cov=app --cov-report=html
open htmlcov/index.html  # View in browser
```

---

### 🖥️ Extension Testing

#### 1. Compile and Launch

```bash
cd extension
npm install
npm run compile
```

Then in VS Code:
1. Press `F5` to launch Extension Development Host
2. Open the Conductor panel in the sidebar

#### 2. Test Checklist

| # | Feature | Steps | Expected |
|---|---------|-------|----------|
| 1 | Panel Opens | Click Conductor icon | Chat UI displays |
| 2 | Role Badge | Check header | Shows "👤 Member" or "👑 Lead" |
| 3 | Change Role | Settings → `aiCollab.role` → "lead" | Notification + UI updates |
| 4 | Generate Changes | Click "Generate Changes" (Lead only) | Changes preview appears |
| 5 | View Diff | Click "View Diff" | Diff viewer opens |
| 6 | Apply Changes | Click "Apply" | Changes applied to files |
| 7 | Auto Apply | Toggle Auto Apply | State persists |

#### 3. Test with VSIX Package

```bash
cd extension
npx vsce package
```

Install the VSIX:
1. `Ctrl+Shift+P` → "Extensions: Install from VSIX..."
2. Select `ai-collab-0.0.1.vsix`
3. Reload VS Code

---

### 🔄 End-to-End Testing (Multi-User)

This tests the complete collaboration workflow.

#### Prerequisites

- Backend server running (`uvicorn app.main:app --reload`)
- Two VS Code instances (or two computers)

#### Scenario: Host + Guest Collaboration

| Step | Actor | Action | Expected |
|------|-------|--------|----------|
| 1 | Host | Open Conductor panel | Live Share starts, invite URL logged |
| 2 | Host | Copy invite URL from Output | URL copied |
| 3 | Guest | Open invite URL in browser | Invite page shows |
| 4 | Guest | Click "Join Live Share in VS Code" | VS Code opens |
| 5 | Guest | Install Conductor extension | Extension installed |
| 6 | Both | Send chat messages | Messages appear in both |
| 7 | Host | Click "Generate Changes" | Changes generated |
| 8 | Host | Review and Apply | Changes applied |
| 9 | Guest | Verify file changes | Files updated via Live Share |
| 10 | Host | Click "End Chat" | Session ends for all |

#### Testing WebSocket Chat

```bash
# In terminal 1: Start backend
cd backend && uvicorn app.main:app --reload

# In browser: Open chat page
open "http://localhost:8000/chat?roomId=test-room&role=engineer"

# In terminal 2: Send a test message via WebSocket
# (Use a WebSocket client like wscat)
npx wscat -c ws://localhost:8000/ws/chat/test-room
> {"type":"join","userId":"user1","displayName":"Test","role":"engineer"}
> {"userId":"user1","displayName":"Test","role":"engineer","content":"Hello!"}
```

---

### 🐛 Debugging Tips

#### Backend Debug

```bash
# Run with debug logging
LOG_LEVEL=DEBUG uvicorn app.main:app --reload

# Check DuckDB audit logs
cd backend
python -c "import duckdb; print(duckdb.connect('audit_logs.duckdb').execute('SELECT * FROM audit_logs').fetchall())"
```

#### Extension Debug

1. Open Output panel (`Ctrl+Shift+U`)
2. Select "Conductor Invite Links" from dropdown
3. View WebSocket and session logs

---

<a name="中文"></a>
## 中文

本文档提供 Conductor 项目的完整测试说明。

### 📋 测试概览

| 组件 | 测试类型 | 测试数量 | 覆盖范围 |
|------|----------|----------|----------|
| 后端 | 单元 + 集成 | 104 | 完整 API 覆盖 |
| 扩展 | 手动 | N/A | UI + 功能 |
| 端到端 | 手动 | N/A | 完整工作流 |

---

### 🔧 后端测试

#### 前置条件

```bash
# 激活虚拟环境
source .venv/bin/activate  # Linux/Mac
# 或: .venv\Scripts\activate  # Windows
```

#### 运行所有测试

```bash
cd backend
pytest tests/ -v
```

预期输出：
```
============================= 104 passed in 1.5s ==============================
```

#### 运行特定测试模块

```bash
# 聊天测试
pytest tests/test_chat.py -v

# Agent 测试 (MockAgent)
pytest tests/test_mock_agent.py -v

# 策略测试
pytest tests/test_policy.py -v

# 审计测试
pytest tests/test_audit.py -v
```

---

### 🖥️ 扩展测试

#### 1. 编译和启动

```bash
cd extension
npm install
npm run compile
```

然后在 VS Code 中：
1. 按 `F5` 启动扩展开发主机
2. 在侧边栏打开 Conductor 面板

#### 2. 测试清单

| # | 功能 | 步骤 | 预期结果 |
|---|------|------|----------|
| 1 | 面板打开 | 点击 Conductor 图标 | 聊天 UI 显示 |
| 2 | 角色徽章 | 检查头部 | 显示"👤 Member"或"👑 Lead" |
| 3 | 更改角色 | 设置 → `aiCollab.role` → "lead" | 通知 + UI 更新 |
| 4 | 生成更改 | 点击"Generate Changes"（仅Lead） | 更改预览出现 |
| 5 | 查看差异 | 点击"View Diff" | 差异查看器打开 |
| 6 | 应用更改 | 点击"Apply" | 更改应用到文件 |

#### 3. 使用 VSIX 包测试

```bash
cd extension
npx vsce package
```

安装 VSIX：
1. `Ctrl+Shift+P` → "Extensions: Install from VSIX..."
2. 选择 `ai-collab-0.0.1.vsix`
3. 重新加载 VS Code

---

### 🔄 端到端测试（多用户）

此测试完整的协作工作流。

#### 前置条件

- 后端服务器运行中 (`uvicorn app.main:app --reload`)
- 两个 VS Code 实例（或两台电脑）

#### 场景：Host + Guest 协作

| 步骤 | 角色 | 操作 | 预期结果 |
|------|------|------|----------|
| 1 | Host | 打开 Conductor 面板 | Live Share 启动，邀请 URL 记录 |
| 2 | Host | 从 Output 复制邀请 URL | URL 已复制 |
| 3 | Guest | 在浏览器中打开邀请 URL | 邀请页面显示 |
| 4 | Guest | 点击"Join Live Share in VS Code" | VS Code 打开 |
| 5 | Guest | 安装 Conductor 扩展 | 扩展已安装 |
| 6 | 两者 | 发送聊天消息 | 消息在两边都出现 |
| 7 | Host | 点击"Generate Changes" | 更改已生成 |
| 8 | Host | 审查并应用 | 更改已应用 |
| 9 | Guest | 验证文件更改 | 文件通过 Live Share 更新 |
| 10 | Host | 点击"End Chat" | 所有人的会话结束 |

---

### 🐛 调试技巧

#### 后端调试

```bash
# 带调试日志运行
LOG_LEVEL=DEBUG uvicorn app.main:app --reload
```

#### 扩展调试

1. 打开输出面板 (`Ctrl+Shift+U`)
2. 从下拉菜单选择"Conductor Invite Links"
3. 查看 WebSocket 和会话日志


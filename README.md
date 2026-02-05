# Conductor

> AI-Powered Collaborative Coding for VS Code

[English](#english) | [中文](#中文)

---

<a name="english"></a>
## English

Conductor is a VS Code extension that combines **Live Share**, **real-time chat**, and **AI-powered code generation** for seamless team collaboration.

### ✨ Key Features

| Feature | Description |
|---------|-------------|
| 🔗 **Live Share Integration** | Share your coding session with teammates in real-time |
| 💬 **Real-time Chat** | Built-in chat with message history and user presence |
| 🤖 **AI Code Generation** | Generate code changes using AI (MockAgent for testing) |
| 👥 **Role-Based Access** | Lead (full control) vs Member (chat only) permissions |
| 🔄 **Auto Apply** | Automatically apply safe, small changes |
| 📝 **Diff Preview** | Review AI-generated changes before applying |
| 📊 **Audit Logging** | Track all applied changes with DuckDB |

### 🏗️ Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                         VS Code Host                            │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐              │
│  │  Conductor  │  │  Live Share │  │   Editor    │              │
│  │   WebView   │──│  Extension  │──│  Workspace  │              │
│  └─────────────┘  └─────────────┘  └─────────────┘              │
│         │                                                        │
│         │ WebSocket + REST                                       │
└─────────│───────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────────┐
│                      Backend (FastAPI)                          │
│  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌─────────┐  ┌────────┐ │
│  │  Chat   │  │  Agent  │  │ Summary │  │ Policy  │  │ Audit  │ │
│  │ (WS)    │  │ (Mock)  │  │         │  │         │  │(DuckDB)│ │
│  └─────────┘  └─────────┘  └─────────┘  └─────────┘  └────────┘ │
└─────────────────────────────────────────────────────────────────┘
```

### 📦 Project Structure

```
Conductor/
├── backend/               # FastAPI backend server
│   ├── app/
│   │   ├── chat/         # WebSocket chat rooms
│   │   ├── agent/        # AI code generation (MockAgent)
│   │   ├── policy/       # Auto-apply policy rules
│   │   ├── audit/        # DuckDB audit logging
│   │   └── summary/      # Chat summarization
│   └── tests/            # Backend tests (104 tests)
├── extension/            # VS Code extension
│   ├── src/              # TypeScript source
│   └── media/            # WebView HTML/CSS
├── config/               # Configuration files
│   └── conductor.yaml    # Main config (ngrok, LLM, limits)
└── shared/               # Shared schemas
    └── changeset.schema.json
```

### 🚀 Quick Start

#### Prerequisites

- Python 3.10+
- Node.js 18+
- VS Code 1.85+

#### 1. Clone the Repository

```bash
git clone https://github.com/yourusername/Conductor.git
cd Conductor
```

#### 2. Set Up Backend

```bash
# Create virtual environment
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# or: .venv\Scripts\activate  # Windows

# Install dependencies
cd backend
pip install -r requirements.txt

# Start server
uvicorn app.main:app --reload
```

#### 3. Set Up Extension

```bash
cd extension
npm install
npm run compile
```

#### 4. Launch Extension (Development Mode)

1. Open `extension/` folder in VS Code
2. Press `F5` to launch Extension Development Host
3. The Conductor panel appears in the sidebar

#### 5. Install Extension (Production Mode)

**Option A: Install from VSIX file**

```bash
# Build the VSIX package
cd extension
npx vsce package
```

Then in VS Code:
1. Press `Ctrl+Shift+P` (or `Cmd+Shift+P` on Mac)
2. Type `Extensions: Install from VSIX...`
3. Select the generated `ai-collab-0.0.1.vsix` file
4. Reload VS Code

**Option B: Share VSIX with team members**

1. Send the `.vsix` file to team members
2. They install it using the same steps above

### ⚙️ Configuration

Edit `config/conductor.yaml`:

```yaml
server:
  host: "0.0.0.0"
  port: 8000

ngrok:
  enabled: false
  authtoken: "your-ngrok-token"

change_limits:
  max_files: 2
  max_lines: 50
```

### 📖 Documentation

- [Backend API Documentation](backend/README.md)
- [Extension Documentation](extension/README.md)
- [Testing Guide](TESTING.md)

### 🤝 Collaboration Workflow

1. **Host** opens VS Code and starts Conductor
2. Live Share session auto-starts and generates invite URL
3. **Host** shares the invite URL with team members
4. **Guests** click the link → install extension → join session
5. Team collaborates via chat while Host can generate AI code
6. Changes are reviewed and applied to shared workspace

---

<a name="中文"></a>
## 中文

Conductor 是一个 VS Code 扩展，将 **Live Share**、**实时聊天** 和 **AI 代码生成** 结合在一起，实现无缝团队协作。

### ✨ 主要功能

| 功能 | 描述 |
|------|------|
| 🔗 **Live Share 集成** | 与队友实时共享编码会话 |
| 💬 **实时聊天** | 内置聊天，支持消息历史和用户在线状态 |
| 🤖 **AI 代码生成** | 使用 AI 生成代码更改（测试用 MockAgent） |
| 👥 **基于角色的访问控制** | Lead（完全控制）vs Member（仅聊天）权限 |
| 🔄 **自动应用** | 自动应用安全的小型更改 |
| 📝 **差异预览** | 在应用之前审查 AI 生成的更改 |
| 📊 **审计日志** | 使用 DuckDB 跟踪所有应用的更改 |

### 🚀 快速开始

#### 前置要求

- Python 3.10+
- Node.js 18+
- VS Code 1.85+

#### 1. 克隆仓库

```bash
git clone https://github.com/yourusername/Conductor.git
cd Conductor
```

#### 2. 设置后端

```bash
# 创建虚拟环境
python -m venv .venv
source .venv/bin/activate  # Linux/Mac
# 或: .venv\Scripts\activate  # Windows

# 安装依赖
cd backend
pip install -r requirements.txt

# 启动服务器
uvicorn app.main:app --reload
```

#### 3. 设置扩展

```bash
cd extension
npm install
npm run compile
```

#### 4. 启动扩展（开发模式）

1. 在 VS Code 中打开 `extension/` 文件夹
2. 按 `F5` 启动扩展开发主机
3. Conductor 面板出现在侧边栏

#### 5. 安装扩展（生产模式）

**方式 A：从 VSIX 文件安装**

```bash
# 构建 VSIX 包
cd extension
npx vsce package
```

然后在 VS Code 中：
1. 按 `Ctrl+Shift+P`（Mac 上按 `Cmd+Shift+P`）
2. 输入 `Extensions: Install from VSIX...`
3. 选择生成的 `ai-collab-0.0.1.vsix` 文件
4. 重新加载 VS Code

**方式 B：与团队成员分享 VSIX**

1. 将 `.vsix` 文件发送给团队成员
2. 他们使用上述相同步骤安装

### ⚙️ 配置

编辑 `config/conductor.yaml`：

```yaml
server:
  host: "0.0.0.0"
  port: 8000

ngrok:
  enabled: false
  authtoken: "your-ngrok-token"

change_limits:
  max_files: 2
  max_lines: 50
```

### 🤝 协作工作流

1. **Host** 打开 VS Code 并启动 Conductor
2. Live Share 会话自动启动并生成邀请 URL
3. **Host** 与团队成员分享邀请 URL
4. **Guest** 点击链接 → 安装扩展 → 加入会话
5. 团队通过聊天协作，Host 可以生成 AI 代码
6. 更改被审查并应用到共享工作区

### 📖 文档

- [后端 API 文档](backend/README.md)
- [扩展文档](extension/README.md)
- [测试指南](TESTING.md)

---

## License

MIT License - See [LICENSE](LICENSE) for details.

Copyright (c) 2024 Kalok


# AI Collab VS Code Extension

A VS Code extension that provides an AI collaboration panel with role-based permissions.

## Features

- **Chat UI**: Modern, responsive chat interface built with Tailwind CSS
- **Role-Based Permissions**: Lead and Member roles with different UI capabilities
  - **Lead**: Full access (Create Summary, Generate Changes, Auto Apply)
  - **Member**: Chat only
- **Real-time Updates**: UI updates automatically when settings change
- **Generate Changes**: Generate code modifications via AI agent
- **Diff Preview**: Preview proposed changes before applying
- **Apply Changes**: Apply generated changes to workspace files
- **Auto Apply Toggle**: Enable/disable automatic application of safe changes
- **Policy Evaluation**: Automatic safety checks before applying changes
  - Max 2 files per change
  - Max 50 lines changed
  - Forbidden paths: `infra/`, `db/`, `security/`

## Quick Start

```bash
cd extension
npm install
npm run compile
```

Then press `F5` in VS Code to launch the extension.

## Project Structure

```
extension/
├── src/
│   ├── extension.ts          # Main extension entry point
│   └── services/
│       ├── permissions.ts    # Role-based permission logic
│       └── diffPreview.ts    # Diff preview & apply changes
├── media/
│   ├── chat.html             # Chat UI with pending changes card
│   ├── tailwind.css          # Compiled Tailwind CSS
│   └── input.css             # Tailwind source
├── out/                      # Compiled JavaScript (generated)
├── package.json              # Extension manifest
├── tsconfig.json             # TypeScript configuration
└── tailwind.config.js        # Tailwind configuration
```

## Testing

### 1. Launch the Extension

```bash
# Compile (required after code changes)
npm run compile

# Then press F5 in VS Code to start debugging
```

### 2. Open the Panel

In the Extension Development Host window:
- Press `Cmd+Shift+P` (Mac) or `Ctrl+Shift+P` (Windows/Linux)
- Type **"AI Collab: Open Panel"** and select it

### 3. Test Role-Based Permissions

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open Panel | Shows 👤 Member badge, chat only |
| 2 | Open Settings (`Cmd+,` / `Ctrl+,`) | Settings window opens |
| 3 | Search `aiCollab.role` | Find the role setting |
| 4 | Change to `lead` | Notification: "Role changed to: lead" |
| 5 | Check Panel | Shows 👑 Lead badge + 3 action buttons |
| 6 | Change back to `member` | Buttons disappear, badge changes |

**Lead-only features:**
- 🟢 Create Summary button (emerald)
- 🟣 Generate Changes button (purple)
- 🔄 Auto Apply toggle switch (in header)

### 4. Test Generate Changes Flow

> ⚠️ Requires backend server running: `cd backend && uvicorn app.main:app --reload`

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Open a file in editor | File is active |
| 2 | Set role to `lead` | Lead badge + action buttons appear |
| 3 | Click "Generate Changes" | Progress notification, then diff preview opens |
| 4 | Check sidebar | "Pending Changes" card appears with file count, lines changed, policy status |
| 5 | Click "View Diff" | Opens diff view in editor |
| 6 | Click "Apply" | Changes applied to file, success notification |
| 7 | Click "Discard" (alternative) | Pending changes card disappears |

### 5. Test Auto Apply Toggle

| Step | Action | Expected Result |
|------|--------|-----------------|
| 1 | Enable Auto toggle | Notification: "🔄 Auto Apply enabled" |
| 2 | Click "Generate Changes" | If policy allows, changes auto-apply |
| 3 | Disable Auto toggle | Notification: "⏸️ Auto Apply disabled" |

### Troubleshooting

| Problem | Solution |
|---------|----------|
| Old UI shows "Welcome..." | Run `npm run compile`, restart debug (Shift+F5, then F5) |
| Role change has no effect | Restart debug session (Shift+F5, then F5) |
| No notification on role change | Check Developer Tools console for errors |

**Open WebView Developer Tools:**
`Cmd+Shift+P` → "Developer: Open Webview Developer Tools"

## Development

### Commands

```bash
npm run compile    # Compile TypeScript
npm run watch      # Watch mode (auto-compile on save)
npm run build:css  # Rebuild Tailwind CSS
```

### Architecture

- **AICollabPanel**: Singleton WebView panel manager
- **PermissionsService**: Singleton for role-based access control
- **Configuration**: `aiCollab.role` setting (`lead` | `member`)

### Adding New Features

1. Add permission to `permissions.ts`:
   ```typescript
   const PERMISSION_MATRIX: Record<Role, Feature[]> = {
       lead: ['chat', 'createSummary', 'generateChanges', 'autoApply', 'newFeature'],
       member: ['chat']
   };
   ```

2. Update `WebViewPermissions` interface
3. Add UI elements in `chat.html`
4. Rebuild: `npm run compile && npm run build:css`

## Building for Production

```bash
npm install -g @vscode/vsce
vsce package
```

## License

MIT


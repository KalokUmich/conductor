import { useRef, useState } from "react";
import { useSession } from "../contexts/SessionContext";
import { ChatProvider } from "../contexts/ChatContext";
import { ChatTab } from "./chat/ChatTab";
import { TasksTab } from "./tasks/TasksTab";
import { StatePanels } from "./panels/StatePanels";
import { ChatHeader } from "./chat/ChatHeader";
import { UsersSidebar } from "./chat/UsersSidebar";
import { useContainerWidth } from "../hooks/useContainerWidth";
import type { ConductorState } from "../types/messages";

type TabId = "chat" | "tasks";

const ACTIVE_STATES: ConductorState[] = ["Hosting", "Joined"];

export function App() {
  const { state } = useSession();
  const [activeTab, setActiveTab] = useState<TabId>("chat");
  const [showUsers, setShowUsers] = useState(false);
  const shellRef = useRef<HTMLDivElement>(null);
  const widthBreakpoint = useContainerWidth(shellRef);

  const isActive = ACTIVE_STATES.includes(state.conductorState);

  if (!isActive) {
    return <StatePanels />;
  }

  return (
    <ChatProvider>
      <div ref={shellRef} className={`app-shell app-${widthBreakpoint}`}>
        <ChatHeader showUsers={showUsers} onToggleUsers={() => setShowUsers(!showUsers)} />

        <TabBar activeTab={activeTab} onTabChange={setActiveTab} />

        <div className="app-content-row">
          <div className="app-content">
            {/* Both tabs stay mounted via stacked layers — preserves state across switches */}
            <div className={`tab-panel tab-panel-chat ${activeTab === "chat" ? "tab-panel-active" : "tab-panel-hidden"}`}>
              <ChatTab />
            </div>
            <div className={`tab-panel tab-panel-tasks ${activeTab === "tasks" ? "tab-panel-active" : "tab-panel-hidden"}`}>
              <TasksTab />
            </div>
          </div>
          <UsersSidebar visible={showUsers} />
        </div>
      </div>
    </ChatProvider>
  );
}

interface TabBarProps { activeTab: TabId; onTabChange: (tab: TabId) => void; }

function TabBar({ activeTab, onTabChange }: TabBarProps) {
  return (
    <div className="tab-bar">
      <TabButton label="Chat" active={activeTab === "chat"} onClick={() => onTabChange("chat")} />
      <TabButton label="Tasks" active={activeTab === "tasks"} onClick={() => onTabChange("tasks")} />
    </div>
  );
}

function TabButton({ label, active, onClick }: { label: string; active: boolean; onClick: () => void }) {
  return (
    <button className={`tab-button ${active ? "tab-active" : ""}`} onClick={onClick} role="tab" aria-selected={active}>
      {label}
      {active && <div className="tab-indicator" />}
    </button>
  );
}

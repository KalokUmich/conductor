import { useCallback, useEffect, useState } from "react";

// ============================================================
// Toast notification system
// ============================================================

interface ToastItem {
  id: number;
  message: string;
  type: "success" | "error" | "info";
}

let toastId = 0;
const listeners: Set<(toast: ToastItem) => void> = new Set();

/** Show a toast notification from anywhere */
export function showToast(message: string, type: "success" | "error" | "info" = "info") {
  const toast: ToastItem = { id: ++toastId, message, type };
  listeners.forEach((fn) => fn(toast));
}

export function ToastContainer() {
  const [toasts, setToasts] = useState<ToastItem[]>([]);

  useEffect(() => {
    const handler = (toast: ToastItem) => {
      setToasts((prev) => [...prev, toast]);
      // Auto-dismiss after 3s
      setTimeout(() => {
        setToasts((prev) => prev.filter((t) => t.id !== toast.id));
      }, 3000);
    };
    listeners.add(handler);
    return () => { listeners.delete(handler); };
  }, []);

  if (toasts.length === 0) return null;

  return (
    <div className="toast-container">
      {toasts.map((t) => (
        <div key={t.id} className={`toast toast-${t.type} animate-slide-up`}>
          {t.message}
        </div>
      ))}
    </div>
  );
}

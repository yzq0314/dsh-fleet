import { uiText } from "./i18n";
import {isValidElement} from "react";
import {Choice} from "./Choice";
import { useEffect, useRef, type ReactNode } from "react";
import { X, Layers3, Slash } from "lucide-react";
export function Brand() {
  return (
    <div className="brand">
      <span className="brand-icon">
        <Layers3 size={23} strokeWidth={2} />
      </span>
      DSH <strong>Fleet</strong>
    </div>
  );
}
export function Modal({
  title,
  close,
  children,
}: {
  title: string;
  close: () => void;
  children: ReactNode;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const d = ref.current;
    d?.showModal();
    return () => d?.close();
  }, []);
  return (
    <dialog ref={ref} onCancel={close} aria-label={title}>
      <div className="dialog-head">
        <h2>{title}</h2>
        <button
          type="button"
          className="icon"
          aria-label={uiText("关闭弹窗")}
          onClick={close}
        >
          <X size={18} />
        </button>
      </div>
      {children}
    </dialog>
  );
}
export function Empty({ children }: { children: ReactNode }) {
  return (
    <div className="empty">
      <Slash size={16} />
      {children}
    </div>
  );
}
export function Label({
  name,
  children,
}: {
  name: string;
  children: ReactNode;
}) {
  const Tag=isValidElement(children) && children.type===Choice ? "div" : "label";
  return (
    <Tag className="field">
      <span>{name}</span>
      {children}
    </Tag>
  );
}
export function Section({
  title,
  count,
  action,
}: {
  title: string;
  count?: number;
  action?: ReactNode;
}) {
  return (
    <div className="section-heading">
      <h2>{title}</h2>
      {count != null && <span className="count">{count}</span>}
      <div className="spacer" />
      {action}
    </div>
  );
}
export function displayBytes(bytes: number) {
  return bytes < 1024
    ? bytes + " B"
    : bytes < 1048576
      ? (bytes / 1024).toFixed(1) + " KB"
      : (bytes / 1048576).toFixed(1) + " MB";
}

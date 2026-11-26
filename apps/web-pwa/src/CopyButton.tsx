import { uiText } from "./i18n";
import { useRef, useState } from "react";
import { Check, Copy } from "lucide-react";

export function CopyButton({ text, label, className }: {
  text: string;
  label: string;
  className: string;
}) {
  const [result, setResult] = useState<{ text: string; copied: boolean }>();
  const field = useRef<HTMLTextAreaElement>(null);
  const copied = result?.text === text && result.copied;
  const manual = result?.text === text && !result.copied;

  async function copy() {
    try {
      if (navigator.clipboard?.writeText) {
        await navigator.clipboard.writeText(text);
        setResult({ text, copied: true });
        return;
      }
    } catch { /* The browser can require manual clipboard access. */ }
    const input = field.current;
    if (!input) return;
    const previous = document.activeElement;
    // Keep the selection inside the active dialog, including on HTTP origins.
    input.hidden = false;
    input.focus({ preventScroll: true });
    input.select();
    input.setSelectionRange(0, text.length);
    let success = false;
    try { success = document.execCommand("copy"); } catch { /* Keep the text selected. */ }
    if (success) {
      input.hidden = true;
      if (previous instanceof HTMLElement) previous.focus({ preventScroll: true });
    }
    setResult({ text, copied: success });
  }

  return <>
    <button type="button" className={className} aria-label={copied?uiText("已复制"):label} title={label} onClick={copy}>
      {copied ? <Check size={16} /> : <Copy size={16} />}
      <span>{copied ? uiText("已复制") : label}</span>
    </button>
    <textarea ref={field} hidden={!manual} readOnly value={text}
      aria-label={label + uiText("的文本")} style={{ width: "100%", minWidth: 0 }} />
    {manual && <small role="status">{uiText("长按选中的文字并选择「复制」，或按 Ctrl+C。")}</small>}
  </>;
}

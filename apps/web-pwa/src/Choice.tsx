import { uiText } from "./i18n";
import { Children, isValidElement, useId, useLayoutEffect, useRef, useState, type KeyboardEvent, type ReactNode } from "react";
import { createPortal } from "react-dom";
import { ChevronDown } from "lucide-react";

type Props = { children: ReactNode; popover?: boolean; value?: string; defaultValue?: string; name?: string;
  disabled?: boolean; "aria-label": string; onChange?: (e: {target: {value: string}}) => void };

export function Choice({ children, popover, value, defaultValue, name, disabled, onChange, "aria-label": label }: Props) {
  const options = Children.toArray(children).filter(isValidElement).map(el => {
    const p = el.props as {value?: string; children: ReactNode; disabled?: boolean};
    return {value: String(p.value ?? p.children), label: p.children, disabled: p.disabled};
  });
  const [local, setLocal] = useState(defaultValue);
  const selected = value ?? local ?? options[0]?.value ?? "";
  const [open, setOpen] = useState(false);
  const dialog = useRef<HTMLDialogElement>(null);
  const trigger = useRef<HTMLButtonElement>(null);
  const list = useRef<HTMLDivElement>(null);
  const id = useId();
  const small = !popover && options.length <= 3;
  useLayoutEffect(() => {
    const panel = dialog.current, anchor = trigger.current;
    if (!panel || !anchor || !open || small || disabled) return;
    panel.showModal();
    function position() {
      if (!panel || !anchor) return;
      const rect = anchor.getBoundingClientRect();
      const width = Math.min(innerWidth - 16, Math.max(180, rect.width));
      panel.style.setProperty("--menu-w", width + "px");
      panel.style.setProperty("--menu-x", Math.max(8, Math.min(rect.left, innerWidth - width - 8)) + "px");
      const top = rect.bottom + 4;
      const height = panel.getBoundingClientRect().height;
      panel.style.setProperty("--menu-y", (top + height < innerHeight ? top : Math.max(8, rect.top - height - 4)) + "px");
    }
    position();
    (list.current?.querySelector<HTMLButtonElement>('[aria-selected=true]:not(:disabled)') ??
      list.current?.querySelector<HTMLButtonElement>("button:not(:disabled)"))?.focus({preventScroll:true});
    window.addEventListener("resize", position);
    window.addEventListener("scroll", position, true);
    return () => {
      window.removeEventListener("resize", position);
      window.removeEventListener("scroll", position, true);
      panel.close();
      if (anchor.isConnected) anchor.focus({preventScroll:true});
    };
  }, [open, small, disabled]);
  function navigate(e: KeyboardEvent) {
    if (!["ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight", "Home", "End"].includes(e.key)) return;
    e.preventDefault();
    if (!small && !open) { setOpen(true); return; }
    const items = Array.from(list.current?.querySelectorAll<HTMLButtonElement>("button:not(:disabled)") ?? []);
    const i = items.indexOf(document.activeElement as HTMLButtonElement);
    const step = ["ArrowDown", "ArrowRight"].includes(e.key) ? 1 : -1;
    items[e.key === "Home" ? 0 : e.key === "End" ? items.length - 1 : (i + step + items.length) % items.length]?.focus();
  }
  const choices = <div ref={list} id={id} className="choice-options" role={small ? "group" : "listbox"} aria-label={label}>
    {options.map(o => <button type="button" key={o.value} data-option={o.value} role={small ? "button" : "option"}
      disabled={disabled || o.disabled} aria-pressed={small ? selected === o.value : undefined}
      aria-selected={small ? undefined : selected === o.value}
      onClick={() => { setLocal(o.value); setOpen(false); onChange?.({target:{value:o.value}}); }}>{o.label}</button>)}
  </div>;
  return <div className={"choice " + (small ? "segmented" : "popover-choice")} role="group" aria-label={label} data-value={selected} onKeyDown={navigate}>
    {name && <input type="hidden" name={name} value={selected}/>}
    {small ? choices : <button type="button" className="choice-trigger" ref={trigger} aria-haspopup="listbox"
      aria-controls={open ? id : undefined} aria-expanded={open} disabled={disabled || !options.length} onClick={() => setOpen(true)}>
      <span>{options.find(o => o.value === selected)?.label || uiText("请选择")}</span><ChevronDown size={16}/>
    </button>}
    {!small && open && !disabled && createPortal(<dialog ref={dialog} className="choice-menu" aria-label={label}
      onCancel={e => { e.preventDefault(); setOpen(false); }}
      onClick={e => { if (e.target === e.currentTarget) {
        const rect = e.currentTarget.getBoundingClientRect();
        if (e.clientX < rect.left || e.clientX > rect.right || e.clientY < rect.top || e.clientY > rect.bottom) setOpen(false);
      } }}>
      <div className="choice-sheet-head"><strong>{label}</strong><button type="button" onClick={() => setOpen(false)}>{uiText("取消")}</button></div>
      {choices}
    </dialog>, document.body)}
  </div>;
}

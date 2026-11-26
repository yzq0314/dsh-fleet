import type { Macro } from "./model";

export function matchTags(macro: Macro, tags: Set<string>) {
  return (macro.target_tags ?? []).filter(tag => tags.has(tag));
}

export function orderMacros(macros: Macro[], tags: Set<string>, managing: boolean) {
  return [...macros].sort((a, b) =>
    (managing ? 0 : matchTags(b, tags).length - matchTags(a, tags).length) || a.position - b.position);
}

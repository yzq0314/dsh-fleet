import { uiText } from "./i18n";
export const blockNames={overview:"集群状态",topology:"设备拓扑",devices:"设备节点",hardware:"只读硬件监控",services:"快捷服务"};
export type Block=keyof typeof blockNames;
export const blocks=Object.keys(blockNames) as Block[];
export const skins={industrial:"工业暗黑",light:"极简浅色",midnight:"午夜蓝",oled:"OLED 纯黑",glass:"磨砂玻璃",paper:"暖白纸感"};
export const accents={薄荷绿:"#a5ecc4",琥珀金:"#f4c382",冰蓝:"#9eceff",赛博紫:"#be9aff"};
export type Theme={version:1;theme:keyof typeof skins;accent:string;radius:4|10|16;density:"compact"|"comfortable";font:"system"|"mono";
 motion:boolean; glow:boolean; blur:number;shade:number;wallpaper:boolean;css:string;layout:Record<"mobile"|"desktop",Block[]>;shortcuts:Record<string,string[]>};
export const defaults:Theme={version:1,theme:"industrial",accent:accents.薄荷绿,radius:10,density:"comfortable",font:"system",
 motion:true,glow:true,blur:12,shade:50,wallpaper:false,css:"",layout:{desktop:[...blocks],mobile:[...blocks]},shortcuts:{}};
export function foreground(hex:string){
  const [r,g,b]=[1,3,5].map(i=>parseInt(hex.slice(i,i+2),16)/255).map(c=>c<=.04045?c/12.92:((c+.055)/1.055)**2.4);
  return .2126*r+.7152*g+.0722*b>.179?"#000000":"#ffffff";
}
export function palette(css:string){
  const out:Record<string,string>={};
  for(const line of css.split(";").filter(s=>s.trim())){
    const match=/^\s*(--(?:panel|layer|line|text|mute))\s*:\s*(#[\da-f]{6})\s*$/i.exec(line);
    if(!match)throw Error(uiText("CSS 仅支持 --panel、--layer、--line、--text、--mute 与六位 Hex 颜色"));
    out[match[1]]=match[2];
  }
  return out;
}
export function exportTheme(theme:Theme){return JSON.stringify(theme,null,2);}
export function importTheme(raw:string):Theme{
  const v=JSON.parse(raw);
  const fail=()=>{throw Error(uiText("主题包格式或取值无效"));};
  if(!v||v.version!==1||!Object.hasOwn(skins,v.theme)||!/^#[\da-f]{6}$/i.test(v.accent)||
    ![4,10,16].includes(v.radius)||!["compact","comfortable"].includes(v.density)||!["system","mono"].includes(v.font)||
    !["motion","glow","wallpaper"].every(k=>typeof v[k]==="boolean")||
    !Number.isInteger(v.blur)||v.blur<0||v.blur>32||!Number.isInteger(v.shade)||v.shade<0||v.shade>90||
    typeof v.css!=="string"||v.css.length>512||!v.layout||!v.shortcuts||Array.isArray(v.shortcuts)||typeof v.shortcuts!=="object"||
    Object.keys(v).some(k=>!Object.hasOwn(defaults,k)))fail();
  for(const k of ["desktop","mobile"]){
    const arr=v.layout[k];if(!Array.isArray(arr)||new Set(arr).size!==arr.length||arr.some(b=>!blocks.includes(b)))fail();
  }
  for(const [k,a] of Object.entries(v.shortcuts)){
    if(!/^[\w.-]{1,100}$/.test(k)||!Array.isArray(a)||!a.length||a.length>2||a.some(s=>typeof s!=="string"||!/^(workbench|(?:service|macro|volume):[\w.-]{1,100})$/.test(s)))fail();
  }
  palette(v.css);return v;
}
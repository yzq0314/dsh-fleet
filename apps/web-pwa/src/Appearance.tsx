import { uiText } from "./i18n";
import {createContext,useContext,useEffect,useState,type ReactNode} from "react";
import {api,errorText} from "./model";
import {defaults,foreground,palette,type Theme} from "./theme";

const Context=createContext({theme:defaults,save:async(_:Theme)=>{},wallVersion:0,refreshWallpaper:()=>{}});
export const useAppearance=()=>useContext(Context);
export function AppearanceProvider({children}:{children:ReactNode}){
  const [theme,setTheme]=useState<Theme>(defaults);
  const [fault,setFault]=useState("");
  const [wallVersion,refresh]=useState(0);
  const [ready,setReady]=useState(false);
  async function save(value:Theme){setTheme(await api<Theme>("/ui-preferences","PUT",value));}
  useEffect(()=>{
    let active=true;
    async function load(){
      const url=new URL(location.href);const reset=url.searchParams.get("reset_theme")==="1";
      const value=reset?await api<Theme>("/ui-preferences","PUT",defaults):await api<Theme>("/ui-preferences");
      if(active){setTheme(value);setReady(true);if(reset){url.searchParams.delete("reset_theme");history.replaceState(null,"",url);}}
    }
    load().catch(e=>{if(active)setFault(errorText(e));});return ()=>{active=false;};
  },[]);
  useEffect(()=>{
    const el=document.documentElement;
    Object.assign(el.dataset,{theme:theme.theme,density:theme.density,font:theme.font,motion:String(theme.motion),glow:String(theme.glow)});
    const tokens:Record<string,string>={"--accent":theme.accent,"--on-accent":foreground(theme.accent),"--radius":theme.radius+"px","--glass-blur":theme.blur+"px",
      "--shade":String(theme.shade/100),"--wallpaper":theme.wallpaper?'url("/api/ui-preferences/wallpaper?v='+wallVersion+'")':"none",...palette(theme.css)};
    for(const [name,value] of Object.entries(tokens))el.style.setProperty(name,value);
    return ()=>{for(const k of Object.keys(tokens))el.style.removeProperty(k);for(const k of ["theme","density","font","motion","glow"])delete el.dataset[k];};
  },[theme,wallVersion]);
  if(fault)return <main className="login"><p>{fault}</p><button onClick={()=>location.reload()}>{uiText("重试")}</button></main>;
  return <Context.Provider value={{theme,save,wallVersion,refreshWallpaper:()=>refresh(v=>v+1)}}>{ready&&children}</Context.Provider>;
}
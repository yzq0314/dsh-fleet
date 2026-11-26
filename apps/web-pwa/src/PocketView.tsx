import { uiText } from "./i18n";
import { useEffect, useState } from "react";
import { ArrowLeft, ChevronDown, ChevronUp, ExternalLink, RotateCw } from "lucide-react";
import { Choice } from "./Choice";
import { createPortal } from "react-dom";
import { api, type NodeInfo } from "./model";

export function PocketView({nodes, selected, close}: {nodes: NodeInfo[]; selected: string; close: () => void}) {
  const [id, setId] = useState(selected);
  const [gen, refresh] = useState(0);
  const [hidden, collapse] = useState(false);
  const [entry, setEntry] = useState("");
  const [problem, setProblem] = useState("");
  const [loaded, setLoaded] = useState(false);
  const node = nodes.find(n => n.id === id);
  const ready = !!(node?.online && node.pocket_embed && node.health?.pocket_status === "ready");
  useEffect(() => {
    let active = true;
    setEntry(""); setLoaded(false); setProblem("");
    if(ready) api<{entry:string}>("/pocket/"+encodeURIComponent(id)+"/entry","POST").then(r=>{
      const u = new URL(r.entry);
      if (u.hostname === location.hostname || u.protocol !== location.protocol || u.port !== location.port ||
          u.username || u.password || !u.pathname.startsWith("/.fleet/enter/")) throw Error(uiText("同屏访问地址未隔离"));
      if(active) setEntry(u.href);
    }).catch(()=>{if(active)setProblem(uiText("同屏入口暂时不可用"));});
    return ()=>{active=false;};
  },[id,gen,ready]);
  useEffect(() => {
    const previous = document.body.style.overflow;
    document.body.style.overflow = "hidden";
    return () => {document.body.style.overflow = previous;};
  },[]);
  return createPortal(<section className="pocket-view" role="dialog" aria-label={uiText("DSH 实时同屏")}>
    <header className={hidden ? "pocket-hidden" : ""}>
      {!hidden && <><button onClick={close} aria-label={uiText("返回 Fleet")}><ArrowLeft size={15}/> Fleet</button>
        <Choice popover value={id} aria-label={uiText("切换同屏节点")} onChange={e=>{setEntry("");setId(e.target.value);}}>
          {nodes.filter(n=>n.id===id || (n.online && n.pocket_embed && n.health?.pocket_status==="ready")).map(n=><option key={n.id} value={n.id}>{n.alias}</option>)}
        </Choice>
        <i className={"dot "+(ready && loaded ? "" : "off")} title={loaded ? uiText("页面已加载") : uiText("等待页面")}/>
        <button aria-label={uiText("刷新同屏")} onClick={()=>refresh(gen+1)}><RotateCw size={15}/></button>
        <a href={"/pocket/"+encodeURIComponent(id)+"/"} target="_blank" rel="noopener noreferrer" aria-label={uiText("新窗口打开同屏")}><ExternalLink size={15}/></a></>}
      <button aria-label={hidden?uiText("展开同屏顶栏"):uiText("折叠同屏顶栏")} onClick={()=>collapse(!hidden)}>{hidden?<ChevronDown size={15}/>:<ChevronUp size={15}/>}</button>
    </header>
    {ready && entry ? <iframe key={entry} title={"DSH · "+node?.alias} src={entry} onLoad={()=>setLoaded(true)}
      referrerPolicy="no-referrer" /> : <p>{problem || (ready ? uiText("正在连接") : uiText("节点同屏服务暂不可用"))}</p>}
  </section>,document.body);
}

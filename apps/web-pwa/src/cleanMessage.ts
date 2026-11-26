/** Keep examples inside Markdown fences intact. The source events remain unmodified. */
export function cleanMessage(text:string):string {
  let fence="", hidden="";
  const out:string[]=[];
  for(let line of text.split(/\r?\n/)){
    const delimiter=line.match(/^\s*(`{3,}|~{3,})/)?.[1];
    if(!hidden && (fence || delimiter)){
      out.push(line);
      if(fence && delimiter?.startsWith(fence))fence="";
      else if(!fence && delimiter)fence=delimiter;
      continue;
    }
    let visible="";
    while(line){
      if(hidden){
        const end=line.toLowerCase().indexOf("</"+hidden+">");
        if(end<0)break;
        line=line.slice(end+hidden.length+3);hidden="";continue;
      }
      const tag=/<(system-reminder|available_skills)\b[^>]*>/i.exec(line);
      if(!tag){visible+=line;break;}
      visible+=line.slice(0,tag.index);hidden=tag[1].toLowerCase();line=line.slice(tag.index+tag[0].length);
    }
    if(!/^\s*(?:Time sampled while preparing turn\b|Browser time zone for this request\b|Elapsed since the preceding\b)/i.test(visible))out.push(visible);
  }
  return out.join("\n").trim();
}
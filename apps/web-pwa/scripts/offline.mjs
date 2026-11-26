import { readFile, readdir, writeFile } from "node:fs/promises";
import { createHash } from "node:crypto";
const files = [
  "/index.html",
  "/icon.svg",
  "/manifest.json",
  "/icon-192.png",
  "/icon-512.png",
  ...(await readdir("dist/assets")).map((n) => "/assets/" + n),
];
const hash = createHash("sha256");
for (const name of files) hash.update(await readFile("dist" + name));
const name = "dsh-fleet-shell-" + hash.digest("hex").slice(0, 16);
const code = `const version=${JSON.stringify(name)}, files=${JSON.stringify(files)};
self.addEventListener('install',event=>event.waitUntil(caches.open(version).then(c=>c.addAll(files)).then(()=>self.skipWaiting())));
self.addEventListener('activate',event=>event.waitUntil(caches.keys().then(keys=>Promise.all(keys.filter(x=>x.startsWith('dsh-fleet-shell-')&&x!==version).map(x=>caches.delete(x)))).then(()=>self.clients.claim())));
self.addEventListener('fetch',event=>{
const url=new URL(event.request.url);
if(event.request.method!=='GET'||url.origin!==self.location.origin||url.pathname.startsWith('/api/'))return;
if(event.request.mode==='navigate')event.respondWith(fetch(event.request).catch(()=>caches.open(version).then(c=>c.match('/index.html'))));
else if(files.includes(url.pathname))event.respondWith(caches.open(version).then(c=>c.match(url.pathname)).then(r=>r||fetch(event.request)));
});`;
await writeFile("dist/sw.js", code);

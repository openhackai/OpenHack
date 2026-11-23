import { posix } from "node:path";
import type { Project } from "./project";

/** Static navigation leads, not proof of runtime reachability. Every edge stays inside the snapshot. */
export function buildSourceGraph(project: Project) {
  const imports = new Map<string, Set<string>>(), callers = new Map<string, Set<string>>();
  const routes = new Map<string, {method:string;line:number}[]>();
  for (const [path, source] of project.files) {
    const targets = new Set<string>();
    for (const m of source.matchAll(/(?:from\s*|require\s*\(\s*|import\s*\(\s*|import\s*)["']([^"']+)["']/g)) {
      const spec = m[1]!;
      const bases = spec.startsWith(".") ? [posix.normalize(posix.join(posix.dirname(path),spec))] : spec.startsWith("@/") ? [spec.slice(2),"src/"+spec.slice(2)] : [];
      for (const base of bases) {
        const target = [base,...[".ts",".tsx",".js",".jsx","/index.ts","/index.tsx","/index.js"].map(s=>base+s)].find(p=>project.files.has(p));
        if (!target) continue;
        targets.add(target); const set=callers.get(target)??new Set<string>();set.add(path);callers.set(target,set);break;
      }
    }
    imports.set(path,targets);
    const entries: {method:string;line:number}[]=[];
    source.split("\n").forEach((line,i)=>{
      const m=line.match(/export\s+(?:async\s+)?function\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b|export\s+const\s+(GET|POST|PUT|PATCH|DELETE|HEAD|OPTIONS)\b/);
      if(m)entries.push({method:m[1]??m[2]!,line:i+1});
    });
    if(entries.length)routes.set(path,entries);
  }
  return {
    summary:{files:project.files.size,importEdges:[...imports.values()].reduce((n,s)=>n+s.size,0),routeHandlers:[...routes.values()].reduce((n,s)=>n+s.length,0)},
    neighbors(path:string,offset=0){
      if(!project.files.has(path))return {error:"Path is outside the source snapshot."};
      const edges=[...[...(imports.get(path)??[])].map(file=>({kind:"imports",file})),...[...(callers.get(path)??[])].map(file=>({kind:"imported_by",file}))];
      return {path,edges:edges.slice(offset,offset+100),total:edges.length,nextOffset:offset+100<edges.length?offset+100:null,routes:routes.get(path)??[],note:"Static leads; verify guards, callers and reachability in source."};
    },
  };
}

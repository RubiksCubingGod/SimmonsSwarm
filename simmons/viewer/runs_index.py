"""Static index over every run under runs/ and runs-local/."""

from __future__ import annotations

import hashlib
import html
import json
import os
from collections.abc import Callable, Iterable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..commands.ls import _row
from ..dataset import _uses_fake
from ..runner import _forked_run_ids, _iter_run_metadata, _RunMetadata
from . import _STYLE, _recorded_models, _safe_json, generate

_PROMPT_SLOTS = (
    ("p1", "sender", "P₁"),
    ("p2", "guard", "P₂"),
    ("plain", "plain_sender", "Plain"),
)
_INDEX_MODEL_ROLES = ("sender", "receiver", "guard", "plain_sender")


def _prompt_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _rate(summary: Mapping[str, Any], key: str) -> dict[str, Any] | None:
    rates = summary.get("rates")
    value = rates.get(key) if isinstance(rates, Mapping) else None
    if not isinstance(value, Mapping):
        return None
    return {name: value.get(name) for name in ("value", "num", "den")}


def _read_summary(run_dir: Path) -> dict[str, Any]:
    try:
        value = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _ensure_view(item: _RunMetadata, status: str) -> tuple[Path | None, str | None]:
    """Return the run's view page, generating it when missing."""
    view = item.path / "view" / "index.html"
    if view.is_file():
        return view, None
    if status == "running":
        return None, "Run in progress; view not generated yet."
    try:
        index, _ = generate(item.path)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        return None, f"View could not be generated: {exc}"
    return index, None


def _run_prompts(
    item: _RunMetadata, prompt_table: dict[str, dict[str, Any]]
) -> dict[str, dict[str, str] | None]:
    result: dict[str, dict[str, str] | None] = {}
    for slot, role, _label in _PROMPT_SLOTS:
        path = item.path / "inputs" / "prompts" / f"{role}.md"
        try:
            text = path.read_text(encoding="utf-8")
        except OSError:
            result[slot] = None
            continue
        digest = _prompt_hash(text)
        name = Path(str(item.config.get(f"{role}_prompt", role))).stem
        entry = prompt_table.setdefault(digest, {"text": text, "names": [], "slots": []})
        if name not in entry["names"]:
            entry["names"].append(name)
        if slot not in entry["slots"]:
            entry["slots"].append(slot)
        result[slot] = {"hash": digest, "name": name}
    return result


def _model_labels(item: _RunMetadata) -> dict[str, str | None]:
    models = _recorded_models(item.run, [], item.config)
    labels: dict[str, str | None] = {}
    for role in _INDEX_MODEL_ROLES:
        value = models.get(role)
        if isinstance(value, Mapping) and value.get("provider") and value.get("model"):
            labels[role] = f"{value['provider']}:{value['model']}"
        elif isinstance(value, Mapping) and value.get("configured"):
            labels[role] = str(value["configured"])
        else:
            labels[role] = None
    return labels


def _is_fake(item: _RunMetadata) -> bool:
    try:
        return _uses_fake(item.config)
    except (ValueError, TypeError):
        return False


ViewFor = Callable[[_RunMetadata, str], tuple[Path | None, str | None]]


def _index_data(
    roots: Iterable[Path],
    output: Path,
    *,
    include: Callable[[_RunMetadata], bool] | None = None,
    view_for: ViewFor = _ensure_view,
) -> dict[str, Any]:
    resolved_roots = [root.resolve() for root in roots]
    metadata = _iter_run_metadata(resolved_roots)
    forked = _forked_run_ids(metadata)
    prompt_table: dict[str, dict[str, Any]] = {}
    runs: list[dict[str, Any]] = []
    for item in metadata:
        if include is not None and not include(item):
            continue
        row = _row(item, forked=forked)
        summary = _read_summary(item.path)
        decode = summary.get("decode") if isinstance(summary.get("decode"), Mapping) else {}
        view, view_error = view_for(item, row["status"])
        root = next((r for r in resolved_roots if item.path.resolve().is_relative_to(r)), None)
        runs.append(
            {
                "experiment": row["experiment"],
                "run_id": row["run_id"],
                "started_at": item.run.get("started_at"),
                "author": row["author"],
                "status": row["status"],
                "fake": _is_fake(item),
                "forked": row["forked"],
                "root": root.name if root is not None else "",
                "episodes_done": row["episodes_done"],
                "episodes_planned": item.run.get("episodes_planned"),
                "models": _model_labels(item),
                "rates": {
                    "hit": _rate(summary, "hit_rate"),
                    "catch": _rate(summary, "catch_rate"),
                    "false_alarm": _rate(summary, "false_alarm_rate"),
                },
                "decode_gap": decode.get("gap") if isinstance(decode, Mapping) else None,
                "refusals": (summary.get("counts") or {}).get("refusals", 0),
                "spent_usd": row["spent_usd"],
                "href": (
                    Path(os.path.relpath(view.resolve(), output.parent.resolve())).as_posix()
                    if view is not None
                    else None
                ),
                "view_error": view_error,
                "prompts": _run_prompts(item, prompt_table),
            }
        )
    runs.sort(key=lambda run: (str(run["started_at"] or ""), run["run_id"]), reverse=True)
    for digest, entry in prompt_table.items():
        entry["runs"] = sum(
            1
            for run in runs
            if any(p is not None and p["hash"] == digest for p in run["prompts"].values())
        )
    return {
        "page": "runs",
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "roots": [root.name for root in resolved_roots],
        "slots": [{"key": slot, "label": label} for slot, _role, label in _PROMPT_SLOTS],
        "prompts": prompt_table,
        "runs": runs,
    }


_INDEX_STYLE = (
    "main{max-width:1440px}.models div,.chip,.tag{white-space:nowrap}.run-cell a{font-size:14px;white-space:nowrap}"
    ".run-cell strong{display:block}.run-cell a{color:var(--cyan);text-decoration:none;overflow-wrap:anywhere}"
    ".run-cell a:hover{text-decoration:underline}.models{font-size:13px;color:var(--muted);line-height:1.45}"
    ".models b{color:var(--text);font-weight:600}.tag{display:inline-block;font-size:12px;border:1px solid var(--line);"
    "border-radius:999px;padding:1px 8px;margin:2px 4px 2px 0;color:var(--muted)}.tag.real{color:var(--green);border-color:#2f6b49}"
    ".tag.fake{color:var(--gold);border-color:#6d5a2c}.tag.refused{color:var(--purple);border-color:#5b4683}.chip{display:flex;gap:6px;align-items:baseline;min-height:30px;"
    "padding:3px 9px;margin:3px 0;font-size:13px;border-radius:7px;text-align:left;width:100%}"
    ".chip code{font:12px ui-monospace,SFMono-Regular,monospace}.chip .slot{color:var(--muted);min-width:38px}"
    ".chip[aria-expanded=\"true\"]{box-shadow:inset 0 -2px currentColor}td small{display:block;color:var(--muted);font-size:12px}"
    "tr.prompt-row td{background:#101722}tr.prompt-row pre{margin:6px 0 0}td{vertical-align:top}"
    ".prompt-list details{border-top:1px solid var(--line);padding:8px 0}.prompt-list summary{cursor:pointer}"
    ".prompt-list summary code{font:13px ui-monospace,SFMono-Regular,monospace;font-weight:650}"
    ".prompt-list pre{margin:8px 0 0}.prompt-groups{display:grid;grid-template-columns:repeat(auto-fit,minmax(320px,1fr));gap:14px}"
)

_INDEX_SCRIPT = r"""
(()=>{
const data=JSON.parse(document.getElementById("viewer-data").textContent);
const root=document.getElementById("app");
const el=(tag,text="",cls="")=>{const n=document.createElement(tag);if(text!==null)n.textContent=String(text);if(cls)n.className=cls;return n};
const add=(parent,tag,text="",cls="")=>{const n=el(tag,text,cls);parent.append(n);return n};
const show=(v)=>v===null||v===undefined||v===""?"—":String(v);
const pct=(v)=>v===null||v===undefined?"—":`${(Number(v)*100).toFixed(1)}%`;
const money=(v)=>v===null||v===undefined?"—":`$${Number(v).toFixed(2)}`;
const short=(h)=>String(h).slice(0,8);
const hue=(h)=>parseInt(String(h).slice(0,6),16)%360;
const tint=(node,h)=>{node.style.color=`hsl(${hue(h)} 75% 74%)`;node.style.borderColor=`hsl(${hue(h)} 45% 42%)`};
const slotLabel=Object.fromEntries(data.slots.map(s=>[s.key,s.label]));
const runs=data.runs||[];
const pageHead=add(root,"header","","top");const left=add(pageHead,"div");add(left,"div","SIMMONS SWARM","eyebrow");add(left,"h1","All runs");add(left,"div",`${runs.length} runs · ${data.roots.map(r=>`${r}/`).join(" and ")} · generated ${data.generated_at}`,"muted");
const promptCard=add(root,"section","","card prompt-list");add(promptCard,"h2","Prompts in use");add(promptCard,"p","Each distinct prompt text, by slot. The same hash means the same text, whatever the file was called.","muted small");const groups=add(promptCard,"div","","prompt-groups");
const promptHint={p1:"P₁ · shared by sender and receiver",p2:"P₂ · guard",plain:"Plain sender"};
for(const slot of data.slots){const group=add(groups,"div");add(group,"h3",promptHint[slot.key]||slot.label);const entries=Object.entries(data.prompts).filter(([,p])=>p.slots.includes(slot.key)).sort((a,b)=>b[1].runs-a[1].runs);if(!entries.length)add(group,"p","No snapshot found.","muted small");for(const [h,p] of entries){const d=add(group,"details");const s=add(d,"summary");const code=add(s,"code",short(h));tint(code,h);add(s,"span",` ${p.names.join(", ")} · ${p.runs} run${p.runs===1?"":"s"}`);add(d,"pre",p.text||"(empty)");}}
const section=add(root,"section","","card");add(section,"h2","Runs");const toolbar=add(section,"div","","toolbar");
const expLabel=add(toolbar,"label","Experiment");const exp=add(expLabel,"select");exp.setAttribute("aria-label","Filter by experiment");for(const v of ["all",...[...new Set(runs.map(r=>r.experiment))].sort()]){const o=add(exp,"option",v==="all"?"All experiments":v);o.value=v;}
const promptLabel=add(toolbar,"label","Prompt");const promptSelect=add(promptLabel,"select");promptSelect.setAttribute("aria-label","Filter by prompt");promptSelect.style.width="260px";add(promptSelect,"option","Any prompt").value="all";for(const slot of data.slots){for(const [h,p] of Object.entries(data.prompts).filter(([,p])=>p.slots.includes(slot.key))){const o=add(promptSelect,"option",`${slot.label} · ${p.names.join(", ")} · ${short(h)}`);o.value=`${slot.key}:${h}`;}}
const count=add(toolbar,"span","","muted");count.style.minWidth="90px";
const wrap=add(section,"div","","table-wrap");const table=add(wrap,"table");const tr=add(add(table,"thead"),"tr");for(const [label,num] of [["Run",0],["Status",0],["Models",0],["Hit",1],["Catch",1],["False alarm",1],["Decode gap",1],["Spend",1],["Prompts",0]]){const th=add(tr,"th",label);if(num)th.className="num";}
const body=add(table,"tbody");
function rateCell(row,rate){const td=add(row,"td",pct(rate?.value),"num");if(rate&&rate.den!=null)add(td,"small",`${rate.num} / ${rate.den}`);}
function update(){
 const [pslot,phash]=promptSelect.value==="all"?[null,null]:promptSelect.value.split(":");
 const rows=runs.filter(r=>(exp.value==="all"||r.experiment===exp.value)&&(!pslot||r.prompts[pslot]?.hash===phash));
 body.replaceChildren();
 for(const r of rows){
  const row=add(body,"tr");const run=add(row,"td","","run-cell");add(run,"strong",r.experiment);if(r.href){const a=add(run,"a",r.run_id);a.href=r.href;}else add(run,"span",r.run_id);add(run,"small",`${show(r.started_at).replace("T"," ").replace(/\.\d+Z$/,"Z")} · ${r.author}`);if(r.view_error)add(run,"small",r.view_error);
  const status=add(row,"td");add(status,"div",r.status);add(status,"span",r.fake?"fake":"real",`tag ${r.fake?"fake":"real"}`);if(r.root)add(status,"span",`${r.root}/`,"tag");if(r.forked)add(status,"span","fork","tag");if(r.refusals)add(status,"span",`${r.refusals} refused`,"tag refused");add(status,"small",`${show(r.episodes_done)} / ${show(r.episodes_planned)} episodes`);
  const models=add(row,"td","","models");for(const [role,label] of [["sender","S"],["receiver","R"],["guard","G"],["plain_sender","Plain"]]){const line=add(models,"div");add(line,"b",`${label} `);line.append(show(r.models?.[role]));}
  rateCell(row,r.rates.hit);rateCell(row,r.rates.catch);rateCell(row,r.rates.false_alarm);add(row,"td",pct(r.decode_gap),"num");add(row,"td",money(r.spent_usd),"num");
  const prompts=add(row,"td");const detail=el("tr","","prompt-row");detail.hidden=true;const detailCell=add(detail,"td");detailCell.colSpan=9;
  for(const slot of data.slots){const p=r.prompts[slot.key];const chip=add(prompts,"button","","chip");chip.type="button";add(chip,"span",slot.label,"slot");if(!p){add(chip,"span","—");chip.disabled=true;continue;}const code=add(chip,"code",short(p.hash));tint(chip,p.hash);add(chip,"span",p.name);chip.setAttribute("aria-expanded","false");chip.title=`Show the ${slot.label} prompt text`;
   chip.addEventListener("click",()=>{const open=chip.getAttribute("aria-expanded")==="true";for(const b of prompts.querySelectorAll("button"))b.setAttribute("aria-expanded","false");if(open){detail.hidden=true;return;}chip.setAttribute("aria-expanded","true");detailCell.replaceChildren();add(detailCell,"strong",`${promptHint[slot.key]||slot.label} · ${p.name} · ${short(p.hash)}`);add(detailCell,"pre",data.prompts[p.hash]?.text??"(missing)");detail.hidden=false;});}
  body.append(detail);
 }
 count.textContent=`${rows.length} of ${runs.length} runs`;
}
exp.addEventListener("change",update);promptSelect.addEventListener("change",update);update();
})();
"""


def render_index_page(data: Mapping[str, Any]) -> str:
    title = html.escape("Simmons runs", quote=True)
    return (
        "<!doctype html>\n<html lang=\"en\"><head><meta charset=\"utf-8\">"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">"
        f"<title>{title}</title><style>{_STYLE}{_INDEX_STYLE}</style></head>"
        "<body><main id=\"app\"></main>"
        f"<script id=\"viewer-data\" type=\"application/json\">{_safe_json(data)}</script>"
        f"<script>{_INDEX_SCRIPT}</script></body></html>\n"
    )


def generate_index(
    roots: Iterable[Path],
    output: Path,
    *,
    include: Callable[[_RunMetadata], bool] | None = None,
    view_for: ViewFor = _ensure_view,
) -> tuple[Path, dict[str, Any]]:
    """Write one page listing every run under ``roots``, generating missing run views.

    ``include`` limits the listing to selected runs; ``view_for`` returns each
    listed run's page (or an error note) in place of the run's own ``view/``.
    """
    output.parent.mkdir(parents=True, exist_ok=True)
    data = _index_data(roots, output, include=include, view_for=view_for)
    output.write_text(render_index_page(data), encoding="utf-8")
    return output, data


__all__ = ["generate_index", "render_index_page"]

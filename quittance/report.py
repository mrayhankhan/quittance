"""The exception queue: a screen an analyst can actually work.

A match rate is a number in a terminal. The exception list is the thing someone
has to sit down and clear on a Tuesday morning, so it gets the same care as the
matching engine.

Every exception carries three things a reviewer needs and most tools omit: the
exact rupee gap, the rule or layer that produced the verdict, and what to
actually do about it. An exception that says "unexplained" and nothing else has
moved the work, not done it.

Stdlib only -- no template engine, no bundler, no runtime dependencies. The
report is a single self-contained HTML file you can email to a CA.
"""

from __future__ import annotations

import html
from collections import Counter
from datetime import datetime

from .money import inr
from .pipeline import Report
from .schema import ExceptionCode, Layer

#: What a reviewer should actually do. An exception queue without next actions
#: is a list of complaints.
NEXT_ACTION = {
    ExceptionCode.NARRATION_TRUNCATED:
        "Pull the full advice from the bank portal — the UTR is usually recoverable "
        "there even when the statement feed truncates it.",
    ExceptionCode.DUPLICATE_UTR:
        "Two credits carry the same UTR. Ask the bank which settlement each one "
        "belongs to; do not guess from the amounts.",
    ExceptionCode.UNEXPLAINED:
        "Arithmetic does not close. Check for a rolling reserve hold, an unrecorded "
        "adjustment, or a settlement that spans the cycle boundary.",
    ExceptionCode.ITC_NOT_IN_2B:
        "Supplier has not filed. Chase Razorpay for the invoice and do NOT claim this "
        "period — claiming ITC absent from 2B invites a notice.",
    ExceptionCode.ITC_AMOUNT_MISMATCH:
        "Claim the lower of the two figures, then raise the difference with the "
        "supplier before the Sec 16(4) window closes.",
    ExceptionCode.ROUNDING_DRIFT:
        "Expected drift between per-row and monthly-aggregate GST rounding. Book the "
        "invoice figure.",
    ExceptionCode.MISSING_ORDER:
        "Settlement row has no matching order. Check whether the order was created "
        "outside the merchant system.",
    ExceptionCode.AMOUNT_MISMATCH:
        "Order value and captured payment disagree. Look for a partial capture, or a "
        "discount applied after the order was written.",
    ExceptionCode.DUPLICATE_PAYMENT:
        "The same order settled twice. Confirm whether the customer was double-charged "
        "and refund immediately if so — this one is urgent.",
    ExceptionCode.LATE_REFUND:
        "Refund deducted in a later cycle than it was initiated. Match to the "
        "originating payment, not the settlement date.",
    ExceptionCode.CHARGEBACK_REVERSAL:
        "Chargeback debited weeks after the sale. Tie to the original payment ID.",
    ExceptionCode.FX_CONVERSION:
        "International payment. Reconcile at the settled INR value, not the "
        "transaction currency.",
    ExceptionCode.UNSETTLED:
        "Row is not yet settled. Expect it in a later cycle; no action today.",
}

SEVERITY = {
    ExceptionCode.UNEXPLAINED: "critical",
    ExceptionCode.DUPLICATE_PAYMENT: "critical",
    ExceptionCode.DUPLICATE_UTR: "critical",
    ExceptionCode.ITC_NOT_IN_2B: "warning",
    ExceptionCode.ITC_AMOUNT_MISMATCH: "warning",
    ExceptionCode.AMOUNT_MISMATCH: "warning",
    ExceptionCode.MISSING_ORDER: "warning",
}


def _e(text: object) -> str:
    return html.escape(str(text))


def render_html(report: Report) -> str:
    """Build the self-contained report page."""
    ex_rows = "\n".join(_exception_row(i, e) for i, e in enumerate(report.exceptions))
    counts = Counter(e.code.value for e in report.exceptions)
    filters = "\n".join(
        f'<button class="chip" data-filter="{_e(code)}">{_e(code)} '
        f'<span class="n">{n}</span></button>'
        for code, n in counts.most_common()
    )
    fm = len(report.false_matches)

    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Quittance — exception queue</title>
<style>{_CSS}</style>
</head><body>
<header>
  <div class="brand">QUITTANCE</div>
  <div class="sub">settlement reconciliation · seed {report.dataset_seed} ·
    engine {_e(report.engine)} · {report.elapsed_ms:.0f} ms ·
    generated {datetime.now().astimezone():%d %b %Y %H:%M %Z}</div>
</header>

<section class="tiles">
  {_tile("Match rate", f"{report.match_rate * 100:.1f}%",
         f"{len(report.matches)} of {report.bank_lines} credits", "ok")}
  {_tile("False matches", str(fm), "answer-key verified",
         "ok" if fm == 0 else "critical")}
  {_tile("Unexplained", inr(report.unexplained_amount),
         f"{len(report.exceptions)} exceptions", "warning")}
  {_tile("ITC at risk", inr(report.itc_at_risk), "input tax credit", "warning")}
  {_tile("Order coverage",
         f"{report.orders.coverage * 100:.2f}%" if report.orders else "—",
         f"{report.orders.tied} of {report.orders.payment_rows} rows tied"
         if report.orders else "not run", "ok")}
</section>

<section>
  <h2>Where the matching happened</h2>
  <div class="layers">{_layers(report)}</div>
  <p class="note">Layers 0 and 1 are deterministic. The model proposes only what
  they decline, and Layer 3 recomputes every proposal before it can become a match.</p>
</section>

<section>
  <h2>Exception queue <span class="count">{len(report.exceptions)}</span></h2>
  <div class="toolbar">
    <button class="chip active" data-filter="all">all <span class="n">{len(report.exceptions)}</span></button>
    {filters}
    <span class="spacer"></span>
    <button class="chip" id="hide-done">hide reviewed</button>
    <button class="chip" id="export">export decisions</button>
  </div>
  <p class="note">Click a row to expand. <kbd>j</kbd>/<kbd>k</kbd> to move,
  <kbd>r</kbd> to mark reviewed. Decisions persist in this browser and export as JSON.</p>
  <div class="queue">{ex_rows or '<p class="empty">Nothing unresolved.</p>'}</div>
</section>

{_rejections(report)}
{_itc(report)}
{_audit(report)}

<footer>Quittance · every match carries the rule that produced it</footer>
<script>{_JS}</script>
</body></html>
"""


def _tile(label: str, value: str, sub: str, tone: str) -> str:
    return (f'<div class="tile {tone}"><div class="k">{_e(label)}</div>'
            f'<div class="v">{_e(value)}</div><div class="s">{_e(sub)}</div></div>')


def _layers(report: Report) -> str:
    labels = {
        Layer.EXACT.value: ("L0 · exact identifier", "no AI"),
        Layer.SOLVER.value: ("L1 · constrained solver", "no AI"),
        Layer.LLM.value: ("L2 · model, verified by L3", "AI"),
    }
    out = []
    for key, (label, tag) in labels.items():
        n = report.by_layer[key]
        pct = n / report.bank_lines * 100 if report.bank_lines else 0
        cls = "ai" if tag == "AI" else "noai"
        out.append(
            f'<div class="layer"><div class="ll">{_e(label)}'
            f'<span class="tag {cls}">{_e(tag)}</span></div>'
            f'<div class="bar"><i style="width:{pct:.1f}%"></i></div>'
            f'<div class="lv">{n} · {pct:.1f}%</div></div>'
        )
    return "\n".join(out)


def _exception_row(i: int, e) -> str:
    sev = SEVERITY.get(e.code, "info")
    action = NEXT_ACTION.get(e.code, "Review manually.")
    amount = inr(e.amount) if e.amount else "—"
    layer = f' · raised at {_e(e.layer.value)}' if e.layer else ""
    return f"""<article class="row {sev}" data-code="{_e(e.code.value)}" data-id="{_e(e.ref)}" tabindex="0">
  <div class="head">
    <span class="sev"></span>
    <span class="ref">{_e(e.ref)}</span>
    <span class="code">{_e(e.code.value)}</span>
    <span class="amt">{_e(amount)}</span>
    <span class="done">reviewed</span>
  </div>
  <div class="body">
    <p class="detail">{_e(e.detail)}{layer}</p>
    <p class="action"><strong>Next:</strong> {_e(action)}</p>
    <label class="mark"><input type="checkbox" class="cb"> mark reviewed</label>
  </div>
</article>"""


def _rejections(report: Report) -> str:
    if not report.rejected:
        return ""
    rows = "\n".join(
        f'<tr><td class="m">{_e(m.bank_line_id)}</td><td>{_e(m.rule)}</td>'
        f'<td class="m">{m.confidence:.2f}</td>'
        f'<td>{_e(" · ".join(m.evidence))}</td></tr>'
        for m in report.rejected
    )
    return f"""<section>
  <h2>Rejected by the verifier <span class="count">{len(report.rejected)}</span></h2>
  <p class="note">Proposals the model made that the arithmetic refused. These are
  not failures of the design — they are the design working.</p>
  <div class="scroll"><table>
    <thead><tr><th>bank line</th><th>rule</th><th>confidence</th><th>evidence cited</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
</section>"""


def _itc(report: Report) -> str:
    if not report.itc:
        return ""
    rows = "\n".join(
        f'<tr class="{"bad" if line.at_risk else ""}"><td class="m">{_e(line.period)}</td>'
        f'<td class="m">{_e(line.invoice_no)}</td>'
        f'<td>{_e(line.status.value)}</td>'
        f'<td class="m num">{_e(inr(line.invoiced_gst))}</td>'
        f'<td class="m num">{_e(inr(line.gstr2b_gst) if line.gstr2b_gst is not None else "absent")}</td>'
        f'<td class="m num">{_e(inr(line.at_risk))}</td></tr>'
        for line in report.itc
    )
    return f"""<section>
  <h2>Input tax credit</h2>
  <p class="note">GST charged on gateway fees is claimable only if Razorpay's invoice
  reaches your GSTR-2B. Where it has not, the credit is not deferred — it is lost.</p>
  <div class="scroll"><table>
    <thead><tr><th>period</th><th>invoice</th><th>status</th>
    <th class="num">invoiced GST</th><th class="num">in 2B</th><th class="num">at risk</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
</section>"""


def _audit(report: Report) -> str:
    rows = "\n".join(
        f'<tr><td class="m">{_e(m.bank_line_id)}</td>'
        f'<td class="m">{_e(m.settlement_id)}</td>'
        f'<td class="m num">{_e(inr(m.amount))}</td>'
        f'<td>{_e(m.layer.value)}</td><td>{_e(m.rule)}</td>'
        f'<td class="m">{m.confidence:.2f}</td>'
        f'<td>{_e(" · ".join(m.evidence))}</td></tr>'
        for m in report.matches[:40]
    )
    more = (f'<p class="note">Showing 40 of {len(report.matches)}.</p>'
            if len(report.matches) > 40 else "")
    return f"""<section>
  <h2>Audit trail</h2>
  <p class="note">Every match records the layer and rule that produced it, plus the
  evidence. Nothing here is unattributable.</p>
  <div class="scroll"><table>
    <thead><tr><th>bank line</th><th>settlement</th><th class="num">amount</th>
    <th>layer</th><th>rule</th><th>conf</th><th>evidence</th></tr></thead>
    <tbody>{rows}</tbody></table></div>
  {more}
</section>"""


_CSS = """
:root{--bg:#eef0ec;--card:#f8f9f6;--ink:#16211c;--soft:#56605a;--faint:#8a938c;
--rule:#c9d0c7;--ok:#1f5d3f;--okbg:#dce9e0;--warn:#8a5f16;--warnbg:#efe5d0;
--crit:#8c2f27;--critbg:#f0dfdc;
--mono:ui-monospace,"SF Mono",Menlo,Consolas,monospace;
--serif:ui-serif,"Iowan Old Style",Palatino,Georgia,serif}
@media(prefers-color-scheme:dark){:root{--bg:#10150f;--card:#171d16;--ink:#e2e8e0;
--soft:#9ba69c;--faint:#6c7770;--rule:#2e382f;--ok:#63bc8c;--okbg:#172e20;
--warn:#d3a24e;--warnbg:#302716;--crit:#e08c7f;--critbg:#33201c}}
*{box-sizing:border-box}
body{background:var(--bg);color:var(--ink);font:16px/1.6 var(--serif);margin:0;
padding:0 24px 80px;-webkit-font-smoothing:antialiased}
header{max-width:1080px;margin:0 auto;padding:40px 0 22px;border-bottom:2px solid var(--ink)}
.brand{font:600 13px/1 var(--mono);letter-spacing:.22em}
.sub{font:11px/1.5 var(--mono);color:var(--faint);margin-top:8px;letter-spacing:.04em}
section{max-width:1080px;margin:0 auto;padding-top:46px}
h2{font:500 12px/1 var(--mono);letter-spacing:.16em;text-transform:uppercase;
color:var(--ok);margin:0 0 14px;padding-bottom:9px;border-bottom:1px solid var(--rule)}
h2 .count{color:var(--faint);margin-left:6px}
.note{font-size:14px;color:var(--soft);max-width:70ch;margin:0 0 16px}
kbd{font:11px var(--mono);background:var(--card);border:1px solid var(--rule);
border-radius:3px;padding:1px 5px}
.tiles{display:grid;grid-template-columns:repeat(auto-fit,minmax(190px,1fr));gap:14px;
max-width:1080px;margin:0 auto;padding-top:30px}
.tile{background:var(--card);border:1px solid var(--rule);border-left-width:3px;padding:16px 18px}
.tile.ok{border-left-color:var(--ok)}.tile.warning{border-left-color:var(--warn)}
.tile.critical{border-left-color:var(--crit)}
.tile .k{font:10px var(--mono);letter-spacing:.14em;text-transform:uppercase;color:var(--faint)}
.tile .v{font:26px/1.2 var(--mono);font-variant-numeric:tabular-nums;margin:7px 0 3px}
.tile .s{font-size:13px;color:var(--soft)}
.layer{display:grid;grid-template-columns:230px 1fr 92px;gap:14px;align-items:center;
padding:9px 0;border-bottom:1px solid var(--rule)}
.ll{font:13px var(--mono)}.lv{font:13px var(--mono);text-align:right;font-variant-numeric:tabular-nums}
.tag{font:9px var(--mono);letter-spacing:.1em;padding:2px 6px;margin-left:8px;border-radius:2px}
.tag.noai{background:var(--okbg);color:var(--ok)}.tag.ai{background:var(--warnbg);color:var(--warn)}
.bar{height:7px;background:var(--rule);border-radius:1px;overflow:hidden}
.bar i{display:block;height:100%;background:var(--ok)}
.toolbar{display:flex;flex-wrap:wrap;gap:7px;align-items:center;margin-bottom:12px}
.spacer{flex:1}
.chip{font:11px var(--mono);letter-spacing:.05em;background:var(--card);color:var(--soft);
border:1px solid var(--rule);border-radius:3px;padding:5px 10px;cursor:pointer}
.chip:hover{color:var(--ink)}
.chip.active{background:var(--ink);color:var(--bg);border-color:var(--ink)}
.chip .n{color:var(--faint);margin-left:3px}.chip.active .n{color:var(--bg);opacity:.65}
.queue{display:flex;flex-direction:column;gap:8px}
.row{background:var(--card);border:1px solid var(--rule);border-left-width:3px;cursor:pointer}
.row:focus{outline:2px solid var(--ok);outline-offset:2px}
.row.critical{border-left-color:var(--crit)}.row.warning{border-left-color:var(--warn)}
.row.info{border-left-color:var(--faint)}
.head{display:grid;grid-template-columns:10px 150px 1fr 130px 78px;gap:12px;
align-items:center;padding:13px 16px}
.sev{width:7px;height:7px;border-radius:50%;background:var(--faint)}
.row.critical .sev{background:var(--crit)}.row.warning .sev{background:var(--warn)}
.ref{font:13px var(--mono)}
.code{font:11px var(--mono);letter-spacing:.06em;color:var(--soft)}
.amt{font:13px var(--mono);text-align:right;font-variant-numeric:tabular-nums}
.done{font:9px var(--mono);letter-spacing:.1em;text-transform:uppercase;text-align:right;
color:var(--ok);opacity:0}
.row.reviewed .done{opacity:1}.row.reviewed{opacity:.5}
.body{display:none;padding:0 16px 16px 38px;border-top:1px solid var(--rule);margin-top:2px}
.row.open .body{display:block}
.detail{font-size:14px;margin:13px 0 9px}
.action{font-size:14px;color:var(--soft);margin:0 0 11px}
.mark{font:11px var(--mono);color:var(--soft);cursor:pointer;user-select:none}
.empty{color:var(--faint);font-style:italic}
.scroll{overflow-x:auto}
table{width:100%;border-collapse:collapse;font-size:14px;min-width:640px}
th{text-align:left;font:10px var(--mono);letter-spacing:.11em;text-transform:uppercase;
color:var(--faint);padding:0 12px 7px 0;border-bottom:1px solid var(--ink)}
td{padding:9px 12px 9px 0;border-bottom:1px solid var(--rule)}
td.m{font-family:var(--mono);font-size:13px}
th.num,td.num{text-align:right;font-variant-numeric:tabular-nums}
tr.bad td{color:var(--crit)}
footer{max-width:1080px;margin:64px auto 0;padding-top:18px;border-top:2px solid var(--ink);
font:10px var(--mono);letter-spacing:.1em;text-transform:uppercase;color:var(--faint)}
@media(max-width:760px){.head{grid-template-columns:10px 1fr 90px;}
.code,.done{display:none}.layer{grid-template-columns:1fr}}
"""

_JS = """
const KEY='quittance.reviewed';
const store=JSON.parse(localStorage.getItem(KEY)||'{}');
const rows=[...document.querySelectorAll('.row')];
let cur=-1;

rows.forEach(r=>{
  const id=r.dataset.id;
  if(store[id]){r.classList.add('reviewed');r.querySelector('.cb').checked=true;}
  r.querySelector('.head').addEventListener('click',()=>r.classList.toggle('open'));
  r.querySelector('.cb').addEventListener('click',ev=>{
    ev.stopPropagation();
    toggle(r,ev.target.checked);
  });
});

function toggle(r,on){
  r.classList.toggle('reviewed',on);
  r.querySelector('.cb').checked=on;
  if(on)store[r.dataset.id]=new Date().toISOString();else delete store[r.dataset.id];
  localStorage.setItem(KEY,JSON.stringify(store));
}

document.querySelectorAll('.chip[data-filter]').forEach(c=>{
  c.addEventListener('click',()=>{
    document.querySelectorAll('.chip[data-filter]').forEach(x=>x.classList.remove('active'));
    c.classList.add('active');
    const f=c.dataset.filter;
    rows.forEach(r=>r.style.display=(f==='all'||r.dataset.code===f)?'':'none');
  });
});

document.getElementById('hide-done').addEventListener('click',e=>{
  const on=e.target.classList.toggle('active');
  rows.forEach(r=>{if(r.classList.contains('reviewed'))r.style.display=on?'none':''});
});

document.getElementById('export').addEventListener('click',()=>{
  const blob=new Blob([JSON.stringify(store,null,2)],{type:'application/json'});
  const a=document.createElement('a');
  a.href=URL.createObjectURL(blob);a.download='quittance-decisions.json';a.click();
});

document.addEventListener('keydown',e=>{
  if(e.target.tagName==='INPUT')return;
  const vis=rows.filter(r=>r.style.display!=='none');
  if(!vis.length)return;
  if(e.key==='j'||e.key==='k'){
    cur=Math.max(0,Math.min(vis.length-1,cur+(e.key==='j'?1:-1)));
    vis[cur].focus();vis[cur].scrollIntoView({block:'center',behavior:'smooth'});
    e.preventDefault();
  }
  if(e.key==='r'&&cur>=0)toggle(vis[cur],!vis[cur].classList.contains('reviewed'));
  if(e.key==='Enter'&&cur>=0)vis[cur].classList.toggle('open');
});
"""


def write_report(report: Report, path: str) -> str:
    from pathlib import Path

    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render_html(report), encoding="utf-8")
    return str(out.resolve())

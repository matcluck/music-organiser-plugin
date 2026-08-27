#!/usr/bin/env python3
"""Local-only, auto-refreshing monitor for music metadata cleanup runs."""

from __future__ import annotations

import argparse
import html
import json
import re
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from urllib.request import urlopen


def read_json(path: Path) -> object | None:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def latest_progress(log_path: Path) -> str | None:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return None
    return next((line for line in reversed(lines) if line.startswith("Progress:")), None)


def latest_results(log_path: Path, limit: int = 12) -> list[str]:
    try:
        lines = log_path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []
    return [line.strip() for line in lines if re.match(r"^\s+\[\d+\].* -> ", line)][-limit:]


def service_health(port: int) -> bool:
    try:
        with urlopen(f"http://127.0.0.1:{port}/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def service_status(port: int) -> dict[str, object]:
    ready = service_health(port)
    status: dict[str, object] = {"ready": ready, "processing": False, "task": None, "remaining": None}
    if not ready:
        return status
    try:
        with urlopen(f"http://127.0.0.1:{port}/slots", timeout=1) as response:
            slots = json.loads(response.read().decode("utf-8"))
        if isinstance(slots, list) and slots and isinstance(slots[0], dict):
            slot = slots[0]
            status["processing"] = bool(slot.get("is_processing"))
            status["task"] = slot.get("id_task")
            next_token = slot.get("next_token")
            if isinstance(next_token, list) and next_token and isinstance(next_token[0], dict):
                status["remaining"] = next_token[0].get("n_remain")
    except (OSError, json.JSONDecodeError):
        pass
    return status


def run_status(root: Path) -> list[dict[str, object]]:
    runs: list[dict[str, object]] = []
    # A cleanup run is identified by its manifest, not by the name or type of
    # the source device.  This includes ordinary folders/HDD payloads as well
    # as Rekordbox USB exports while excluding unrelated artifact directories.
    for run_dir in sorted(path for path in root.iterdir() if path.is_dir() and (path / "manifest.json").is_file()):
        manifest = read_json(run_dir / "manifest.json")
        output = read_json(run_dir / "manifest-llm.json")
        if not isinstance(manifest, list):
            continue
        accepted = reviewed = 0
        if isinstance(output, list):
            for item in output:
                metadata = item.get("outputMetadata", {}) if isinstance(item, dict) else {}
                if isinstance(metadata, dict):
                    if metadata.get("title") and metadata.get("artist"):
                        accepted += 1
                    if metadata.get("needsReview"):
                        reviewed += 1
        runs.append(
            {
                "name": run_dir.name,
                "tracks": len(manifest),
                "accepted": accepted,
                "reviewed": reviewed,
                "progress": latest_progress(run_dir / "llm.log"),
                "results": latest_results(run_dir / "llm.log"),
            }
        )
    return runs


PAGE = """<!doctype html><title>Music metadata monitor</title><style>
body{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px;background:#111;color:#eee} h1{margin-bottom:.2rem}.muted{color:#aaa}.service{display:inline-block;margin:.4rem .8rem .8rem 0;padding:.25rem .6rem;border-radius:1rem;background:#333}.ok{background:#174d30}.bad{background:#622}section{margin:1.4rem 0;padding:1rem;border:1px solid #444;border-radius:.5rem}pre{white-space:pre-wrap;word-break:break-word;background:#181818;padding:.6rem;border-radius:.3rem}a{color:#8dc5ff}table{border-collapse:collapse;width:100%}td,th{padding:.35rem;text-align:left;border-bottom:1px solid #333}</style><body><h1>Music metadata monitor</h1><div id=services></div><p class=muted id=updated>Loading…</p><main id=runs></main><script>
function esc(s){const e=document.createElement('span');e.textContent=s??'';return e.innerHTML}
async function refresh(){try{const r=await fetch('/api/status',{cache:'no-store'});if(!r.ok)throw new Error('Status request failed ('+r.status+')');const d=await r.json();document.querySelector('#services').innerHTML=Object.entries(d.services).map(([p,s])=>`<span class="service ${s.ready?(s.processing?'ok':''):'bad'}">GPU ${p==='8080'?'0':'1'} (${p}): ${s.ready?(s.processing?'processing':'idle'):'offline'}${s.remaining!==null?' · '+s.remaining+' tokens remaining':''}</span>`).join('');document.querySelector('#updated').textContent='Updated '+new Date().toLocaleTimeString();document.querySelector('#runs').innerHTML=d.runs.map(r=>`<section><h2>${esc(r.name)}</h2><table><tr><th>Tracks</th><th>Cleaned</th><th>Needs review</th></tr><tr><td>${r.tracks}</td><td>${r.accepted}</td><td>${r.reviewed}</td></tr></table><p>${esc(r.progress||'Awaiting local-model cleanup')}</p><h3>Latest model results</h3><pre>${esc((r.results||[]).join('\\n')||'No completed batch yet.')}</pre><p><a href="/run?name=${encodeURIComponent(r.name)}">Inspect every track’s input evidence and cleaned output</a></p></section>`).join('')||'<p>No music manifests found yet.</p>'}catch(e){document.querySelector('#updated').textContent='Could not load live status: '+e.message}}refresh();setInterval(refresh,2000);</script>"""


def detail_page(root: Path, run_name: str) -> str:
    run = (root / run_name).resolve()
    if root.resolve() not in run.parents or not run.is_dir():
        return "Invalid run."
    source = read_json(run / "manifest.json")
    output = read_json(run / "manifest-llm.json")
    if not isinstance(source, list):
        return "No source manifest."
    outputs = output if isinstance(output, list) else []
    rows = []
    for index, item in enumerate(source):
        current = outputs[index] if index < len(outputs) and isinstance(outputs[index], dict) else {}
        source_path = item.get("source", "") if isinstance(item, dict) else ""
        evidence = item.get("originalMetadata", {}) if isinstance(item, dict) else {}
        cleaned = current.get("outputMetadata", {}) if isinstance(current, dict) else {}
        rows.append(f"<details><summary>{index + 1}. {html.escape(str(source_path))}</summary><h3>Input evidence</h3><pre>{html.escape(json.dumps(evidence, ensure_ascii=False, indent=2))}</pre><h3>Current model output</h3><pre>{html.escape(json.dumps(cleaned, ensure_ascii=False, indent=2))}</pre></details>")
    return f"<!doctype html><title>{html.escape(run_name)}</title><style>body{{font-family:system-ui,sans-serif;margin:2rem;max-width:1100px}}pre{{white-space:pre-wrap;word-break:break-word;background:#f3f3f3;padding:.7rem}}details{{margin:.7rem 0}}summary{{cursor:pointer}}</style><p><a href='/'>← live monitor</a></p><h1>{html.escape(run_name)}</h1>{''.join(rows)}"


class Handler(BaseHTTPRequestHandler):
    root: Path

    def send_text(self, text: str, content_type: str = "text/html; charset=utf-8") -> None:
        data = text.encode("utf-8")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store, max-age=0")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        if parsed.path == "/api/status":
            self.send_text(json.dumps({"services": {"8080": service_status(8080), "8081": service_status(8081)}, "runs": run_status(self.root)}), "application/json; charset=utf-8")
        elif parsed.path == "/run":
            self.send_text(detail_page(self.root, parse_qs(parsed.query).get("name", [""])[0]))
        elif parsed.path == "/":
            self.send_text(PAGE)
        else:
            self.send_error(HTTPStatus.NOT_FOUND)

    def log_message(self, _format: str, *_args: object) -> None:
        return


def main() -> None:
    parser = argparse.ArgumentParser(description="Serve a local-only music LLM monitor.")
    parser.add_argument("--runs-root", type=Path, default=Path("artifacts/runs"))
    parser.add_argument("--port", type=int, default=8090)
    args = parser.parse_args()
    Handler.root = args.runs_root.expanduser().resolve()
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    print(f"Music metadata monitor: http://127.0.0.1:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()

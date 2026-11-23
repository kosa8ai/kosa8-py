# Copyright (c) 2026 kosa8. Licensed under the MIT License; see LICENSE.
"""The compat pass rate: each competitor SDK's own tests and examples, run
unmodified against the kosa8 shims on kosa8 Cloud, written to
``docs/compat-results.md``.

    KOSA8_API_KEY=k8c_live_... python compat_suite/run.py

Needs ``pip install e2b==2.64.0 daytona==2.1.0 modal==1.6.4 pytest pytest-asyncio
pytest-timeout`` (the SDKs are imported only so their own tests import; every
``Sandbox`` they touch is the shim's). Sources are fetched at the pinned
commits below into ``--refs`` (default ``.compat-refs``). It makes real
sandboxes (one per test or example) and deletes every one it made.

A case is **pass** (ran to the end), **unsupported** (stopped at a call the
shim refuses with :class:`kosa8.compat.Unsupported`, which names what to use
instead), **fail** (anything else) or **skip** (the suite itself skipped it).
The pass rate is pass / (pass + unsupported + fail).
"""

from __future__ import annotations

import argparse
import datetime as dt
import os
import re
import subprocess
import sys
import tempfile
import time
import xml.etree.ElementTree as ET
from typing import Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

#: name -> (git URL, commit, what to run)
SOURCES = {
    "e2b": ("https://github.com/e2b-dev/E2B.git", "08c4eeb", "packages/python-sdk tests/sync/sandbox_sync"),
    "daytona": ("https://github.com/daytonaio/daytona.git", "b5a5d9e", "examples/python"),
    "modal": ("https://github.com/modal-labs/modal-examples.git", "50b16f6", "13_sandboxes"),
}
UNSUPPORTED = re.compile(r"is not supported by kosa8")

Case = Tuple[str, str, str]  # name, outcome, reason


def fetch(refs: str, name: str) -> str:
    url, commit, _ = SOURCES[name]
    d = os.path.join(refs, name)
    if not os.path.isdir(os.path.join(d, ".git")):
        subprocess.run(["git", "clone", "-q", url, d], check=True)
    if subprocess.run(["git", "-C", d, "cat-file", "-e", commit + "^{commit}"], capture_output=True).returncode != 0:
        subprocess.run(["git", "-C", d, "fetch", "-q", "--unshallow"], check=False)
    subprocess.run(["git", "-C", d, "checkout", "-q", commit, "--", "."], check=True)
    return d


def env_for(provider: str, record: str) -> Dict[str, str]:
    e = dict(os.environ)
    e["PYTHONPATH"] = os.pathsep.join([HERE, os.path.join(ROOT, "src"), e.get("PYTHONPATH", "")])
    e["KOSA8_COMPAT_PROVIDERS"] = "e2b,e2b_code_interpreter" if provider == "e2b" else provider
    e["KOSA8_COMPAT_RECORD"] = record
    e.setdefault("PYTHONUNBUFFERED", "1")
    return e


def reason_of(text: str) -> str:
    text = text.replace(sys.executable, "python").replace(os.path.expanduser("~"), "~")
    lines = [ln.strip() for ln in text.strip().splitlines() if ln.strip()]
    for ln in reversed(lines):
        if re.match(r"^(E\s+)?[\w.]*(Error|Exception|Unsupported|Timeout)\b", ln) or "is not supported by kosa8" in ln:
            return ln[:240]
    return (lines[-1] if lines else "")[:240]


def run_e2b(src: str, record: str, timeout: int) -> List[Case]:
    sdk = os.path.join(src, "packages/python-sdk")
    xml = os.path.join(tempfile.mkdtemp(), "e2b.xml")
    subprocess.run([sys.executable, "-m", "pytest", "tests/sync/sandbox_sync", "-p", "alias", "-q", "-p", "no:cacheprovider",
                    f"--timeout={timeout}", f"--junitxml={xml}"], cwd=sdk, env=env_for("e2b", record))
    out: List[Case] = []
    for tc in ET.parse(xml).getroot().iter("testcase"):
        name = f"{tc.get('classname', '').replace('tests.sync.sandbox_sync.', '')}::{tc.get('name')}"
        bad = tc.find("failure") if tc.find("failure") is not None else tc.find("error")
        sk = tc.find("skipped")
        if sk is not None:
            out.append((name, "skip", (sk.get("message") or "")[:200]))
        elif bad is not None:
            text = (bad.get("message") or "") + "\n" + (bad.text or "")
            out.append((name, "unsupported" if UNSUPPORTED.search(text) else "fail", reason_of(text)))
        else:
            out.append((name, "pass", ""))
    return out


def run_scripts(provider: str, files: List[str], base: str, record: str, timeout: int) -> List[Case]:
    out: List[Case] = []
    for f in files:
        name = os.path.relpath(f, base)
        try:
            p = subprocess.run([sys.executable, os.path.join(HERE, "_script.py"), f], env=env_for(provider, record),
                               capture_output=True, text=True, timeout=timeout, cwd=os.path.dirname(f))
            text = p.stdout[-4000:] + "\n" + p.stderr[-4000:]
            if p.returncode == 0:
                out.append((name, "pass", ""))
            else:
                out.append((name, "unsupported" if UNSUPPORTED.search(p.stderr) else "fail", reason_of(p.stderr or text)))
        except subprocess.TimeoutExpired:
            out.append((name, "fail", f"timed out after {timeout} s"))
        print(f"{provider} {name}: {out[-1][1]} {out[-1][2][:120]}", flush=True)
    return out


def cleanup(record: str) -> int:
    sys.path.insert(0, os.path.join(ROOT, "src"))
    from kosa8 import Kosa8Cloud
    from kosa8.errors import Kosa8NotFoundError

    if not os.path.exists(record):
        return 0
    cloud = Kosa8Cloud()
    n = 0
    for sid in sorted(set(open(record).read().split())):
        try:
            box = cloud.sandboxes.get(sid)
            if box.state != "deleted":
                box.delete()
                n += 1
        except Kosa8NotFoundError:
            pass
    return n


def summary(cases: List[Case]) -> Tuple[int, int, int, int]:
    c = {k: sum(1 for _, o, _ in cases if o == k) for k in ("pass", "unsupported", "fail", "skip")}
    return c["pass"], c["unsupported"], c["fail"], c["skip"]


def write(out: str, results: Dict[str, List[Case]], versions: Dict[str, str]) -> None:
    today = dt.date.today().isoformat()
    lines = [
        "# Compat results: other SDKs' own tests and examples on the kosa8 shims",
        "",
        f"Run {today} against https://api.kosa8.com by `compat_suite/run.py`. Each competitor's own test",
        "suite or examples run **unmodified**; only their SDK's names are pointed at the kosa8 shim",
        "(`kosa8.compat.e2b`, `kosa8.compat.daytona`, `kosa8.compat.modal`). A case is *pass* (ran to the end),",
        "*unsupported* (stopped at a call the shim refuses with `Unsupported`, which names the kosa8",
        "alternative), *fail* (anything else) or *skip* (skipped by the suite itself). Pass rate =",
        "pass / (pass + unsupported + fail). Rerun it yourself: `python compat_suite/run.py`.",
        "",
        "| Suite | Source | Pass | Unsupported | Fail | Skip | Pass rate |",
        "|---|---|---|---|---|---|---|",
    ]
    for name, cases in results.items():
        p, u, f, s = summary(cases)
        url, commit, what = SOURCES[name]
        total = p + u + f
        rate = f"{p}/{total} ({100 * p / total:.0f}%)" if total else "n/a"
        repo = url.removesuffix(".git").replace("https://github.com/", "")
        lines.append(f"| {versions.get(name, name)} | `{repo}@{commit}` {what.split()[-1]} | {p} | {u} | {f} | {s} | {rate} |")
    for name, cases in results.items():
        lines += ["", f"## {versions.get(name, name)}", "", "| Case | Outcome | Why |", "|---|---|---|"]
        for case, outcome, why in sorted(cases, key=lambda c: ({"fail": 0, "unsupported": 1, "skip": 2, "pass": 3}[c[1]], c[0])):
            lines.append(f"| `{case}` | {outcome} | {why.replace('|', '/')} |")
    os.makedirs(os.path.dirname(out), exist_ok=True)
    with open(out, "w") as fh:
        fh.write("\n".join(lines) + "\n")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--refs", default=os.path.join(ROOT, ".compat-refs"))
    ap.add_argument("--out", default=os.path.join(ROOT, "docs", "compat-results.md"))
    ap.add_argument("--only", default="e2b,daytona,modal")
    ap.add_argument("--timeout", type=int, default=300, help="seconds per test or example")
    a = ap.parse_args()
    if not os.environ.get("KOSA8_API_KEY"):
        sys.exit("set KOSA8_API_KEY to a kosa8 Cloud key (k8c_live_...)")
    os.makedirs(a.refs, exist_ok=True)
    record = os.path.join(tempfile.mkdtemp(), "sandboxes.txt")
    versions = {}
    for mod, label in (("e2b", "E2B Python SDK"), ("daytona", "Daytona Python SDK"), ("modal", "Modal Python SDK")):
        try:
            from importlib.metadata import version

            versions[mod] = f"{label} {version(mod)}"
        except Exception:  # noqa: BLE001
            versions[mod] = label
    results: Dict[str, List[Case]] = {}
    started = time.time()
    try:
        for name in [x.strip() for x in a.only.split(",") if x.strip()]:
            src = fetch(a.refs, name)
            if name == "e2b":
                results[name] = run_e2b(src, record, a.timeout)
            elif name == "daytona":
                base = os.path.join(src, "examples/python")
                files = sorted(os.path.join(dp, f) for dp, _, fs in os.walk(base) for f in fs
                               if f.endswith(".py") and "_async" not in dp)
                results[name] = run_scripts(name, files, base, record, a.timeout)
            elif name == "modal":
                base = os.path.join(src, "13_sandboxes")
                files = sorted(os.path.join(base, f) for f in os.listdir(base) if f.endswith(".py"))
                results[name] = run_scripts(name, files, base, record, a.timeout)
    finally:
        print(f"deleted {cleanup(record)} sandboxes left behind", flush=True)
    write(a.out, results, versions)
    for name, cases in results.items():
        p, u, f, s = summary(cases)
        print(f"{name}: {p} pass, {u} unsupported, {f} fail, {s} skip")
    print(f"wrote {a.out} in {time.time() - started:.0f} s")


if __name__ == "__main__":
    main()

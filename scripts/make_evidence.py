import argparse
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

import yaml

from bag.artifact import Artifact, Locator, Step, load_artifact, next_version, resolve_artifact_path, save_artifact
from bag.artifact.builder import default_interruptions, default_outcomes
from bag.handoff import list_interventions
from bag.surface import Target

ROOT = Path(__file__).resolve().parent.parent
HOST = "127.0.0.1"
ARTIFACT = "evidence-member-balance"
HANDOFF_ARTIFACT = "evidence-member-balance-handoff"
GOAL = "Sign on, look up the member whose id is the member_id input, and read the member's name and savings balance."
RESUME_WAIT_SECONDS = 90

RUNS = [
    ("01-discovery", "done",
     "An LLM learned the member-balance task once from a plain-English goal; the log holds only placeholders, never the real login."),
    ("02-replay", "SUCCESS",
     "The saved artifact replays for a different member with no LLM call and returns typed outputs."),
    ("03-replay-faults", "SUCCESS",
     "The same artifact survives an injected maintenance popup and slow pages on its own, with no human."),
    ("04-handoff", "SUCCESS",
     "A step that cannot be done pauses the automation, a person gets the visible browser and an intervention file, "
     "`bag resume` hands control back, and the run finishes."),
]

IMPOSSIBLE_TEXT = "Confirmation banner that never appears"


def sample_artifact(version: int) -> Artifact:
    def locator(primary, *fallbacks):
        return {"primary": primary, "fallbacks": list(fallbacks)}

    steps = [
        {"action": "type", "text": "{{secret:BANK_USER}}",
         "locator": locator({"role": "textbox", "name": "User name:"}, {"css": 'input[name="user"]'})},
        {"action": "type", "text": "{{secret:BANK_PASSWORD}}", "locator": locator({"css": 'input[name="pw"]'})},
        {"action": "click", "locator": locator({"role": "button", "name": "Sign On"}),
         "expected": {"url_contains": "/home"}},
        {"action": "wait", "locator": locator({"role": "link", "name": "Sign Off"})},
        {"action": "type", "text": "{{member_id}}", "locator": locator({"css": 'input[name="mid"]'})},
        {"action": "click", "locator": locator({"role": "button", "name": "Search"})},
        {"action": "wait", "locator": locator({"role": "heading", "name": "Member Details"})},
        {"action": "read", "output_name": "member_name",
         "locator": locator({"css": "tr:nth-of-type(2) > td:nth-of-type(2)"})},
        {"action": "read", "output_name": "savings_balance",
         "locator": locator({"css": "tr:nth-of-type(4) > td:nth-of-type(2)"})},
    ]
    return Artifact.model_validate({
        "metadata": {
            "name": ARTIFACT, "version": version, "app": "First Legacy Bank", "status": "approved",
            "description": "Sample task for the evidence runs: look up a member and read the name and savings balance.",
            "start_url": f"http://{HOST}:5000/login",
        },
        "inputs": [{"name": "member_id", "type": "string", "pattern": "[0-9]{4}",
                    "description": "The four-digit member id to look up."}],
        "steps": steps,
        "outputs": [{"name": "member_name"}, {"name": "savings_balance", "type": "decimal"}],
        "success_check": {"outputs_present": ["member_name", "savings_balance"]},
        "known_outcomes": [o.model_dump(mode="json") for o in default_outcomes()],
        "known_interruptions": [i.model_dump(mode="json") for i in default_interruptions()],
    })


def seed_sample_artifact(artifacts_dir: Path) -> Path:
    version = next_version(artifacts_dir, ARTIFACT)
    return save_artifact(sample_artifact(version), artifacts_dir)


def make_handoff_artifact(artifacts_dir: Path, source_name: str = ARTIFACT) -> Path:
    source = load_artifact(resolve_artifact_path(source_name, artifacts_dir))
    data = source.model_dump(mode="json")
    data["metadata"].update(
        name=HANDOFF_ARTIFACT, version=next_version(artifacts_dir, HANDOFF_ARTIFACT), status="approved",
        description=f"{source.metadata.description} Ends with a step that cannot be done, so a person is needed.",
    )
    extra = Step(
        action="wait",
        locator=Locator(primary=Target(text=IMPOSSIBLE_TEXT), why="Evidence run: a step that cannot be done."),
    )
    data["steps"].append(extra.model_dump(mode="json", exclude_none=True))
    return save_artifact(Artifact.model_validate(data), artifacts_dir)


def port_is_open(port: int, host: str = HOST) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
        probe.settimeout(0.5)
        return probe.connect_ex((host, port)) == 0


def wait_for_port(port: int, seconds: float = 20) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if port_is_open(port):
            return True
        time.sleep(0.25)
    return False


def write_safety_config(port: int, evidence_dir: Path, directory: Path) -> Path:
    shipped = yaml.safe_load((ROOT / "config" / "safety.yaml").read_text(encoding="utf-8"))
    shipped["allowed_urls"] = [f"http://{HOST}:{port}", f"http://localhost:{port}"]
    shipped["audit_log"] = str(evidence_dir / "safety-decisions.jsonl")
    path = directory / "safety.yaml"
    path.write_text(yaml.safe_dump(shipped, sort_keys=False), encoding="utf-8")
    return path


def run_commands(options, evidence_dir: Path, safety: Path, interventions_dir: Path) -> dict[str, list[str]]:
    base = f"http://{HOST}:{options.bank_port}"
    common = ["--evidence-dir", str(evidence_dir), "--safety-config", str(safety)]
    replay_common = [*common, "--artifacts-dir", str(options.artifacts_dir)]
    replay = ["replay", ARTIFACT, "--input", f"member_id={options.replay_member}"]
    return {
        "01-discovery": ["discover", "--goal", GOAL, "--input", f"member_id={options.discovery_member}",
                         "--start-url", base, "--output-dir", str(evidence_dir / "recordings"),
                         "--run-id", "01-discovery", *common],
        "02-replay": [*replay, "--run-id", "02-replay", *replay_common],
        "03-replay-faults": [*replay, "--start-url", f"{base}/login?popup=1&slow=1", "--run-id", "03-replay-faults",
                             *replay_common],
        "04-handoff": ["replay", HANDOFF_ARTIFACT, "--input", f"member_id={options.replay_member}", "--headed",
                       "--takeover", "--timeout", "3", "--interventions-dir", str(interventions_dir),
                       "--run-id", "04-handoff", *replay_common],
    }


def bag(arguments, env, **kwargs):
    return subprocess.run(
        [sys.executable, "-m", "bag", *arguments], cwd=ROOT, env=env, text=True, encoding="utf-8", errors="replace",
        **kwargs,
    )


def captured(arguments, env):
    done = bag(arguments, env, capture_output=True)
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def save_console(evidence_dir: Path, run_id: str, text: str) -> None:
    folder = evidence_dir / run_id
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "console.txt").write_text(text, encoding="utf-8")


def run_with_auto_resume(arguments, env, interventions_dir: Path) -> tuple[int, str]:
    process = subprocess.Popen(
        [sys.executable, "-m", "bag", *arguments], cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        text=True, encoding="utf-8", errors="replace",
    )
    lines: list[str] = []
    resume_text = ""
    reader = threading.Thread(target=lambda: lines.extend(process.stdout), daemon=True)
    reader.start()
    resumed = False
    deadline = time.monotonic() + RESUME_WAIT_SECONDS
    while process.poll() is None and time.monotonic() < deadline:
        waiting = [item for item in list_interventions(interventions_dir) if item["status"] == "waiting"]
        if waiting and not resumed:
            time.sleep(1.0)
            code, text = captured(["resume", "--interventions-dir", str(interventions_dir)], env)
            resume_text = "\n$ bag resume\n" + text
            resumed = code == 0
        time.sleep(0.5)
    if process.poll() is None:
        process.kill()
    process.wait()
    reader.join(5)
    return process.returncode, "".join(lines) + resume_text


def read_status(evidence_dir: Path, run_id: str) -> tuple[str | None, dict]:
    path = evidence_dir / run_id / "result.json"
    if not path.is_file():
        return None, {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return data.get("status"), data


def render_index(evidence_dir: Path, skipped: tuple[str, ...] = ()) -> str:
    rows = []
    for run_id, expected, proves in RUNS:
        status, data = read_status(evidence_dir, run_id)
        if status is not None:
            result = f"`{status}` in {data.get('seconds', '?')} s"
            name = f"[{run_id}]({run_id}/)"
        elif run_id in skipped:
            result, name = "skipped (a sample artifact was used instead)", run_id
        else:
            result, name = "not run yet", run_id
        rows.append(f"| {name} | {proves} | {result} |")
    return (
        "# Evidence index\n\n"
        "Each row is one run, made by `scripts/make_evidence.sh` (Windows: `scripts/make_evidence.ps1`). "
        "Every run folder holds `steps.jsonl` (one redacted JSON line per step), `result.json` (how the run ended), "
        "`console.txt` (what the terminal showed) and, only after a failure or a human handoff, `screenshots/` "
        "(and, for a handoff, `interventions/` with the intervention file and what the person did).\n\n"
        "| Run | What it proves | Result |\n|---|---|---|\n" + "\n".join(rows) + "\n\n"
        "Secrets never appear in these files: logins show as `{{secret:NAME}}`, account numbers keep only their last "
        "four digits, and screenshots are blurred where a password, a typed secret or an account number is on screen.\n"
    )


def write_index(evidence_dir: Path, skipped: tuple[str, ...] = ()) -> Path:
    evidence_dir.mkdir(parents=True, exist_ok=True)
    path = evidence_dir / "INDEX.md"
    path.write_text(render_index(evidence_dir, skipped), encoding="utf-8")
    return path


def parse_args(argv):
    parser = argparse.ArgumentParser(description="Run the four evidence runs and write evidence/INDEX.md.")
    parser.add_argument("--evidence-dir", type=Path, default=ROOT / "evidence")
    parser.add_argument("--artifacts-dir", type=Path, default=ROOT / "artifacts")
    parser.add_argument("--bank-port", type=int, default=int(os.environ.get("BANK_PORT", "5000")))
    parser.add_argument("--skip-discovery", action="store_true",
                        help="No LLM: use a built-in sample artifact for runs 02-04 and mark run 01 as skipped.")
    parser.add_argument("--manual", action="store_true",
                        help="Run 04 in this terminal and wait for YOU to act in the browser and press Enter.")
    parser.add_argument("--discovery-member", default="1001")
    parser.add_argument("--replay-member", default="1003")
    parser.add_argument("--index-only", action="store_true", help="Only rewrite INDEX.md from what is on disk.")
    return parser.parse_args(argv)


def main(argv=None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(errors="replace")
    options = parse_args(argv)
    evidence_dir = options.evidence_dir.resolve()
    skipped = ("01-discovery",) if options.skip_discovery else ()
    if options.index_only:
        print(write_index(evidence_dir))
        return 0

    work = Path(tempfile.mkdtemp(prefix="bag-evidence-"))
    interventions_dir = evidence_dir / "04-handoff" / "interventions"
    safety = write_safety_config(options.bank_port, evidence_dir, work)
    env = {**os.environ, "BANK_URL": f"http://{HOST}:{options.bank_port}", "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"}
    commands = run_commands(options, evidence_dir, safety, interventions_dir)
    for run_id, _, _ in RUNS:
        shutil.rmtree(evidence_dir / run_id, ignore_errors=True)

    bank = None
    failures: list[str] = []
    try:
        if port_is_open(options.bank_port):
            print(f"Using the bank already running on port {options.bank_port}.")
        else:
            print(f"Starting the bank on port {options.bank_port}.")
            bank = subprocess.Popen([sys.executable, "-m", "bag", "bank", "--port", str(options.bank_port)], cwd=ROOT,
                                    env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if not wait_for_port(options.bank_port):
                print("The bank did not start. Check BANK_USER and BANK_PASSWORD in .env.", file=sys.stderr)
                return 1

        for run_id, expected, proves in RUNS:
            print(f"\n=== {run_id}: {proves}")
            if run_id == "01-discovery" and options.skip_discovery:
                path = seed_sample_artifact(options.artifacts_dir)
                print(f"Skipped (no LLM). Sample artifact written to {path}")
                continue
            if run_id == "02-replay" and not options.skip_discovery:
                status, data = read_status(evidence_dir, "01-discovery")
                if status != "done":
                    failures.append("01-discovery did not finish, so there is no artifact to replay")
                    break
                code, text = captured(["build", data["recording"], "--name", ARTIFACT,
                                       "--artifacts-dir", str(options.artifacts_dir)], env)
                print(text)
                if code != 0:
                    failures.append("bag build failed")
                    break
                code, text = captured(["approve", ARTIFACT, "--yes", "--artifacts-dir", str(options.artifacts_dir)], env)
                print(text)
                if code != 0:
                    failures.append("bag approve failed")
                    break
            if run_id == "04-handoff":
                path = make_handoff_artifact(options.artifacts_dir)
                print(f"Handoff artifact written to {path}")

            arguments = commands[run_id]
            if run_id == "04-handoff" and options.manual:
                print("Manual mode: act in the browser window, then press Enter in this terminal.")
                code = bag(arguments, env).returncode
                text = ""
            elif run_id == "04-handoff":
                code, text = run_with_auto_resume(arguments, env, interventions_dir)
            else:
                code, text = captured(arguments, env)
            if text:
                print(text)
                save_console(evidence_dir, run_id, text)
            status, _ = read_status(evidence_dir, run_id)
            print(f"-> {run_id}: exit code {code}, status {status}")
            if status != expected:
                failures.append(f"{run_id} ended with {status!r}, expected {expected!r}")
                if run_id == "01-discovery":
                    break
    finally:
        if bank is not None:
            bank.terminate()
        write_index(evidence_dir, skipped)
        shutil.rmtree(work, ignore_errors=True)

    print(f"\nIndex written to {evidence_dir / 'INDEX.md'}")
    if failures:
        print("Problems:\n  " + "\n  ".join(failures), file=sys.stderr)
        return 1
    print("All four evidence runs ended as expected.")
    return 0


if __name__ == "__main__":
    sys.exit(main())

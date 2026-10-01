"""Run the frontend's pure duration function without starting a browser."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


def test_resumed_run_duration_excludes_idle_time():
    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the frontend unit test")
    source = (Path(__file__).parents[1] / "src/lightworker/web_static/app.js").read_text()
    function = source.split("function runElapsedMilliseconds(", 1)[1].split("\nfunction statusLabel", 1)[0]
    script = (
        "const isBusy = run => run.status === 'running';\nfunction runElapsedMilliseconds("
        + function
        + "\nconst events = ["
        "{type:'agentic_run_started',timestamp:'2026-10-01T00:00:00Z'},"
        "{type:'agentic_run_completed',timestamp:'2026-10-01T00:00:25Z'},"
        "{type:'agentic_run_started',timestamp:'2026-10-01T00:15:00Z'}];\n"
        "const run = {status:'running',events};\n"
        "const active = runElapsedMilliseconds(run, Date.parse('2026-10-01T00:15:10Z'));\n"
        "run.status = 'succeeded';\n"
        "run.events.push({type:'agentic_run_completed',timestamp:'2026-10-01T00:15:15Z'});\n"
        "const completed = runElapsedMilliseconds(run);\n"
        "const fallback = runElapsedMilliseconds({status:'succeeded',"
        "created_at:'2026-10-01T00:00:00Z',updated_at:'2026-10-01T00:00:05Z'});\n"
        "console.log(JSON.stringify({active, completed, fallback}));"
    )
    result = subprocess.run([node, "-e", script], capture_output=True, text=True, check=True)
    assert json.loads(result.stdout) == {"active": 35_000, "completed": 40_000, "fallback": 5_000}

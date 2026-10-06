#!/usr/bin/env python3
"""Seed only official subscription auth into a private Docker volume. Never print secrets."""

import argparse
import json
import subprocess
from pathlib import Path

p = argparse.ArgumentParser(description=__doc__)
p.add_argument("provider", choices=["codex", "claude"])
p.add_argument("--source", type=Path, required=True)
p.add_argument("--image", default="auto-reviewer-cli:local")
a = p.parse_args()
raw = a.source.read_bytes()
data = json.loads(raw)
if a.provider == "codex":
    if data.get("auth_mode") != "chatgpt" or not data.get("tokens"):
        raise SystemExit("Source must contain official Codex ChatGPT login, not an API key.")
    filename = "auth.json"
else:
    if not data.get("claudeAiOauth"):
        raise SystemExit("Source must contain official Claude Code subscription login.")
    filename = ".credentials.json"
volume = f"auto-reviewer_{a.provider}-auth"
subprocess.run(["docker", "volume", "create", volume], check=True, stdout=subprocess.DEVNULL)
script = (
    'const fs=require("fs");let b=[];process.stdin.on("data",c=>b.push(c));process.stdin.on("end",()=>{fs.chownSync("/auth",1000,1000);fs.chmodSync("/auth",0o700);const p="/auth/'
    + filename
    + '";fs.writeFileSync(p,Buffer.concat(b),{mode:0o600});fs.chownSync(p,1000,1000);});'
)
subprocess.run(
    [
        "docker",
        "run",
        "--rm",
        "-i",
        "--network=none",
        "--user=0:0",
        "--read-only",
        "--mount",
        f"type=volume,source={volume},target=/auth",
        "--entrypoint=node",
        a.image,
        "-e",
        script,
    ],
    input=raw,
    check=True,
    stdout=subprocess.DEVNULL,
)
print(
    f"{a.provider} subscription login seeded into private runner volume. No personal settings copied."
)

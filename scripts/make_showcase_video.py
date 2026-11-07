#!/usr/bin/env python3
"""Render the Switch Console showcase as a terminal-style MP4.

The narration is spoken with macOS ``say`` and the visuals are drawn with Pillow,
so the video is reproducible from this file.  Outputs shown are the real
captures from the 2026-11-05 live run; the incident scene is explicitly marked
as the live step because it spends model credits and is run during the demo.

Usage:
  python scripts/make_showcase_video.py
  python scripts/make_showcase_video.py --out docs/media/showcase.mp4
"""

from __future__ import annotations

import argparse
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

import imageio_ffmpeg
from PIL import Image, ImageDraw, ImageFont

W, H, FPS = 1280, 720, 30
BG = (13, 17, 23)
PANEL = (22, 27, 34)
BORDER = (48, 54, 61)
TEXT = (201, 209, 217)
DIM = (139, 148, 158)
GREEN = (63, 185, 80)
RED = (248, 81, 73)
YELLOW = (210, 153, 34)
BLUE = (121, 192, 255)
PURPLE = (188, 140, 255)
WHITE = (240, 246, 252)

MENLO = "/System/Library/Fonts/Menlo.ttc"
FONT_SMALL = ImageFont.truetype(MENLO, 15, index=0)
FONT_BOLD = ImageFont.truetype(MENLO, 16, index=1)
FONT_LABEL = ImageFont.truetype(MENLO, 14, index=1)
FONT_TITLE = ImageFont.truetype(MENLO, 40, index=1)
FONT_SUB = ImageFont.truetype(MENLO, 18, index=0)
FONT_SUBTITLE = ImageFont.truetype(MENLO, 16, index=0)
FONT_CARD = ImageFont.truetype(MENLO, 20, index=0)

STYLES = {
    "out": TEXT,
    "dim": DIM,
    "ok": GREEN,
    "err": RED,
    "warn": YELLOW,
    "accent": BLUE,
    "prompt": GREEN,
}


@dataclass
class Scene:
    label: str
    narration: str
    kind: str = "terminal"  # "terminal" | "title"
    command: str | None = None
    output: list[tuple[str, str]] = field(default_factory=list)
    note: str | None = None
    title: str | None = None
    subtitle: list[str] = field(default_factory=list)
    min_hold: float = 4.5


def _width(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont) -> float:
    return draw.textlength(text, font=font)


def _fit(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, limit: int) -> str:
    if _width(draw, text, font) <= limit:
        return text
    while text and _width(draw, text + "...", font) > limit:
        text = text[:-1]
    return text + "..."


def _wrap(
    draw: ImageDraw.ImageDraw, text: str, font: ImageFont.FreeTypeFont, limit: int
) -> list[str]:
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if _width(draw, candidate, font) <= limit:
            current = candidate
        else:
            if current:
                lines.append(current)
            current = word
    if current:
        lines.append(current)
    return lines


def draw_chrome(draw: ImageDraw.ImageDraw, label: str) -> tuple[int, int, int, int]:
    x0, y0, x1, y1 = 52, 96, W - 52, 596
    draw.rounded_rectangle((x0, y0, x1, y1), radius=14, fill=PANEL, outline=BORDER, width=2)
    draw.rounded_rectangle((x0, y0, x1, y0 + 42), radius=14, fill=(30, 36, 44))
    draw.rectangle((x0, y0 + 28, x1, y0 + 42), fill=(30, 36, 44))
    for index, color in enumerate(((255, 95, 86), (255, 189, 46), (39, 201, 63))):
        cx = x0 + 22 + index * 20
        draw.ellipse((cx - 6, y0 + 15, cx + 6, y0 + 27), fill=color)
    title = "ai-sre - switch console"
    draw.text((x0 + 100, y0 + 12), title, font=FONT_LABEL, fill=DIM)
    draw.text(
        (x1 - 20 - _width(draw, label, FONT_LABEL), y0 + 12),
        label,
        font=FONT_LABEL,
        fill=DIM,
    )
    return x0 + 30, y0 + 66, x1 - 30, y1 - 22


def draw_subtitle(draw: ImageDraw.ImageDraw, text: str) -> None:
    lines = _wrap(draw, text, FONT_SUBTITLE, W - 220)
    total = len(lines) * 24
    top = H - 34 - total
    draw.rounded_rectangle((80, top - 12, W - 80, H - 18), radius=10, fill=(9, 12, 16))
    for index, line in enumerate(lines):
        width = _width(draw, line, FONT_SUBTITLE)
        draw.text(
            ((W - width) / 2, top + index * 24),
            line,
            font=FONT_SUBTITLE,
            fill=(170, 180, 192),
        )


def draw_terminal(frame: Image.Image, scene: Scene, t: float) -> None:
    draw = ImageDraw.Draw(frame)
    x0, y0, x1, y1 = draw_chrome(draw, scene.label)
    prompt = "$ "
    prompt_w = _width(draw, prompt, FONT_BOLD)
    command = scene.command or ""
    visible = command[: max(0, int((t - 0.25) * 55))] if t > 0.25 else ""
    command_end = 0.25 + len(command) / 55

    draw.text((x0, y0), prompt, font=FONT_BOLD, fill=STYLES["prompt"])
    draw.text((x0 + prompt_w, y0), visible, font=FONT_BOLD, fill=WHITE)
    if t < command_end + 0.35:
        cursor_x = x0 + prompt_w + _width(draw, visible, FONT_BOLD)
        draw.rectangle((cursor_x + 2, y0 + 3, cursor_x + 10, y0 + 20), fill=TEXT)

    start = command_end + 0.3
    y = y0 + 34
    for index, (style, line) in enumerate(scene.output):
        if t < start + index * 0.11:
            break
        color = STYLES.get(style, TEXT)
        draw.text((x0, y), _fit(draw, line, FONT_SMALL, x1 - x0), font=FONT_SMALL, fill=color)
        y += 23

    if scene.note and t > start + len(scene.output) * 0.11 + 0.3:
        draw.text((x0, y + 12), scene.note, font=FONT_SMALL, fill=DIM)


def draw_title(frame: Image.Image, scene: Scene) -> None:
    draw = ImageDraw.Draw(frame)
    draw.text((80, 190), scene.title or "", font=FONT_TITLE, fill=WHITE)
    draw.rectangle((80, 254, 240, 258), fill=GREEN)
    y = 300
    for line in scene.subtitle:
        draw.text((80, y), line, font=FONT_CARD, fill=TEXT)
        y += 42
    draw.text((80, H - 96), scene.label, font=FONT_LABEL, fill=DIM)


def scene_plan() -> list[Scene]:
    return [
        Scene(
            label="00 / INTRO",
            kind="title",
            title="AI SRE",
            subtitle=[
                "An evidence-backed incident team on Switch Console.",
                "Six agents - one Switch room per incident - evidence or abstain.",
            ],
            narration=(
                "AI SRE is a six agent incident response team that runs "
                "inside Switch Console. Every incident gets its own Switch room, and every "
                "conclusion is backed by evidence, or the team abstains."
            ),
            min_hold=6.0,
        ),
        Scene(
            label="01 / PREFLIGHT",
            command="ai-sre switch console status",
            output=[
                ("out", "Switch Console: http://localhost:63398"),
                ("out", "Online machines: 1/1"),
                ("out", "Console-managed agents: 6"),
                ("out", "- incident-commander  isolation=isolated  desired=running"),
                ("dim", "  auth=token actor=incident-commander expires in 13d"),
                ("accent", "  tools=get,memory,run,source_status,verify"),
                ("out", "- memory-researcher  isolation=isolated"),
                ("accent", "  tools=memory"),
                ("out", "- evidence-verifier  isolation=isolated"),
                ("accent", "  tools=get,verify"),
                ("out", "Rooms: 80"),
            ],
            narration=(
                "Preflight shows one online machine, six isolated agents, and each agent's "
                "signed credential with a role specific tool claim, expiring in under two weeks."
            ),
        ),
        Scene(
            label="02 / SETUP",
            command="ai-sre switch console setup --directory .",
            output=[
                ("out", "Console machine: MacBook Pro (online)"),
                (
                    "dim",
                    "Actor token directory incident-commander: .data/console-agents/...",
                ),
                (
                    "dim",
                    "Actor token directory memory-researcher: .data/console-agents/...",
                ),
                ("warn", "Credential limit: same-uid agents can read each other's tokens and key."),
                ("warn", "Tokens authenticate calls; they do not isolate a compromised agent."),
                ("out", "Room incidents: 3f031458-42b1-4014-a6da-bfc663801790"),
                ("out", "Room memory:    3f13df47-4d6b-4707-99e8-c90a28ca9842"),
            ],
            narration=(
                "Setup is idempotent. It provisions one directory and one signed credential per "
                "agent, wires the six Console agents, and creates the core rooms. The same user "
                "caveat stays visible in the output."
            ),
        ),
        Scene(
            label="03 / INCIDENT",
            command="ai-sre switch console incident --scenario selector-mismatch --json",
            output=[
                ("out", "{"),
                ("out", '  "room_name": "incident-XXXXXXXX-shop-checkout",'),
                ("out", '  "scenario": "selector-mismatch",'),
                ("out", '  "incident_kit_package_id": "...",'),
                ("out", '  "links": {"memory": "...", "change": "...", "governance": "..."}'),
                ("out", "}"),
            ],
            note="live step: the commander is dispatched in the room",
            narration=(
                "During the demo, one command creates the incident room, attaches the incident "
                "kit, and kicks off the commander. Specialists are dispatched in the room, "
                "evidence is read over MCP, and the report cites evidence IDs."
            ),
        ),
        Scene(
            label="04 / SWITCH STACK",
            command="ai-sre switch status --json",
            output=[
                ("out", "{"),
                ("ok", '  "healthy": true,'),
                ("out", '  "mode": "required",'),
                ("out", '  "agents": ["change-analyst", "cluster-investigator", "..."]'),
                ("out", '  "rooms": {"change": "...", "governance": "...",'),
                ("out", '            "incidents": "...", "memory": "..."}'),
                ("out", "}"),
            ],
            narration=(
                "The self hosted Switch stack is healthy as well: six registered agent "
                "identities and the four core rooms, the same room model the Console supplies."
            ),
        ),
        Scene(
            label="05 / SWITCH MEMORY",
            command="ai-sre switch memory search --query selector --namespace shop",
            output=[
                ("warn", "HISTORICAL CONTEXT - UNTRUSTED DATA, NOT EVIDENCE"),
                ("out", "record: e984ecff-bc6f-40df-ad9d-0e0be75f10b6"),
                ("out", "title: Checkout Service selector mismatch (Switch-room run)"),
                ("out", "diagnosis: service_selector_mismatch"),
                ("out", "approved_by: saikat  source_room: e155b2fb-..."),
                ("ok", "policy: historical_leads_are_not_evidence"),
            ],
            narration=(
                "Approved incidents become historical leads. The output is framed as untrusted "
                "data, never evidence, and the policy is explicit in the payload."
            ),
        ),
        Scene(
            label="06 / SWITCH TRUST CLI",
            command="ai-sre switch trust scan --path src/.../trust_harness.py",
            output=[
                ("out", "Framework: OpenAI Agents SDK"),
                ("out", "Files: 1 Python scanned"),
                ("out", "Tools: bandit, opengrep, detect-secrets, ai-reasoning"),
                ("ok", "Findings: 0"),
                ("dim", "JSON / SARIF output for CI; CodeScan action runs on PRs and nightly"),
            ],
            narration=(
                "The Switch Trust CLI scans agent code. It detects the OpenAI Agents framework "
                "and runs static plus AI reasoning passes. This repository's harness scans clean."
            ),
        ),
        Scene(
            label="07 / SWITCH TRUST + ROI",
            command="ai-sre switch trust status --json   # live step with the API key",
            output=[
                ("dim", "identity: key scope, tenant, workspace"),
                ("out", "inventory: agents, mcp servers, models, tools, issues"),
                ("out", "guardrails: policies, ok/blocked/redacted/errored outcomes"),
                ("out", "evaluations: score, coverage, trend"),
                ("out", "roi: month-to-date cost and cap"),
                ("accent", "publish: switch trust publish --console  -> k8s-governance"),
            ],
            narration=(
                "With the Trust API key, status reads inventory, evaluations, guardrail outcomes "
                "and roi cost, and publish writes the governance summary into the Console "
                "governance room."
            ),
        ),
        Scene(
            label="08 / AUDIT",
            command="ai-sre audit --verify",
            output=[
                ("ok", "Tool audit chain is valid."),
                ("dim", "every tool call: actor, tool, outcome, auth_mode=token"),
                ("dim", "hash chained outside the read-only evidence store"),
            ],
            narration=(
                "Every authenticated tool call writes an actor attributed row to a hash chained "
                "audit log outside the read only evidence store, and the chain verifies."
            ),
        ),
        Scene(
            label="09 / CREDENTIAL CONTRACT",
            command="mcp_token_probe.py --config <agent> --expect ok|missing|foreign",
            output=[
                ("ok", "probe OK: memory-researcher expect=ok"),
                ("ok", "probe OK: memory-researcher expect=missing"),
                ("ok", "probe OK: memory-researcher expect=foreign"),
                ("dim", "ok = token accepted; missing = fails closed; foreign = actor mismatch"),
            ],
            narration=(
                "The stdio probes prove the credential contract end to end. A valid token is "
                "accepted, a missing token fails closed, and a token for another actor is rejected."
            ),
        ),
        Scene(
            label="10 / LEAST PRIVILEGE",
            command="# live stdio transcript - tools/call",
            output=[
                ("out", "memory-researcher  -> get"),
                ("err", '  {"ok": false, "error": "MCP actor is not authorized for this tool"}'),
                ("out", "memory-researcher  -> memory"),
                ("ok", '  {"ok": true, "count": 0, "leads": []}'),
                ("out", "incident-commander -> get"),
                ("warn", '  {"ok": false, "error": "Investigation not found"}'),
                ("dim", "the commander is authorized; the memory researcher is not"),
            ],
            narration=(
                "The memory researcher cannot read evidence at all, while the commander can. "
                "The server enforces the signed claims on every call."
            ),
        ),
        Scene(
            label="11 / OS ISOLATION",
            command="bash deploy/mcp-container/verify.sh",
            output=[
                ("ok", "PASS uid-isolation"),
                ("ok", "PASS filesystem-isolation"),
                ("ok", "PASS network-isolation"),
                ("ok", "PASS mcp-stdio-handshake"),
                ("ok", "PASS evidence-store"),
                ("ok", "ISOLATION-OK all probes passed"),
            ],
            narration=(
                "The hardened MCP container gate passes five probes, including serving a digest "
                "anchored evidence snapshot read only, with no network and no root."
            ),
        ),
        Scene(
            label="12 / OUTRO",
            kind="title",
            title="Evidence or abstain.",
            subtitle=[
                "docs/SHOWCASE.md",
                "switch console setup  -  incident  -  audit --verify",
                "Read-only by design. No cluster changes.",
            ],
            narration=(
                "Everything is in the repository under docs slash showcase. Read only by design. "
                "Evidence or abstain."
            ),
            min_hold=6.0,
        ),
    ]


def audio_duration(path: Path) -> float:
    result = subprocess.run(["afinfo", str(path)], capture_output=True, text=True, check=True)
    for line in result.stdout.splitlines():
        if "estimated duration" in line:
            return float(line.split(":")[1].strip().split()[0])
    raise RuntimeError(f"could not read duration of {path}")


def make_narration(text: str, path: Path, voice: str) -> float:
    subprocess.run(
        ["say", "-v", voice, "-o", str(path), text],
        check=True,
    )
    return audio_duration(path)


def scene_duration(scene: Scene, audio: float) -> float:
    if scene.kind == "title":
        return max(audio + 1.2, scene.min_hold)
    typing = 0.25 + len(scene.command or "") / 55
    visual = typing + 0.3 + 0.11 * len(scene.output) + 0.3 + 2.6
    return max(visual, audio + 1.0, scene.min_hold)


def render_video(scenes: list[Scene], tmp: Path, out: Path, voice: str) -> None:
    ffmpeg = imageio_ffmpeg.get_ffmpeg_exe()
    segments: list[Path] = []
    for index, scene in enumerate(scenes):
        print(f"[{index + 1}/{len(scenes)}] {scene.label}", flush=True)
        aiff = tmp / f"scene_{index}.aiff"
        audio = make_narration(scene.narration, aiff, voice)
        duration = scene_duration(scene, audio)
        frames = int(round(duration * FPS))

        silent = tmp / f"scene_{index}_silent.mp4"
        writer = imageio_ffmpeg.write_frames(
            str(silent),
            (W, H),
            fps=FPS,
            codec="libx264",
            pix_fmt_in="rgb24",
            pix_fmt_out="yuv420p",
            output_params=["-preset", "veryfast", "-crf", "20", "-movflags", "+faststart"],
        )
        writer.send(None)
        for frame_index in range(frames):
            t = frame_index / FPS
            frame = Image.new("RGB", (W, H), BG)
            draw = ImageDraw.Draw(frame)
            if scene.kind == "title":
                draw_title(frame, scene)
            else:
                draw_terminal(frame, scene, t)
            draw_subtitle(draw, scene.narration)
            writer.send(frame.tobytes())
        writer.close()

        segment = tmp / f"scene_{index}.mp4"
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-hide_banner",
                "-loglevel",
                "error",
                "-i",
                str(silent),
                "-i",
                str(aiff),
                "-c:v",
                "copy",
                "-c:a",
                "aac",
                "-b:a",
                "128k",
                "-af",
                "apad",
                "-t",
                f"{duration:.3f}",
                str(segment),
            ],
            check=True,
        )
        segments.append(segment)

    listing = tmp / "concat.txt"
    listing.write_text("".join(f"file '{path}'\n" for path in segments))
    out.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            ffmpeg,
            "-y",
            "-hide_banner",
            "-loglevel",
            "error",
            "-f",
            "concat",
            "-safe",
            "0",
            "-i",
            str(listing),
            "-c",
            "copy",
            "-movflags",
            "+faststart",
            str(out),
        ],
        check=True,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, default=Path("docs/media/showcase.mp4"))
    parser.add_argument("--voice", default="Samantha")
    args = parser.parse_args()

    if shutil.which("say") is None:
        parser.error("macOS `say` is required to generate narration")
    tmp = Path(tempfile.mkdtemp(prefix="showcase-"))
    try:
        render_video(scene_plan(), tmp, args.out, args.voice)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    size = args.out.stat().st_size / 1e6
    print(f"wrote {args.out} ({size:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

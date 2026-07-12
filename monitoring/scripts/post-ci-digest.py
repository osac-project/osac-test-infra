#!/usr/bin/env python3
"""post-ci-digest.py -- Daily Slack digest of OSAC CI health.

Pulls from the workflow-exporter's JSON API (must run where that's
reachable -- the monitoring-central host, since the exporter binds
127.0.0.1:9103), renders a single designed dashboard-style PNG (matplotlib)
summarizing everything, and posts it to Slack as the entire message (a
short caption plus the image, no separate text digest).

Needs a Slack bot token (chat:write + files:write), not just an incoming
webhook -- webhooks can only post text/Block Kit JSON, they can't upload
files. Uses secret/osac/monitoring/slack-bot (separate app/token from
slack-webhook-url, which Alertmanager and the e2e failure notification use
and which can't upload files either).

Env vars:
  EXPORTER_URL       - base URL of the workflow-exporter (default http://127.0.0.1:9103)
  SLACK_BOT_TOKEN    - Slack bot token (xoxb-...), required unless DRY_RUN
  SLACK_CHANNEL_ID   - Slack channel ID to post to, required unless DRY_RUN
  DRY_RUN            - if "true", save the image to ./ci-digest-preview.png
                        instead of posting to Slack
"""

from __future__ import annotations

import io
import os
import sys
import textwrap
from datetime import datetime, timedelta, timezone

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Circle, FancyBboxPatch, Wedge  # noqa: E402
import numpy as np  # noqa: E402
import requests  # noqa: E402

EXPORTER_URL = os.getenv("EXPORTER_URL", "http://127.0.0.1:9103")
SLACK_BOT_TOKEN = os.getenv("SLACK_BOT_TOKEN")
SLACK_CHANNEL_ID = os.getenv("SLACK_CHANNEL_ID")
DRY_RUN = os.getenv("DRY_RUN", "").lower() in ("true", "1", "yes")

# Palette matching the reference dashboard mockup: light cyan-to-blue
# gradient background, white "headline" cards, dark navy "detail" cards
# with a cyan border, red/gray gauges.
COLOR_NAVY = "#0b2545"
COLOR_CARD_DARK = "#0f2f52"
COLOR_CARD_LIGHT = "#f2f9fc"
COLOR_BORDER_CYAN = "#4fc3f7"
COLOR_GREEN = "#2ecc71"
COLOR_YELLOW = "#f1c40f"
COLOR_RED = "#e74c3c"
COLOR_GRAY = "#95a5a6"
COLOR_TEXT_LIGHT = "#eaf6ff"
COLOR_TEXT_MUTED = "#9fb8cc"


def _get(path, **params):
    resp = requests.get(f"{EXPORTER_URL}{path}", params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def _iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def pct(rate):
    return f"{rate * 100:.1f}%" if rate is not None else "n/a"


def status_color(rate):
    """Same thresholds as ci-health.json's Overall Pass Rate panel."""
    if rate is None:
        return COLOR_GRAY
    if rate < 0.5:
        return COLOR_RED
    if rate < 0.8:
        return COLOR_YELLOW
    return COLOR_GREEN


def decisive_rate(counts):
    """/api/counts always returns success_rate=0 (not None) when there are
    zero success+failure jobs in the window (see get_counts_json) -- return
    None in that case so callers can render "no runs" instead of a
    misleading 0.0%."""
    if counts["success"] + counts["failure"] == 0:
        return None
    return counts["success_rate"]


def fetch_periodic_success_rate(since):
    return _get("/api/counts", job_type="periodic", category="e2e", since=_iso(since))


def fetch_presubmit_infra_failures(since):
    return _get(
        "/api/presubmit-infra-failures",
        job_type="presubmit", category="e2e", since=_iso(since),
    )


def fetch_pr_merge_time(since):
    return _get("/api/pr-merge-time", since=_iso(since))


def fetch_overall_flake_rate(since):
    """/api/flake-rate returns per-workflow entries -- aggregate across all
    e2e workflows for a single overall figure, same "sum then divide"
    approach as the exporter's own overall pass-rate calc (avoids drift
    from averaging already-rounded per-workflow rates)."""
    rows = _get("/api/flake-rate", category="e2e", since=_iso(since))
    flaky = sum(r["flaky_passes"] for r in rows)
    total = sum(r["total_successes"] for r in rows)
    return round(flaky / total, 4) if total else None


def fetch_overall_mttr(since):
    data = _get("/api/mttr", category="e2e", since=_iso(since))
    return data.get("overall")


def fetch_top_failing_workflow(since):
    rows = _get("/api/counts-by-workflow", merge_similar="Yes", category="e2e", since=_iso(since))
    failing = [r for r in rows if r.get("failure", 0) > 0]
    failing.sort(key=lambda r: r["failure"], reverse=True)
    return failing[0] if failing else None


# -- drawing helpers ----------------------------------------------------


def _gradient_background(fig):
    """Light-cyan-to-navy diagonal gradient spanning the whole figure,
    matching the reference mockup's background."""
    ax = fig.add_axes((0, 0, 1, 1), zorder=-10)
    ax.axis("off")
    top_left = np.array([0xe9, 0xf6, 0xfc])
    bottom_right = np.array([0x14, 0x3d, 0x66])
    n = 256
    grad = np.zeros((n, n, 3))
    for i in range(n):
        for j in range(n):
            t = (i + j) / (2 * n)
            grad[i, j] = top_left * (1 - t) + bottom_right * t
    ax.imshow(grad / 255.0, extent=(0, 1, 0, 1), aspect="auto", origin="upper")
    return ax


def _card(fig, rect, dark):
    """rect = (x, y, w, h) in figure-fraction coordinates."""
    facecolor = COLOR_CARD_DARK if dark else COLOR_CARD_LIGHT
    edgecolor = COLOR_BORDER_CYAN if dark else "#c7e6f5"
    patch = FancyBboxPatch(
        (rect[0], rect[1]), rect[2], rect[3],
        boxstyle="round,pad=0,rounding_size=0.015",
        linewidth=1.6, edgecolor=edgecolor, facecolor=facecolor,
        transform=fig.transFigure, zorder=1,
    )
    fig.add_artist(patch)


def _small_axes(fig, cx, cy, r):
    """A square-in-*pixels* axes centered at (cx, cy) [figure-fraction],
    with data coordinates -1..1 on both axes. fig.transFigure is normalized
    0-1 on both x and y regardless of the figure's actual aspect ratio, so
    a Circle/Wedge drawn directly on it (as the first version of this
    script did) renders as an ellipse on any non-square figure. Isolating
    each icon in its own equal-aspect axes sidesteps that entirely.
    """
    fig_w, fig_h = fig.get_size_inches()
    aspect = fig_h / fig_w
    w = 2 * r
    h = w / aspect
    ax = fig.add_axes((cx - w / 2, cy - h / 2, w, h), zorder=6)
    ax.set_xlim(-1, 1)
    ax.set_ylim(-1, 1)
    ax.set_aspect("equal")
    ax.axis("off")
    ax.patch.set_alpha(0)
    return ax


def _checkmark(fig, x, y, r, color):
    ax = _small_axes(fig, x, y, r)
    ax.add_patch(Circle((0, 0), 0.85, color=color))
    ax.text(0, -0.02, "✓", ha="center", va="center", fontsize=r * 1000,
            color="white", fontweight="bold")


def _gauge(fig, cx, cy, r, infra_frac, has_data):
    """Half-donut gauge (dome shape, opens downward) split into test
    (green, right side) / infra (red, left side) portions by angular
    fraction. Sweeps counterclockwise from the right point through the top
    to the left point -- infra failures are CI's own fault, so they get
    the "alarming" red; test failures are the expected/normal category
    here and get green. Rendered as a single neutral gray dome when
    has_data is False (zero failures in the window) -- otherwise
    infra_frac=0 would render as a "fully green" dome that reads as "100%
    test failures" rather than "no failures at all".
    """
    ax = _small_axes(fig, cx, cy, r)
    if not has_data:
        ax.add_patch(Wedge((0, 0), 0.95, 0, 180, width=0.38,
                            facecolor=COLOR_GRAY, edgecolor="none", linewidth=0))
        return
    test_frac = 1 - infra_frac
    test_deg = 180 * test_frac
    ax.add_patch(Wedge((0, 0), 0.95, 0, test_deg, width=0.38,
                        facecolor=COLOR_GREEN, edgecolor="none", linewidth=0))
    ax.add_patch(Wedge((0, 0), 0.95, test_deg, 180, width=0.38,
                        facecolor=COLOR_RED, edgecolor="none", linewidth=0))


def _text(fig, x, y, s, fontsize, color, weight="normal", ha="left", va="center", style="normal"):
    fig.text(x, y, s, fontsize=fontsize, color=color, fontweight=weight,
              ha=ha, va=va, fontstyle=style, zorder=5)


def _truncate(s, max_len=26):
    """Bounds a label's rendered width regardless of source-data length --
    repo names/durations are drawn manually at a small fixed offset from a
    reference point (not via a mechanism that scales its own reserved
    space to the string, like axes tick labels do), so an unexpectedly
    long string would otherwise run past its card."""
    return s if len(s) <= max_len else s[: max_len - 1] + "…"


# -- image assembly -------------------------------------------------------


def build_digest_image(now):
    h24 = now - timedelta(hours=24)
    h72 = now - timedelta(hours=72)
    d7 = now - timedelta(days=7)

    periodic_24h = fetch_periodic_success_rate(h24)
    periodic_72h = fetch_periodic_success_rate(h72)
    infra_24h = fetch_presubmit_infra_failures(h24)
    infra_72h = fetch_presubmit_infra_failures(h72)
    merge_time = fetch_pr_merge_time(d7)
    flake_rate = fetch_overall_flake_rate(d7)
    mttr = fetch_overall_mttr(d7)
    top_failing = fetch_top_failing_workflow(h24)

    fig = plt.figure(figsize=(14, 8.5), dpi=180)
    _gradient_background(fig)

    _text(fig, 0.03, 0.945, "OSAC CI Daily Digest", 30, COLOR_NAVY, weight="bold")
    _text(fig, 0.03, 0.895, now.strftime("%Y-%m-%d %H:%M UTC"), 15, "#3a5a78")

    # -- Card A: Periodic E2E Success Rate (top-left, light) --------------
    a_rect = (0.02, 0.585, 0.47, 0.275)
    _card(fig, a_rect, dark=False)
    _text(fig, a_rect[0] + 0.02, a_rect[1] + a_rect[3] - 0.035,
          "Periodic E2E Success Rate", 17, COLOR_NAVY, weight="bold")

    for i, (label, counts) in enumerate((("Last 24h", periodic_24h), ("Last 72h", periodic_72h))):
        cx = a_rect[0] + 0.025 + i * 0.225
        cw = 0.20
        cy = a_rect[1] + 0.035
        ch = a_rect[3] - 0.10
        _card(fig, (cx, cy, cw, ch), dark=False)
        rate = decisive_rate(counts)
        total = counts["success"] + counts["failure"]
        _text(fig, cx + cw / 2, cy + ch - 0.045, label, 13, "#3a5a78", ha="center")
        _text(fig, cx + cw / 2 - 0.02, cy + ch / 2 - 0.01, pct(rate), 24, COLOR_NAVY,
              weight="bold", ha="center")
        _checkmark(fig, cx + cw - 0.035, cy + ch / 2, 0.018, status_color(rate))
        _text(fig, cx + cw / 2, cy + 0.035, f"({counts['success']}/{total})", 12, "#5a7a98", ha="center")

    # -- Card B: Presubmit E2E Failures -- Infra vs. Test (top-right, dark)
    b_rect = (0.51, 0.35, 0.47, 0.51)
    _card(fig, b_rect, dark=True)
    _text(fig, b_rect[0] + 0.02, b_rect[1] + b_rect[3] - 0.04,
          "Presubmit E2E Failures — Infra vs. Test", 16, COLOR_TEXT_LIGHT, weight="bold")
    _text(fig, b_rect[0] + 0.02, b_rect[1] + b_rect[3] - 0.075,
          "Infra = CI's fault (setup/teardown), not the product's", 10.5, COLOR_TEXT_MUTED)

    for i, (label, data) in enumerate((("Last 24h", infra_24h), ("Last 72h", infra_72h))):
        gx = b_rect[0] + 0.14 + i * 0.22
        gy = b_rect[1] + 0.20
        infra_n, test_n, tot = data["infra_total"], data["test_total"], data["total_failures"]
        frac = infra_n / tot if tot else 0
        # Dome opens downward -- title sits above the dome's peak, counts
        # sit below its flat base.
        _text(fig, gx, gy + 0.19, label, 13, COLOR_TEXT_LIGHT, weight="bold", ha="center")
        _gauge(fig, gx, gy, 0.08, frac, has_data=tot > 0)
        _text(fig, gx - 0.085, gy - 0.05, f"{infra_n}\ninfra", 11, COLOR_RED, ha="center", weight="bold")
        _text(fig, gx + 0.085, gy - 0.05, f"{test_n}\ntest", 11, COLOR_GREEN, ha="center", weight="bold")
        _text(fig, gx, gy - 0.14, f"({tot} total)", 10, COLOR_TEXT_MUTED, ha="center")

    # Wrapped defensively: INFRA_STEPS names are fixed/known, but with
    # several distinct steps failing in the same window the joined summary
    # could otherwise run past the card (or the whole figure) as one
    # unbroken line.
    step_lines = []
    s24 = infra_24h["infra_by_step"]
    s72 = infra_72h["infra_by_step"]
    if s24:
        step_lines.append("24h: " + ", ".join(f"{s['step']} ({s['count']})" for s in s24))
    if s72:
        step_lines.append("72h: " + ", ".join(f"{s['step']} ({s['count']})" for s in s72))
    if step_lines:
        wrapped = "\n".join(
            "\n".join(textwrap.wrap(line, width=78)) for line in step_lines
        )
        _text(fig, b_rect[0] + 0.02, b_rect[1] + 0.02, wrapped, 9.5, COLOR_TEXT_MUTED)

    # -- Card C: Avg Time to Merge (bottom-left, dark) ---------------------
    c_rect = (0.02, 0.02, 0.47, 0.545)
    _card(fig, c_rect, dark=True)
    _text(fig, c_rect[0] + 0.02, c_rect[1] + c_rect[3] - 0.04,
          "Avg Time to Merge (7d)", 16, COLOR_TEXT_LIGHT, weight="bold")
    _text(fig, c_rect[0] + 0.02, c_rect[1] + c_rect[3] - 0.075,
          f"{merge_time['avg_merge_display']} across {merge_time['count']} PRs", 13, COLOR_TEXT_LIGHT)

    by_repo = merge_time["by_repo"]
    if by_repo:
        # Repo names are drawn manually (not via barh's automatic y-tick
        # labels) so their width is fully under control -- relying on
        # tick-label auto-placement let long names like
        # "fulfillment-service" overflow past the card's left edge.
        label_margin = 0.16
        chart_ax = fig.add_axes((
            c_rect[0] + label_margin, c_rect[1] + 0.03,
            c_rect[2] - label_margin - 0.04, c_rect[3] - 0.16,
        ), zorder=2)
        chart_ax.patch.set_alpha(0)
        sorted_repos = sorted(by_repo, key=lambda r: r["avg_merge_seconds"])
        hours = [r["avg_merge_seconds"] / 3600 for r in sorted_repos]
        # A scale of 0 (single repo, or every repo at 0s) would collapse
        # both label offsets below to x=0, drawing the repo name and the
        # value label on top of each other as garbled overlapping text --
        # fall back to a fixed unit scale in that case.
        x_scale = max(hours) if max(hours) > 0 else 1.0
        bar_colors = plt.cm.Blues(np.linspace(0.4, 0.9, len(hours)))
        y_pos = range(len(sorted_repos))
        bars = chart_ax.barh(list(y_pos), hours, color=bar_colors)
        for y, bar, r in zip(y_pos, bars, sorted_repos):
            chart_ax.text(
                bar.get_width() + x_scale * 0.03, y,
                _truncate(f"{r['avg_merge_display']} ({r['count']})"),
                va="center", fontsize=9.5, color=COLOR_TEXT_LIGHT,
            )
            # Repo names are drawn in the reserved label_margin, which sits
            # outside this axes' own data range by design -- clip_on=True
            # would clip against the *axes'* bounding box, not the card,
            # hiding these entirely rather than just trimming long ones.
            # Truncating the string instead bounds the render width without
            # relying on matplotlib's clip box lining up with the margin.
            chart_ax.text(
                -x_scale * 0.04, y, _truncate(r["repo"]),
                va="center", ha="right", fontsize=10.5, color=COLOR_TEXT_LIGHT,
            )
        chart_ax.set_xlim(0, x_scale * 1.5)
        chart_ax.set_ylim(-0.6, len(sorted_repos) - 0.4)
        chart_ax.axis("off")

    # -- Card D: Additional Stability Signals (bottom-right, dark) ---------
    d_rect = (0.51, 0.02, 0.47, 0.31)
    _card(fig, d_rect, dark=True)
    _text(fig, d_rect[0] + 0.02, d_rect[1] + d_rect[3] - 0.045,
          "Additional Stability Signals (7d)", 15, COLOR_TEXT_LIGHT, weight="bold")

    flake_str = pct(flake_rate) if flake_rate is not None else "no successes yet"
    mttr_str = mttr["mttr_display"] if mttr else "no recoveries yet"
    # No emoji here -- this is baked into a rasterized PNG (matplotlib's
    # Agg backend uses DejaVu Sans, which has no color-emoji glyphs), not
    # sent as Slack markdown, so it would render as a missing-glyph box.
    top_str = (
        f"{top_failing['workflow']} ({top_failing['failure']})" if top_failing else "None"
    )
    lines = [
        ("Flake rate", flake_str),
        ("MTTR", mttr_str),
        ("Top failing workflow (24h)", top_str),
    ]
    for i, (label, value) in enumerate(lines):
        ly = d_rect[1] + d_rect[3] - 0.10 - i * 0.075
        _text(fig, d_rect[0] + 0.03, ly, f"•  {label}:", 12, COLOR_TEXT_MUTED)
        _text(fig, d_rect[0] + d_rect[2] - 0.03, ly, value, 12, COLOR_TEXT_LIGHT,
              weight="bold", ha="right")

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
    plt.close(fig)
    return buf.getvalue()


# -- Slack delivery ---------------------------------------------------------


def upload_image(png_bytes, filename, caption):
    """Uploads the digest image directly as a new channel message (no
    separate text message) via Slack's current (2024+) external-upload
    flow -- the older files.upload endpoint is deprecated. initial_comment
    becomes the message text, and posting without thread_ts shares it to
    the channel as a top-level message."""
    resp = requests.post(
        "https://slack.com/api/files.getUploadURLExternal",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        data={"filename": filename, "length": len(png_bytes)},
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        print(f"Slack API error (files.getUploadURLExternal): {data.get('error')}", file=sys.stderr)
        sys.exit(1)
    upload_url, file_id = data["upload_url"], data["file_id"]

    up = requests.post(upload_url, files={"file": (filename, png_bytes, "image/png")}, timeout=60)
    up.raise_for_status()

    resp = requests.post(
        "https://slack.com/api/files.completeUploadExternal",
        headers={"Authorization": f"Bearer {SLACK_BOT_TOKEN}"},
        json={
            "files": [{"id": file_id, "title": "OSAC CI Daily Digest"}],
            "channel_id": SLACK_CHANNEL_ID,
            "initial_comment": caption,
        },
        timeout=30,
    )
    resp.raise_for_status()
    data = resp.json()
    if not data.get("ok"):
        print(f"Slack API error (files.completeUploadExternal): {data.get('error')}", file=sys.stderr)
        sys.exit(1)


def main():
    now = datetime.now(timezone.utc)
    png_bytes = build_digest_image(now)

    if DRY_RUN:
        path = "ci-digest-preview.png"
        with open(path, "wb") as f:
            f.write(png_bytes)
        print(f"Saved preview image: {path}", file=sys.stderr)
        return

    if not SLACK_BOT_TOKEN or not SLACK_CHANNEL_ID:
        print("SLACK_BOT_TOKEN and SLACK_CHANNEL_ID are required unless DRY_RUN=true", file=sys.stderr)
        sys.exit(1)

    caption = f"📊 OSAC CI Daily Digest — {now.strftime('%Y-%m-%d %H:%M UTC')}"
    upload_image(png_bytes, "ci-digest.png", caption)


if __name__ == "__main__":
    main()

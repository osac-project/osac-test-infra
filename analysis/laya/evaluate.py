#!/usr/bin/env python3
"""Replay source-attributed E2E failure evidence through the pinned Laya schema."""

from __future__ import annotations

import argparse
import json
import re
import statistics
import time
import urllib.request
from collections import Counter
from pathlib import Path
from typing import Any

from reextract import load_extractor

DEFAULT_ENDPOINT = "https://laya-server-laya.apps.cnv2.engineering.redhat.com/predict/batch"
SCHEMA = json.loads(Path(__file__).with_name("schema.json").read_text())
CLASSIFY_STAGE = load_extractor(Path(__file__).resolve().parents[2]).classify_stage
CRITERIA = SCHEMA["questions"]["global"]["failure_domain"]["criteria"]
GLOBAL_QUESTION = SCHEMA["questions"]["global"]


def compact_state(state: dict[str, str]) -> dict[str, str]:
    out = {"failed_step": state["failed_step"]}
    for key in ("job_error", "pod_error", "traceback"):
        lines = [re.sub(r"^[^:]+:\d+:\s*", "", line) for line in state[key].splitlines()]
        out[key] = " | ".join(lines)[: 340 if key == "pod_error" else 280]
    return out


def state_for_arm(state: dict[str, str], arm: str) -> dict[str, str]:
    if arm == "short":
        return {"failed_step": state["failed_step"], "job_error": state["job_error"][:360]}
    if arm == "compact":
        return compact_state(state)
    return state


def predict_batch(
    items: list[tuple[dict[str, Any], dict[str, str]]], question: dict[str, Any], endpoint: str
) -> tuple[float, list[dict[str, Any]]]:
    payload = {"states": [state for _, state in items], "questions": question, "model": SCHEMA["model"]}
    request = urllib.request.Request(
        endpoint, data=json.dumps(payload).encode(), headers={"content-type": "application/json"}
    )
    started = time.monotonic()
    with urllib.request.urlopen(request, timeout=180) as response:
        body = json.load(response)
    elapsed = time.monotonic() - started
    results: list[dict[str, Any]] = body["results"]
    if len(results) != len(items):
        raise ValueError(f"Expected {len(items)} Laya results, received {len(results)}")
    return elapsed, results


def evaluate(cases: list[dict[str, Any]], arm: str, endpoint: str) -> dict[str, Any]:
    versions = {case.get("extractor_version") for case in cases}
    if len(versions) != 1 or None in versions:
        raise ValueError("Cases must share one extractor version")

    groups: dict[str, list[tuple[dict[str, Any], dict[str, str]]]] = {}
    for case in cases:
        stage = CLASSIFY_STAGE(case["state"]["failed_step"]) if arm == "stage" else "global"
        if arm == "stage" and stage in SCHEMA["direct_step_routes"]:
            allowed = {SCHEMA["direct_step_routes"][stage]}
        else:
            allowed = SCHEMA["questions"][stage]["failure_domain"]["criteria"]
        if case["label"] not in allowed:
            raise ValueError(f"PR #{case['pr']}: label {case['label']} unavailable for {stage} route")
        groups.setdefault(stage, []).append((case, state_for_arm(case["state"], arm)))

    predictions: list[dict[str, Any]] = []
    batches: list[dict[str, Any]] = []
    for stage, items in groups.items():
        if arm == "stage" and stage in ("e2e", "ci"):
            choice = SCHEMA["direct_step_routes"][stage]
            predictions.extend(
                {"pr": case["pr"], "choice": choice, "score": None, "tokens": 0, "method": "failed_step"}
                for case, _ in items
            )
            continue
        question = SCHEMA["questions"][stage] if arm == "stage" else GLOBAL_QUESTION
        for offset in range(0, len(items), 5):
            batch = items[offset : offset + 5]
            elapsed, results = predict_batch(batch, question, endpoint)
            batches.append({"stage": stage, "prs": [case["pr"] for case, _ in batch], "seconds": round(elapsed, 3)})
            for (case, _), result in zip(batch, results, strict=True):
                answer = result["answers"]["failure_domain"]
                predictions.append(
                    {
                        "pr": case["pr"],
                        "choice": answer["choice"],
                        "score": answer["answer_confidence"],
                        "probabilities": answer["probabilities"],
                        "tokens": result["usage"]["input_tokens"],
                        "method": "laya",
                    }
                )
            print(f"{stage}: {offset + len(batch)}/{len(items)} in {elapsed:.2f}s", flush=True)
    return {
        "arm": arm,
        "schema_version": SCHEMA["version"],
        "extractor_version": versions.pop(),
        "predictions": predictions,
        "batches": batches,
    }


def summarize(cases: list[dict[str, Any]], result: dict[str, Any]) -> str:
    by_pr = {prediction["pr"]: prediction for prediction in result["predictions"]}
    if len(by_pr) != len(cases):
        raise ValueError("Predictions do not match the number of cases")
    labels = list(CRITERIA)
    matrix = Counter((case["label"], by_pr[case["pr"]]["choice"]) for case in cases)
    correct = sum(matrix[label, label] for label in labels)
    lines = [f"# {result['arm']} evaluation", "", f"Correct: {correct}/{len(cases)}", ""]
    lines.extend(("| Actual \\ Predicted | " + " | ".join(labels) + " |", "|---|" + "---:|" * len(labels)))
    for actual in labels:
        lines.append("| " + actual + " | " + " | ".join(str(matrix[actual, predicted]) for predicted in labels) + " |")
    lines.extend(("", "| Actual | TP | FP | FN | Abstentions |", "|---|---:|---:|---:|---:|"))
    for label in labels:
        tp = matrix[label, label]
        fp = sum(matrix[actual, label] for actual in labels if actual != label)
        fn = sum(matrix[label, predicted] for predicted in labels if predicted != label)
        lines.append(f"| {label} | {tp} | {fp} | {fn} | {matrix[label, 'unknown']} |")
    wrong = [
        (case["pr"], case["label"], by_pr[case["pr"]]["choice"], by_pr[case["pr"]]["score"])
        for case in cases
        if case["label"] != by_pr[case["pr"]]["choice"]
    ]
    lines.extend(("", "Wrong routes:", ""))
    lines.extend(
        f"- PR #{pr}: {actual} → {predicted} ({score:.3f})"
        if score is not None
        else f"- PR #{pr}: {actual} → {predicted} (failed-step route)"
        for pr, actual, predicted, score in wrong
    )
    latencies = [batch["seconds"] for batch in result["batches"]]
    if latencies:
        lines.extend(("", f"Batch latency: median {statistics.median(latencies):.2f}s, max {max(latencies):.2f}s."))
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--cases", type=Path, default=Path(__file__).with_name("cases.json"))
    parser.add_argument("--split", choices=("development", "holdout"), required=True)
    parser.add_argument("--arm", choices=("short", "direct", "compact", "stage"), required=True)
    parser.add_argument("--endpoint", default=DEFAULT_ENDPOINT)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    cases = [case for case in json.loads(args.cases.read_text()) if case["split"] == args.split]
    result = evaluate(cases, args.arm, args.endpoint)
    result["split"] = args.split
    args.output.write_text(json.dumps(result, indent=2) + "\n")
    print(summarize(cases, result))


if __name__ == "__main__":
    main()

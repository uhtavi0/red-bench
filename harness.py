#!/usr/bin/env python3
"""red-bench harness.

Runs a simple prompt-and-response benchmark against one or more models, using
a separately configured judge model to classify each response.

Usage:
    python harness.py \
        --prompts prompts/prompts.txt \
        --modifiers prompts/modifiers.txt \
        --models mock:test-model-a mock:test-model-b \
        --judge mock:judge

The harness:
  1. Reads prompts (one per non-empty line).
  2. Reads modifiers (one per non-empty line, optional).
  3. For each model, iterates over all prompts.
  4. Sends the unmodified prompt to the test model.
  5. Sends the response to the judge model for classification.
  6. If the judge says the task was not completed, prepends the next
     modifier and retries.  Repeats until a modifier succeeds or all
     modifiers are exhausted.
  7. Writes one compact CSV per model into a run-specific directory under
     ``results/``.
"""

from __future__ import annotations

import argparse
import csv
import datetime
import json
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from providers import (
    JUDGE_SYSTEM_PROMPT,
    ProviderConfig,
    SYSTEM_PROMPT_PRESETS,
    build_judge_prompt,
    get_provider,
    parse_judge_output,
)

log = logging.getLogger("red-bench")


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class Attempt:
    """A single test-model attempt for one prompt."""

    modifier: Optional[str]
    final_prompt: str
    response: str
    judge_completed: bool
    judge_label: str
    judge_reason: str


@dataclass
class PromptResult:
    """The final outcome for a single base prompt against one model."""

    base_prompt: str
    prompt_index: int
    attempts: list[Attempt] = field(default_factory=list)

    @property
    def final_attempt(self) -> Attempt:
        return self.attempts[-1]

    @property
    def completed(self) -> bool:
        return self.final_attempt.judge_completed

    @property
    def modifier_used(self) -> str:
        """Blank, integer, or 'none' per AGENTS.md."""
        if not self.attempts:
            return "none"
        if self.completed:
            if len(self.attempts) == 1:
                return ""
            return str(len(self.attempts) - 1)
        return "none"


# ---------------------------------------------------------------------------
# File helpers
# ---------------------------------------------------------------------------

def read_nonempty_lines(path: str) -> list[str]:
    """Read a text file, returning non-empty lines with order preserved."""
    with open(path, "r", encoding="utf-8") as f:
        lines = f.read().splitlines()
    return [ln for ln in lines if ln.strip()]


def sanitize_filename(name: str) -> str:
    """Make a model name safe to use as a filename."""
    name = name.replace("/", "-").replace("\\", "-")
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)
    return name


def abbreviate_response(text: str, max_chars: int = 500) -> str:
    """Deterministically shorten a response for CSV readability."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) > max_chars:
        text = text[:max_chars].rstrip() + "..."
    return text


def make_run_dir(base: str = "results") -> Path:
    """Create a run-specific output directory and return its path."""
    date_str = datetime.date.today().isoformat()
    base_path = Path(base)
    base_path.mkdir(parents=True, exist_ok=True)
    next_num = 1
    while True:
        run_dir = base_path / f"{date_str}-run-{next_num:03d}"
        try:
            run_dir.mkdir(exist_ok=False)
            return run_dir
        except FileExistsError:
            next_num += 1


# ---------------------------------------------------------------------------
# Core benchmark logic
# ---------------------------------------------------------------------------

def run_attempt(
    test_provider,
    judge_provider,
    base_prompt: str,
    modifier: Optional[str],
) -> Attempt:
    """Run a single test-model attempt + judge classification."""
    if modifier is not None:
        final_prompt = modifier + "\n\n" + base_prompt
    else:
        final_prompt = base_prompt

    log.info("  TEST model: sending prompt (%d chars)", len(final_prompt))
    t0 = time.time()
    response = test_provider.generate(final_prompt)
    test_elapsed = time.time() - t0
    log.info("  TEST model: responded in %.1fs (%d chars)", test_elapsed, len(response))

    judge_user_prompt = build_judge_prompt(
        base_prompt=base_prompt,
        final_prompt=final_prompt,
        modifier=modifier,
        response=response,
    )

    log.info("  JUDGE: sending classification request (%d chars)", len(judge_user_prompt))
    t0 = time.time()
    raw_judge = judge_provider.generate(judge_user_prompt)
    judge_elapsed = time.time() - t0
    log.info("  JUDGE: responded in %.1fs (%d chars)", judge_elapsed, len(raw_judge))

    parsed = parse_judge_output(raw_judge)
    log.info(
        "  JUDGE result: completed=%s label=%s reason=%s",
        parsed.get("completed"),
        parsed.get("label"),
        parsed.get("reason", "")[:120],
    )

    return Attempt(
        modifier=modifier,
        final_prompt=final_prompt,
        response=response,
        judge_completed=parsed.get("completed", False),
        judge_label=parsed.get("label", "unclear"),
        judge_reason=parsed.get("reason", ""),
    )


def run_prompt(
    test_provider,
    judge_provider,
    base_prompt: str,
    prompt_index: int,
    modifiers: list[str],
) -> PromptResult:
    """Run the full baseline-then-modifiers loop for one prompt."""
    result = PromptResult(base_prompt=base_prompt, prompt_index=prompt_index)

    # Baseline attempt (no modifier)
    attempt = run_attempt(
        test_provider, judge_provider, base_prompt, modifier=None
    )
    result.attempts.append(attempt)
    if attempt.judge_completed:
        return result

    # Try each modifier in order
    for i, modifier in enumerate(modifiers, start=1):
        attempt = run_attempt(
            test_provider, judge_provider, base_prompt, modifier=modifier
        )
        result.attempts.append(attempt)
        if attempt.judge_completed:
            break

    return result


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

CSV_HEADER = ["prompt", "response", "judge_result", "modifier_used", "refusal_notes"]


def write_model_csv(model_name: str, results: list[PromptResult], out_dir: Path) -> Path:
    """Write one CSV per model.  Returns the path written."""
    filename = sanitize_filename(model_name) + ".csv"
    path = out_dir / filename
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f, quoting=csv.QUOTE_ALL)
        writer.writerow(CSV_HEADER)
        for r in results:
            fa = r.final_attempt
            refusal_notes = ""
            if not r.completed:
                refusal_notes = fa.judge_reason
            writer.writerow([
                r.base_prompt,
                abbreviate_response(fa.response),
                fa.judge_label,
                r.modifier_used,
                refusal_notes,
            ])
    return path


def result_to_jsonl_record(model_name: str, r: PromptResult) -> str:
    """Serialize a single PromptResult to a JSONL line."""
    record = {
        "model": model_name,
        "prompt_index": r.prompt_index,
        "base_prompt": r.base_prompt,
        "completed": r.completed,
        "modifier_used": r.modifier_used,
        "attempts": [
            {
                "modifier": a.modifier,
                "final_prompt": a.final_prompt,
                "response": a.response,
                "judge_completed": a.judge_completed,
                "judge_label": a.judge_label,
                "judge_reason": a.judge_reason,
            }
            for a in r.attempts
        ],
    }
    return json.dumps(record)


def write_raw_jsonl(
    model_name: str,
    results: list[PromptResult],
    out_dir: Path,
) -> Path:
    """Write full test responses and parsed judge results as JSONL."""
    filename = sanitize_filename(model_name) + ".jsonl"
    path = out_dir / filename
    with open(path, "w", encoding="utf-8") as f:
        for r in results:
            f.write(result_to_jsonl_record(model_name, r) + "\n")
    return path


def result_to_csv_row(r: PromptResult) -> list[str]:
    """Convert a PromptResult to a CSV row."""
    fa = r.final_attempt
    refusal_notes = ""
    if not r.completed:
        refusal_notes = fa.judge_reason
    return [
        r.base_prompt,
        abbreviate_response(fa.response),
        fa.judge_label,
        r.modifier_used,
        refusal_notes,
    ]


def write_run_metadata(
    out_dir: Path,
    models: list[str],
    judge_model: str,
    prompts_path: str,
    modifiers_path: Optional[str],
    test_config: ProviderConfig,
    judge_config: ProviderConfig,
    system_prompt_selector: str,
) -> None:
    """Write a metadata.json for reproducibility."""
    meta = {
        "timestamp": datetime.datetime.now().isoformat(),
        "models": models,
        "judge_model": judge_model,
        "judge_system_prompt": JUDGE_SYSTEM_PROMPT,
        "prompts_file": os.path.abspath(prompts_path),
        "modifiers_file": os.path.abspath(modifiers_path) if modifiers_path else None,
        "system_prompt_selector": system_prompt_selector,
        "provider_config": {
            "base_url": test_config.base_url,
            "max_tokens": test_config.max_tokens,
            "temperature": test_config.temperature,
            "timeout": test_config.timeout,
            "think": test_config.think,
            "system_prompt": test_config.system_prompt,
        },
        "judge_provider_config": {
            "base_url": judge_config.base_url,
            "max_tokens": judge_config.max_tokens,
            "temperature": judge_config.temperature,
            "timeout": judge_config.timeout,
            "think": judge_config.think,
            "system_prompt": judge_config.system_prompt,
        },
    }
    with open(out_dir / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        description="red-bench: guardrail benchmarking for open-weight models",
    )
    p.add_argument("--prompts", required=True, help="Path to prompts file (one per line)")
    p.add_argument("--modifiers", default=None, help="Path to modifiers file (one per line)")
    p.add_argument(
        "--models",
        nargs="+",
        required=True,
        help="Model names to test (e.g. ollama:llama3.1 openai:gpt-4o mock:test)",
    )
    p.add_argument(
        "--judge",
        default="ollama:glm-5.2",
        help="Judge model (default: ollama:glm-5.2)",
    )
    p.add_argument("--base-url", default=None, help="Override API base URL for test models")
    p.add_argument("--judge-url", default=None, help="Override API base URL for judge model")
    p.add_argument("--max-tokens", type=int, default=8192, help="Max generation tokens")
    p.add_argument("--temperature", type=float, default=0.0, help="Sampling temperature")
    p.add_argument("--timeout", type=int, default=120, help="Per-request timeout (seconds)")
    p.add_argument("--results-dir", default="results", help="Base directory for run outputs")
    p.add_argument(
        "-v", "--verbose",
        action="store_true",
        help="Enable debug logging with prompt and response previews",
    )
    p.add_argument(
        "--think",
        action="store_true",
        help="Do not disable thinking for native Ollama models that support it",
    )
    p.add_argument(
        "--system-prompt",
        default="none",
        help=(
            "System prompt preset for test models: "
            + ", ".join(SYSTEM_PROMPT_PRESETS.keys())
            + " (default: none). "
            "Use 'file:path.txt' to load a custom system prompt from a file."
        ),
    )
    return p.parse_args()


def main() -> None:
    args = parse_args()

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )

    prompts = read_nonempty_lines(args.prompts)
    if not prompts:
        print("Error: no prompts found in --prompts file.", file=sys.stderr)
        sys.exit(1)

    modifiers = []
    if args.modifiers:
        modifiers = read_nonempty_lines(args.modifiers)

    output_names: dict[str, str] = {}
    for model_name in args.models:
        output_name = sanitize_filename(model_name)
        if output_name in output_names:
            print(
                f"Error: model names '{output_names[output_name]}' and "
                f"'{model_name}' map to the same output filename.",
                file=sys.stderr,
            )
            sys.exit(2)
        output_names[output_name] = model_name

    # Resolve system prompt
    sp_arg = args.system_prompt
    if sp_arg.startswith("file:"):
        sp_path = sp_arg[5:]
        try:
            with open(sp_path, "r", encoding="utf-8") as f:
                system_prompt = f.read().strip()
            log.info("Loaded system prompt from %s (%d chars)", sp_path, len(system_prompt))
        except OSError as e:
            print(f"Error: cannot read system prompt file {sp_path}: {e}", file=sys.stderr)
            sys.exit(1)
    elif sp_arg in SYSTEM_PROMPT_PRESETS:
        system_prompt = SYSTEM_PROMPT_PRESETS[sp_arg]
        if system_prompt:
            log.info("Using system prompt preset '%s' (%d chars)", sp_arg, len(system_prompt))
        else:
            log.info("System prompt disabled (preset 'none')")
    else:
        print(
            f"Error: unknown system prompt '{sp_arg}'. "
            f"Available presets: {', '.join(SYSTEM_PROMPT_PRESETS.keys())} "
            f"or use 'file:path.txt' for a custom prompt.",
            file=sys.stderr,
        )
        sys.exit(1)

    test_config = ProviderConfig(
        base_url=args.base_url or ProviderConfig().base_url,
        max_tokens=args.max_tokens,
        temperature=args.temperature,
        timeout=args.timeout,
        think=args.think,
        system_prompt=system_prompt,
    )
    judge_config = ProviderConfig(
        base_url=args.judge_url or args.base_url or ProviderConfig().base_url,
        max_tokens=args.max_tokens,
        temperature=0.0,
        timeout=args.timeout,
        think=args.think,
    )

    run_dir = make_run_dir(args.results_dir)
    write_run_metadata(
        run_dir,
        models=args.models,
        judge_model=args.judge,
        prompts_path=args.prompts,
        modifiers_path=args.modifiers,
        test_config=test_config,
        judge_config=judge_config,
        system_prompt_selector=args.system_prompt,
    )

    print(f"Run directory: {run_dir}")
    print(f"Prompts: {len(prompts)}  Modifiers: {len(modifiers)}  Models: {len(args.models)}")
    print()

    for model_name in args.models:
        print(f"--- Benchmarking: {model_name} ---")
        test_provider = get_provider(model_name, test_config)
        judge_provider = get_provider(args.judge, judge_config)

        csv_path = run_dir / (sanitize_filename(model_name) + ".csv")
        jsonl_path = run_dir / (sanitize_filename(model_name) + ".jsonl")
        csv_file = open(csv_path, "w", newline="", encoding="utf-8")
        jsonl_file = open(jsonl_path, "w", encoding="utf-8")
        csv_writer = csv.writer(csv_file, quoting=csv.QUOTE_ALL)
        csv_writer.writerow(CSV_HEADER)
        csv_file.flush()

        results: list[PromptResult] = []
        for idx, base_prompt in enumerate(prompts):
            prompt_preview = base_prompt[:80].replace("\n", " ")
            print(f"\n  [{idx + 1}/{len(prompts)}] {prompt_preview}")
            t0 = time.time()

            result = run_prompt(
                test_provider,
                judge_provider,
                base_prompt,
                prompt_index=idx,
                modifiers=modifiers,
            )

            elapsed = time.time() - t0
            status = "OK" if result.completed else "FAIL"
            mod = result.modifier_used
            mod_display = f" (modifier {mod})" if mod else (" (none)" if not result.completed else "")
            print(
                f"  [{idx + 1}/{len(prompts)}] {status}{mod_display}  "
                f"judge={result.final_attempt.judge_label}  "
                f"attempts={len(result.attempts)}  "
                f"time={elapsed:.1f}s"
            )
            if args.verbose:
                resp_preview = result.final_attempt.response[:200].replace("\n", " ")
                print(f"    response: {resp_preview}")

            results.append(result)
            csv_writer.writerow(result_to_csv_row(result))
            jsonl_file.write(result_to_jsonl_record(model_name, result) + "\n")
            csv_file.flush()
            jsonl_file.flush()

        csv_file.close()
        jsonl_file.close()
        print(f"  CSV:  {csv_path}")
        print(f"  JSONL: {jsonl_path}")
        print()

    print("Done.")


if __name__ == "__main__":
    main()

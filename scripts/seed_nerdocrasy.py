#!/usr/bin/env python3
"""
seed_nerdocrasy.py — Import questions into nerdocrasy_questions table.

Usage:
    python scripts/seed_nerdocrasy.py --file questions.json [--dry-run] [--clear]

JSON format (array of objects):
    [
      {
        "level": 1,
        "question_type": "TEXT",
        "question_text": "¿Cuántos artículos tiene la Constitución de Venezuela de 1999?",
        "correct_answer": "IN",
        "explanation": "La CRBV tiene 350 artículos.",
        "options_label_in": "350",
        "options_label_out": "320",
        "active": true,

        // Optional visual fields:
        "visual_asset_url": null,
        "visual_asset_a_url": null,
        "visual_asset_b_url": null,
        "visual_alt": null,
        "visual_caption": null,
        "visual_layout": "SINGLE"
      },
      ...
    ]

Required fields per question:
  - level (int, 1-90)
  - question_type (TEXT | IMAGE | IMAGE_COMPARE | DIAGRAM | SCENARIO_VISUAL)
  - question_text (str)
  - correct_answer ("IN" | "OUT")
  - explanation (str) — shown after wrong answer
  - options_label_in (str) — label for IN button
  - options_label_out (str) — label for OUT button

Optional:
  - active (bool, default true)
  - visual_asset_url, visual_asset_a_url, visual_asset_b_url (str|null)
  - visual_alt (str|null)
  - visual_caption (str|null)
  - visual_layout ("SINGLE"|"SIDE_BY_SIDE", default "SINGLE")
  - stage (str) — auto-derived from level if omitted
"""

import argparse
import json
import os
import sys
from collections import Counter


def level_to_stage(level: int) -> str:
    if level <= 30:
        return "KNOW"
    elif level <= 60:
        return "THINK"
    return "TRAP"


REQUIRED = ["level", "question_type", "question_text", "correct_answer",
            "explanation", "options_label_in", "options_label_out"]

VALID_TYPES = {"TEXT", "IMAGE", "IMAGE_COMPARE", "DIAGRAM", "SCENARIO_VISUAL"}
VALID_ANSWERS = {"IN", "OUT"}
VALID_LAYOUTS = {"SINGLE", "SIDE_BY_SIDE"}


def validate(q: dict, idx: int) -> list:
    errors = []
    for f in REQUIRED:
        if f not in q or q[f] is None or str(q[f]).strip() == "":
            errors.append(f"[{idx}] missing required field: {f}")

    level = q.get("level")
    if not isinstance(level, int) or not (1 <= level <= 90):
        errors.append(f"[{idx}] level must be int 1-90, got: {level!r}")

    qt = q.get("question_type", "")
    if qt not in VALID_TYPES:
        errors.append(f"[{idx}] invalid question_type: {qt!r}")

    ca = q.get("correct_answer", "")
    if ca not in VALID_ANSWERS:
        errors.append(f"[{idx}] correct_answer must be 'IN' or 'OUT', got: {ca!r}")

    vl = q.get("visual_layout", "SINGLE")
    if vl not in VALID_LAYOUTS:
        errors.append(f"[{idx}] invalid visual_layout: {vl!r}")

    return errors


def build_row(q: dict) -> dict:
    level = q["level"]
    return {
        "level":              level,
        "stage":              q.get("stage") or level_to_stage(level),
        "question_type":      q["question_type"],
        "question_text":      q["question_text"].strip(),
        "correct_answer":     q["correct_answer"],
        "explanation":        q["explanation"].strip(),
        "options_label_in":   q["options_label_in"].strip(),
        "options_label_out":  q["options_label_out"].strip(),
        "active":             q.get("active", True),
        "visual_asset_url":   q.get("visual_asset_url"),
        "visual_asset_a_url": q.get("visual_asset_a_url"),
        "visual_asset_b_url": q.get("visual_asset_b_url"),
        "visual_alt":         q.get("visual_alt"),
        "visual_caption":     q.get("visual_caption"),
        "visual_layout":      q.get("visual_layout", "SINGLE"),
    }


def main():
    parser = argparse.ArgumentParser(description="Seed nerdocrasy_questions table")
    parser.add_argument("--file", required=True, help="Path to JSON question bank")
    parser.add_argument("--dry-run", action="store_true", help="Validate only, do not insert")
    parser.add_argument("--clear", action="store_true",
                        help="Delete all existing questions before inserting (use with caution)")
    parser.add_argument("--level", type=int, help="Only import questions for this level")
    parser.add_argument("--stage", choices=["KNOW", "THINK", "TRAP"],
                        help="Only import questions for this stage")
    args = parser.parse_args()

    # Load env
    supabase_url = os.environ.get("SUPABASE_URL")
    supabase_key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
    if not args.dry_run:
        if not supabase_url or not supabase_key:
            print("ERROR: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set in environment.")
            print("       Load from .env or export before running.")
            sys.exit(1)

    # Load JSON
    with open(args.file, "r", encoding="utf-8") as f:
        questions = json.load(f)

    if not isinstance(questions, list):
        print("ERROR: JSON must be an array of question objects.")
        sys.exit(1)

    print(f"Loaded {len(questions)} questions from {args.file}")

    # Filter
    if args.level:
        questions = [q for q in questions if q.get("level") == args.level]
        print(f"Filtered to level {args.level}: {len(questions)} questions")
    elif args.stage:
        level_ranges = {
            "KNOW":  set(range(1, 31)),
            "THINK": set(range(31, 61)),
            "TRAP":  set(range(61, 91)),
        }
        allowed = level_ranges[args.stage]
        questions = [q for q in questions if q.get("level") in allowed]
        print(f"Filtered to stage {args.stage}: {len(questions)} questions")

    # Validate
    all_errors = []
    rows = []
    for i, q in enumerate(questions):
        errs = validate(q, i + 1)
        if errs:
            all_errors.extend(errs)
        else:
            rows.append(build_row(q))

    if all_errors:
        print(f"\n{len(all_errors)} validation error(s):")
        for e in all_errors:
            print(f"  {e}")
        sys.exit(1)

    print(f"Validation OK — {len(rows)} rows ready")

    by_stage = Counter(r["stage"] for r in rows)
    print(f"  KNOW  (1-30):  {by_stage.get('KNOW', 0)} questions")
    print(f"  THINK (31-60): {by_stage.get('THINK', 0)} questions")
    print(f"  TRAP  (61-90): {by_stage.get('TRAP', 0)} questions")

    if args.dry_run:
        print("\nDry run complete — no changes made.")
        return

    # Connect to Supabase
    from supabase import create_client
    supabase = create_client(supabase_url, supabase_key)

    if args.clear:
        confirm = input("\nWARNING: --clear will DELETE all existing questions. Type 'yes' to confirm: ")
        if confirm.strip().lower() != "yes":
            print("Aborted.")
            sys.exit(0)
        supabase.table("nerdocrasy_questions").delete().neq(
            "id", "00000000-0000-0000-0000-000000000000"
        ).execute()
        print("Cleared existing questions.")

    # Insert in batches of 50
    BATCH = 50
    inserted = 0
    failed = 0
    for i in range(0, len(rows), BATCH):
        batch = rows[i:i + BATCH]
        try:
            supabase.table("nerdocrasy_questions").insert(batch).execute()
            inserted += len(batch)
            print(f"  Batch {i // BATCH + 1}: inserted {len(batch)} rows ({inserted}/{len(rows)})")
        except Exception as e:
            print(f"  ERROR in batch {i // BATCH + 1}: {e}")
            failed += len(batch)

    print(f"\nDone. Inserted: {inserted}, Failed: {failed}")
    if failed:
        sys.exit(1)


if __name__ == "__main__":
    main()

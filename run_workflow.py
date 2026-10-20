"""Command-line entry point for SWMM-Agentic3 workflow-stage agents."""

from __future__ import annotations

import argparse

from workflow_agents.orchestration.run_preparation import run_workflow_stage, workflow_result_to_text


def main() -> None:
    parser = argparse.ArgumentParser(description="Run SWMM-Agentic3 workflow-stage agents.")
    parser.add_argument("--model", default="", help="Model project name under models/.")
    parser.add_argument("--run-id", required=True, help="Run ID under models/<model>/runs/.")
    parser.add_argument(
        "--stage",
        default="",
        choices=["", "evidence_building"],
        help="Run exactly one stage. Defaults to the next legal stage.",
    )
    parser.add_argument(
        "--until",
        default="",
        choices=["", "evidence_building"],
        help="Requested final stage; execute one stage then wait for user confirmation.",
    )
    parser.add_argument("--rerun", action="store_true", help="Allow rerunning a stage.")
    parser.add_argument('--investigation-action', choices=['initial', 'continue', 'status', 'prepare', 'diagnose', 'evidence', 'verify', 'report', 'display_report', 'review'])
    parser.add_argument('--task-id', default='')
    parser.add_argument('--question', default='')
    parser.add_argument('--review-feedback', default='', help='JSON file with explicit review issues and visible evidence IDs')
    args = parser.parse_args()

    if args.investigation_action:
        import asyncio
        from workflow_agents.orchestration.investigation import advance_investigation
        import json
        from pathlib import Path
        feedback = json.loads(Path(args.review_feedback).read_text(encoding='utf-8')) if args.review_feedback else None
        result = asyncio.run(advance_investigation(args.model, args.run_id, args.investigation_action,
                                                  args.task_id, args.question, review_feedback=feedback))
        print(workflow_result_to_text(result))
        return

    result = run_workflow_stage(
        model_name=args.model,
        run_id=args.run_id,
        target_stage=args.stage,
        until_stage=args.until,
        rerun=args.rerun,
    )
    print(workflow_result_to_text(result))


if __name__ == "__main__":
    main()

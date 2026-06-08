#!/usr/bin/env python3
import argparse
import asyncio
import sys
from pathlib import Path

from rich.console import Console
from rich.table import Table
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

console = Console()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="bb-agent",
        description="Blackboard Auto-Grading Agent for ShanghaiTech University",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  bb-agent grade --course _12345_1 --assignment "Homework 1" \\
      --question "questions.txt" --answer "solutions.txt" --rubric strict

  bb-agent upload --course _12345_1 --column "签到分" --csv scores.csv

  bb-agent login --save-session
  bb-agent login --username 2022533131
        """,
    )

    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    login_parser = subparsers.add_parser("login", help="Login to Blackboard and save session")
    login_parser.add_argument("--username", help="Student ID / username")
    login_parser.add_argument("--password", help="Password")
    login_parser.add_argument("--save-session", action="store_true", default=True)
    login_parser.add_argument("--headless", action="store_true", help="Run browser in headless mode")

    grade_parser = subparsers.add_parser("grade", help="Auto-grade submissions for an assignment")
    grade_parser.add_argument("--course", required=True, help="Course ID from Blackboard URL")
    grade_parser.add_argument("--assignment", required=True, help="Assignment name (column name in Grade Center)")
    grade_parser.add_argument("--question", help="Path to file containing the question")
    grade_parser.add_argument("--answer", required=True, help="Path to file containing the standard answer")
    grade_parser.add_argument("--rubric", default="default", help="Rubric name (default, strict) or path to YAML")
    grade_parser.add_argument("--model", default="gpt-4o", help="LLM model to use")
    grade_parser.add_argument("--download-dir", default="./downloads", help="Directory to save submissions")
    grade_parser.add_argument("--output", default="./grades.csv", help="Output CSV file for grades")
    grade_parser.add_argument("--dry-run", action="store_true", help="Grade but don't upload to Blackboard")
    grade_parser.add_argument("--headless", action="store_true")

    upload_parser = subparsers.add_parser("upload", help="Upload scores from CSV to Grade Center")
    upload_parser.add_argument("--course", required=True, help="Course ID from Blackboard URL")
    upload_parser.add_argument("--column", required=True, help="Grade Center column name")
    upload_parser.add_argument("--csv", required=True, help="CSV file with columns: 姓名, 学号, 分数")
    upload_parser.add_argument("--name-col", default="姓名", help="Name column header")
    upload_parser.add_argument("--id-col", default="学号", help="Student ID column header")
    upload_parser.add_argument("--score-col", default="分数", help="Score column header")
    upload_parser.add_argument("--dry-run", action="store_true", help="Parse CSV only, don't upload")
    upload_parser.add_argument("--headless", action="store_true")

    test_parser = subparsers.add_parser("test-grade", help="Test grading with a single submission")
    test_parser.add_argument("--answer", required=True, help="Path to standard answer file")
    test_parser.add_argument("--submission", required=True, help="Path to student submission file")
    test_parser.add_argument("--question", help="Path to question file")
    test_parser.add_argument("--rubric", default="default")
    test_parser.add_argument("--model", default="gpt-4o")

    config_parser = subparsers.add_parser("config", help="Show current configuration")
    config_parser.add_argument("--show", action="store_true", default=True)

    return parser


async def cmd_login(args):
    from src.auth import BlackboardAuth
    from src.utils import load_env, load_config

    load_env()
    config = load_config()

    username = args.username or config.blackboard.username
    password = args.password or config.blackboard.password
    base_url = config.blackboard.base_url
    sso_url = config.blackboard.sso_url

    if not username or not password:
        console.print("[red]Error: Username and password required. Set in .env or use --username/--password[/red]")
        return 1

    auth = BlackboardAuth(base_url, sso_url, username, password, headless=args.headless)
    try:
        console.print("[yellow]Logging in to Blackboard...[/yellow]")
        await auth.login()
        if await auth.is_authenticated():
            session_file = config.get("session", {}).get("file", "session.json")
            await auth.save_session(session_file)
            console.print(f"[green]Login successful! Session saved to {session_file}[/green]")
        else:
            console.print("[red]Login appeared to succeed but session validation failed[/red]")
            return 1
    except Exception as e:
        console.print(f"[red]Login failed: {e}[/red]")
        return 1
    finally:
        await auth.close()
    return 0


async def cmd_grade(args):
    from src.auth import BlackboardAuth
    from src.submissions import SubmissionFetcher
    from src.grader import Grader, GraderConfig, Rubric, GradeResult
    from src.gradebook import GradebookUploader, GradeEntry
    from src.utils import load_env, load_config
    import yaml

    load_env()
    config = load_config()
    config_dict = config.model_dump()

    answer_text = Path(args.answer).read_text(encoding="utf-8")
    question_text = Path(args.question).read_text(encoding="utf-8") if args.question else ""

    rubric_path = f"prompts/rubric_{args.rubric}.yaml" if args.rubric in ("default", "strict") else args.rubric
    rubric_data = yaml.safe_load(Path(rubric_path).read_text(encoding="utf-8"))
    rubric = Rubric(
        name=rubric_data["name"],
        description=rubric_data["description"],
        criteria=[
            {"name": c["name"], "points": c["points"], "description": c["description"]}
            for c in rubric_data["criteria"]
        ],
        total_points=rubric_data["total_points"],
    )

    grader_config = GraderConfig(
        model=args.model,
        temperature=0.1,
        max_tokens=2000,
        api_key=config.grading.llm.api_key,
        base_url=config.grading.llm.base_url,
        batch_size=5,
        retry_attempts=3,
    )
    grader = Grader(grader_config)

    username = config.blackboard.username
    password = config.blackboard.password
    base_url = config.blackboard.base_url
    sso_url = config.blackboard.sso_url
    session_file = config.session.file

    auth = BlackboardAuth(base_url, sso_url, username, password, headless=args.headless)
    fetcher = SubmissionFetcher(auth, config_dict["submissions"])

    try:
        console.print("[yellow]Authenticating...[/yellow]")
        if Path(session_file).exists():
            await auth.load_session(session_file)
            if not await auth.is_authenticated():
                await auth.login()
        else:
            await auth.login()
        await auth.save_session(session_file)

        console.print(f"[yellow]Fetching submissions for '{args.assignment}'...[/yellow]")
        submissions = await fetcher.fetch_all(args.course, args.assignment, args.download_dir)
        console.print(f"[green]Found {len(submissions)} submissions[/green]")

        grades = []
        with Progress(SpinnerColumn(), TextColumn("[progress.description]{task.description}")) as progress:
            task = progress.add_task("Grading submissions...", total=len(submissions))
            for sub in submissions:
                try:
                    result = grader.grade(
                        student_name=sub.student_name,
                        student_id=sub.student_id,
                        submission_text=sub.content_text or f"[File: {sub.file_path}]",
                        question=question_text,
                        standard_answer=answer_text,
                        rubric=rubric,
                    )
                    grades.append(result)
                    progress.advance(task)
                except Exception as e:
                    console.print(f"[red]Failed to grade {sub.student_name}: {e}[/red]")

        table = Table(title=f"Grades: {args.assignment}")
        table.add_column("Student", style="cyan")
        table.add_column("ID", style="dim")
        table.add_column("Score", style="green")
        table.add_column("Feedback", style="yellow")
        for g in grades:
            feedback_short = (g.feedback or "")[:50] + ("..." if len(g.feedback or "") > 50 else "")
            table.add_row(g.student_name, g.student_id, f"{g.total_score}/{g.max_score}", feedback_short)
        console.print(table)

        csv_path = Path(args.output)
        import csv
        with open(csv_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["姓名", "学号", "分数", "反馈"])
            for g in grades:
                writer.writerow([g.student_name, g.student_id, g.total_score, g.feedback])
        console.print(f"[green]Grades saved to {csv_path}[/green]")

        if not args.dry_run:
            uploader = GradebookUploader(auth, config_dict["gradebook"])
            entries = [
                GradeEntry(student_name=g.student_name, student_id=g.student_id, score=g.total_score, feedback=g.feedback)
                for g in grades
            ]
            console.print(f"[yellow]Uploading {len(entries)} grades to Grade Center...[/yellow]")
            result = await uploader.upload_batch(args.course, args.assignment, entries)
            console.print(f"[green]Upload complete: {result['success']} success, {result['failed']} failed[/green]")
            for err in result.get("errors", []):
                console.print(f"  [red]- {err}[/red]")
        else:
            console.print("[dim]Dry run - grades not uploaded[/dim]")

    finally:
        await auth.close()

    return 0


async def cmd_upload(args):
    from src.auth import BlackboardAuth
    from src.csv_reader import ScoreTable, ScoreRecord
    from src.gradebook import GradebookUploader, GradeEntry
    from src.utils import load_env, load_config

    load_env()
    config = load_config()
    config_dict = config.model_dump()

    score_table = ScoreTable.from_csv(args.csv, args.name_col, args.id_col, args.score_col)
    errors = score_table.validate()
    if errors:
        console.print("[red]CSV validation errors:[/red]")
        for e in errors:
            console.print(f"  [red]- {e}[/red]")
        return 1

    summary = score_table.summary()
    console.print(f"[green]CSV loaded: {summary['count']} records[/green]")
    console.print(f"  Average: {summary['avg_score']:.1f}, Range: {summary['min_score']} - {summary['max_score']}")

    if args.dry_run:
        table = Table(title="Preview")
        table.add_column("Name")
        table.add_column("Student ID")
        table.add_column("Score")
        for r in score_table.to_records():
            table.add_row(r.name, r.student_id, str(r.score))
        console.print(table)
        console.print("[dim]Dry run - grades not uploaded[/dim]")
        return 0

    username = config.blackboard.username
    password = config.blackboard.password
    base_url = config.blackboard.base_url
    sso_url = config.blackboard.sso_url
    session_file = config.session.file

    auth = BlackboardAuth(base_url, sso_url, username, password)
    try:
        console.print("[yellow]Authenticating...[/yellow]")
        if Path(session_file).exists():
            await auth.load_session(session_file)
            if not await auth.is_authenticated():
                await auth.login()
        else:
            await auth.login()
        await auth.save_session(session_file)

        uploader = GradebookUploader(auth, config_dict["gradebook"])
        entries = [GradeEntry(student_name=r.name, student_id=r.student_id, score=r.score) for r in score_table.to_records()]

        console.print(f"[yellow]Uploading {len(entries)} grades to column '{args.column}'...[/yellow]")
        result = await uploader.upload_batch(args.course, args.column, entries)
        console.print(f"[green]Upload complete: {result['success']} success, {result['failed']} failed[/green]")
        for err in result.get("errors", []):
            console.print(f"  [red]- {err}[/red]")

    finally:
        await auth.close()

    return 0


async def cmd_test_grade(args):
    from src.grader import Grader, GraderConfig, Rubric
    import yaml

    answer_text = Path(args.answer).read_text(encoding="utf-8")
    submission_text = Path(args.submission).read_text(encoding="utf-8")
    question_text = Path(args.question).read_text(encoding="utf-8") if args.question else ""

    rubric_path = f"prompts/rubric_{args.rubric}.yaml" if args.rubric in ("default", "strict") else args.rubric
    rubric_data = yaml.safe_load(Path(rubric_path).read_text(encoding="utf-8"))
    rubric = Rubric(
        name=rubric_data["name"],
        description=rubric_data["description"],
        criteria=[
            {"name": c["name"], "points": c["points"], "description": c["description"]}
            for c in rubric_data["criteria"]
        ],
        total_points=rubric_data["total_points"],
    )

    config = GraderConfig(
        model=args.model,
        temperature=0.1,
        max_tokens=2000,
        batch_size=1,
        retry_attempts=3,
    )
    grader = Grader(config)

    console.print("[yellow]Running test grading...[/yellow]")
    result = grader.grade(
        student_name="Test Student",
        student_id="0000000000",
        submission_text=submission_text,
        question=question_text,
        standard_answer=answer_text,
        rubric=rubric,
    )

    console.print(f"\n[bold cyan]Score: {result.total_score}/{result.max_score}[/bold cyan]")
    console.print(f"[bold]Criteria Scores:[/bold]")
    for name, score in result.criteria_scores.items():
        console.print(f"  {name}: {score}")
    console.print(f"\n[bold]Feedback:[/bold]\n{result.feedback}")
    return 0


def cmd_config(args):
    from src.utils import load_env, load_config

    load_env()
    config = load_config()
    import yaml

    console.print(Panel(yaml.dump(config, default_flow_style=False, allow_unicode=True), title="Current Configuration"))
    return 0


def main():
    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        return 0

    command_map = {
        "login": cmd_login,
        "grade": cmd_grade,
        "upload": cmd_upload,
        "test-grade": cmd_test_grade,
        "config": cmd_config,
    }

    handler = command_map.get(args.command)
    if handler is None:
        console.print(f"[red]Unknown command: {args.command}[/red]")
        return 1

    if asyncio.iscoroutinefunction(handler):
        return asyncio.run(handler(args))
    return handler(args)


if __name__ == "__main__":
    sys.exit(main())

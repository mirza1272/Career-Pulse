"""Command-line interface for Radar (Project 1)."""

from __future__ import annotations

import argparse
import sys
from sqlalchemy import func, select

from radar import config
from radar.db import get_session, get_supabase_client, init_db
from radar.expiry import run_expiry_check
from radar.extractor import extract_job_from_html
from radar.models import Application, Job
from radar.pipeline import run_pipeline
from radar.scraper import Scraper


def cmd_scan(args: argparse.Namespace) -> None:
    init_db()
    print(
        f"📡 Scanning for jobs: query='{args.query or 'All configured targets'}' "
        f"| level='{args.experience}' | platform='{args.platform}' (limit={args.limit})..."
    )
    stats = run_pipeline(
        query=args.query,
        experience_level=args.experience,
        platform=args.platform,
        limit=args.limit,
        push_email_jobs=not args.no_push,
    )
    print("\n--- Ingestion Summary ---")
    print(f"Discovered:             {stats.total_found}")
    print(f"Valid Apply Windows:    {stats.valid_count}")
    print(f"Duplicates Skipped:     {stats.duplicates_skipped}")
    print(f"Stored in Shared DB:    {stats.stored_jobs}")
    print(f"Pushed to Career Pulse: {stats.pushed_to_career_pulse}")


def cmd_expire(args: argparse.Namespace) -> None:
    init_db()
    print("⏰ Checking freshness & expiry rules (24h post-deadline / 48h max)...")
    expired = run_expiry_check()
    print(f"✅ Expiry run complete: {expired} jobs marked as 'expired'.")


def cmd_stats(args: argparse.Namespace) -> None:
    init_db()
    supabase = get_supabase_client()
    is_cloud, sb_msg = supabase.check_connection()

    with get_session() as s:
        total_jobs = s.scalar(select(func.count(Job.id))) or 0
        active_jobs = s.scalar(select(func.count(Job.id)).where(Job.status == "active")) or 0
        expired_jobs = s.scalar(select(func.count(Job.id)).where(Job.status == "expired")) or 0
        email_jobs = s.scalar(select(func.count(Job.id)).where(Job.has_email.is_(True))) or 0
        link_jobs = total_jobs - email_jobs
        total_apps = s.scalar(select(func.count(Application.id))) or 0

    print("=" * 45)
    print("       RADAR (PROJECT 1) OVERVIEW")
    print("=" * 45)
    print(f"Shared Database:       {config.database_url()}")
    print(f"Cloud Status (Supabase): {'🟢 Online' if is_cloud else '🟡 ' + sb_msg}")
    print(f"Career Pulse API:      {config.CAREERPULSE_API_URL}")
    print("-" * 45)
    print(f"Total Discovered Jobs: {total_jobs}")
    print(f"  • Active:            {active_jobs}")
    print(f"  • Expired:           {expired_jobs}")
    print(f"  • With Apply Email:  {email_jobs} (pushed to queue)")
    print(f"  • Portal / Link Only:{link_jobs} (manual apply)")
    print(f"Career Pulse Queue:    {total_apps} applications")
    print("=" * 45)


def cmd_test_url(args: argparse.Namespace) -> None:
    scraper = Scraper()
    print(f"🔍 Fetching {args.url}...")
    final_url, html, status = scraper.fetch(args.url)
    if not html:
        print(f"❌ Failed to fetch page (status={status})")
        sys.exit(1)
    extracted = extract_job_from_html(html, final_url)
    if not extracted:
        print("❌ Could not extract job details from page HTML")
        sys.exit(1)
    print("\n--- Extracted Job Data ---")
    print(f"Title:       {extracted.title}")
    print(f"Company:     {extracted.company}")
    print(f"Location:    {extracted.location}")
    print(f"Apply Link:  {extracted.link}")
    print(f"Has Email:   {extracted.has_email} ({extracted.email or 'None'})")
    print(f"Deadline:    {extracted.deadline.isoformat() if extracted.deadline else 'Not specified'}")
    print(f"Source:      {extracted.source}")
    print(f"Description: {extracted.jd_text[:180]}...")


def main() -> None:
    parser = argparse.ArgumentParser(description="Radar (Project 1) CLI")
    subparsers = parser.add_subparsers(dest="command", required=True)

    # Scan command
    p_scan = subparsers.add_parser("scan", help="Discover and ingest jobs")
    p_scan.add_argument("--query", "-q", type=str, default=None, help="Custom job title or search query")
    p_scan.add_argument(
        "--experience",
        "-e",
        choices=["junior", "mid", "senior", "any"],
        default="junior",
        help="Target experience level (default: junior)",
    )
    p_scan.add_argument(
        "--platform",
        "-p",
        choices=["all", "google_jobs", "arbeitnow", "jobicy", "remotive"],
        default="all",
        help="Source platform (default: all)",
    )
    p_scan.add_argument("--limit", "-l", type=int, default=10, help="Max jobs to fetch per target")
    p_scan.add_argument("--no-push", action="store_true", help="Do not push email jobs to Career Pulse")
    p_scan.set_defaults(func=cmd_scan)

    # Expire command
    p_expire = subparsers.add_parser("expire", help="Enforce 24h post-deadline and 48h maximum freshness")
    p_expire.set_defaults(func=cmd_expire)

    # Stats command
    p_stats = subparsers.add_parser("stats", help="Show database counts and cloud connection status")
    p_stats.set_defaults(func=cmd_stats)

    # Test URL command
    p_test = subparsers.add_parser("test-url", help="Test scraper & extractor on a specific URL")
    p_test.add_argument("url", type=str, help="Job posting URL")
    p_test.set_defaults(func=cmd_test_url)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()

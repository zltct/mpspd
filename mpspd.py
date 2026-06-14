from __future__ import annotations

import argparse
import asyncio
import html
import json
import mimetypes
import os
import re
import sys
import time
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from http.client import RemoteDisconnected
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


DEFAULT_BASE_URL = "https://images.meupatrocinio.com"
DEFAULT_CONCURRENCY = 50
DEFAULT_INCREMENT = -1
DEFAULT_MAX_RUNTIME_SECONDS = 30 * 60
DEFAULT_CHECKPOINT_INTERVAL = 100
DEFAULT_TIMEOUT_SECONDS = 12
DEFAULT_REQUEST_DELAY_SECONDS = 0.0
DEFAULT_LOG_INTERVAL = 5000
REQUEST_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/126.0 Safari/537.36"
    ),
    "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
}
STATE_FILE = "state.json"
FOUND_FILE = "found_links.jsonl"
MANUAL_FILE = "manual_links.txt"
INDEX_FILE = "index.html"
STOP_FILE = "STOP"
VERSION = "2.0.0"


@dataclass(frozen=True)
class SeedUrl:
    base_url: str
    profile_id: int
    photo_id: int
    photo_number: int

    @property
    def url(self) -> str:
        return build_image_url(
            self.base_url,
            self.profile_id,
            self.photo_id,
            self.photo_number,
        )


@dataclass
class ScanState:
    base_url: str
    profile_id: int
    next_photo_id: int
    next_photo_number: int
    increment: int
    scanned: int = 0
    found: int = 0
    consecutive_errors: int = 0
    last_status: str = "initialized"
    last_probe_status: int | None = None
    last_error: str | None = None
    last_url: str | None = None
    last_found_url: str | None = None
    updated_at: str | None = None


@dataclass(frozen=True)
class Candidate:
    base_url: str
    profile_id: int
    photo_id: int
    photo_number: int

    @property
    def url(self) -> str:
        return build_image_url(
            self.base_url,
            self.profile_id,
            self.photo_id,
            self.photo_number,
        )


@dataclass(frozen=True)
class FoundRecord:
    url: str
    profile_id: int
    photo_id: int
    photo_number: int
    content_type: str | None
    content_length: int | None
    status: int
    discovered_at: str


@dataclass(frozen=True)
class ProbeResult:
    candidate: Candidate
    status: int | None
    found: bool
    content_type: str | None = None
    content_length: int | None = None
    error: str | None = None


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_image_url(
    base_url: str,
    profile_id: int,
    photo_id: int,
    photo_number: int,
) -> str:
    return f"{base_url.rstrip('/')}/{profile_id}/{photo_id}/{photo_number}/"


def parse_seed_url(url: str) -> SeedUrl:
    parsed = urlparse(url)
    parts = [part for part in parsed.path.split("/") if part]
    if not parsed.scheme or not parsed.netloc or len(parts) < 3:
        raise ValueError(
            "Expected an image URL like "
            "https://images.meupatrocinio.com/<profile_id>/<photo_id>/<photo_number>/"
        )

    try:
        profile_id = int(parts[0])
        photo_id = int(parts[1])
        photo_number = int(parts[2])
    except ValueError as exc:
        raise ValueError("Seed URL profile_id, photo_id, and photo_number must be integers") from exc

    return SeedUrl(
        base_url=f"{parsed.scheme}://{parsed.netloc}",
        profile_id=profile_id,
        photo_id=photo_id,
        photo_number=photo_number,
    )


def candidate_from_state(state: ScanState) -> Candidate:
    return Candidate(
        base_url=state.base_url,
        profile_id=state.profile_id,
        photo_id=state.next_photo_id,
        photo_number=state.next_photo_number,
    )


def advance_state(state: ScanState, count: int = 1) -> None:
    state.next_photo_id += state.increment * count


def advance_after_found(state: ScanState, candidate: Candidate) -> None:
    state.next_photo_id = candidate.photo_id + state.increment
    state.next_photo_number = candidate.photo_number + state.increment


def load_state(path: Path) -> ScanState | None:
    if not path.exists():
        return None

    data = json.loads(path.read_text(encoding="utf-8"))
    return ScanState(**data)


def save_state(path: Path, state: ScanState) -> None:
    state.updated_at = utc_now()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(asdict(state), indent=2, sort_keys=True) + "\n", encoding="utf-8")


def load_found_records(path: Path) -> list[FoundRecord]:
    if not path.exists():
        return []

    records: list[FoundRecord] = []
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            records.append(FoundRecord(**json.loads(line)))
    return records


def load_manual_records(path: Path) -> list[FoundRecord]:
    if not path.exists():
        return []

    records: list[FoundRecord] = []
    for line in path.read_text(encoding="utf-8").splitlines():
        url = line.strip()
        if not url or url.startswith("#"):
            continue

        seed = parse_seed_url(url)
        records.append(
            FoundRecord(
                url=seed.url,
                profile_id=seed.profile_id,
                photo_id=seed.photo_id,
                photo_number=seed.photo_number,
                content_type=None,
                content_length=None,
                status=0,
                discovered_at="manual",
            )
        )
    return records


def append_found_record(path: Path, record: FoundRecord) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(asdict(record), sort_keys=True) + "\n")


def found_key(record: FoundRecord) -> tuple[int, int, int]:
    return (record.profile_id, record.photo_id, record.photo_number)


def anchor_key(record: FoundRecord) -> tuple[int, int]:
    return (record.profile_id, record.photo_number)


def reconcile_known_anchors(state: ScanState, records: list[FoundRecord]) -> int:
    anchors = {anchor_key(record): record for record in records}
    skipped = 0

    while True:
        record = anchors.get((state.profile_id, state.next_photo_number))
        if record is None:
            return skipped

        advance_after_found(
            state,
            Candidate(
                base_url=state.base_url,
                profile_id=record.profile_id,
                photo_id=record.photo_id,
                photo_number=record.photo_number,
            ),
        )
        state.last_found_url = record.url
        state.last_probe_status = record.status or None
        state.last_error = None
        state.last_status = "known_anchor"
        skipped += 1


def import_local_images(output_dir: Path, base_url: str | None = None) -> list[FoundRecord]:
    records: list[FoundRecord] = []
    image_pattern = re.compile(r"^(\d+)_(\d+)_(\d+)\.(jpe?g|png|gif|webp)$", re.IGNORECASE)

    for image_path in sorted(Path.cwd().iterdir()):
        if not image_path.is_file():
            continue
        match = image_pattern.match(image_path.name)
        if not match:
            continue

        photo_number = int(match.group(1))
        photo_id = int(match.group(2))
        profile_id = int(match.group(3))
        resolved_base_url = base_url or DEFAULT_BASE_URL
        content_type = mimetypes.guess_type(image_path.name)[0]
        records.append(
            FoundRecord(
                url=build_image_url(resolved_base_url, profile_id, photo_id, photo_number),
                profile_id=profile_id,
                photo_id=photo_id,
                photo_number=photo_number,
                content_type=content_type,
                content_length=image_path.stat().st_size,
                status=200,
                discovered_at=utc_now(),
            )
        )

    if not records:
        return []

    found_path = output_dir / FOUND_FILE
    existing = {found_key(record) for record in load_found_records(found_path)}
    for record in records:
        if found_key(record) not in existing:
            append_found_record(found_path, record)
            existing.add(found_key(record))

    return records


def seed_state_from_local_images(output_dir: Path, increment: int, base_url: str | None = None) -> ScanState | None:
    records = import_local_images(output_dir, base_url=base_url)
    if not records:
        return None

    if increment >= 0:
        latest = max(records, key=lambda item: (item.photo_number, item.photo_id))
    else:
        latest = min(records, key=lambda item: (item.photo_number, item.photo_id))

    return ScanState(
        base_url=base_url or DEFAULT_BASE_URL,
        profile_id=latest.profile_id,
        next_photo_id=latest.photo_id + increment,
        next_photo_number=latest.photo_number + increment,
        increment=increment,
        scanned=0,
        found=len(records),
        last_status="seeded_from_local_images",
        last_url=latest.url,
        last_found_url=latest.url,
    )


def is_image_response(status: int, content_type: str | None) -> bool:
    if not (200 <= status < 300):
        return False
    if not content_type:
        return True
    return content_type.lower().split(";")[0].strip().startswith("image/")


def content_length_from_header(value: str | None) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def probe_error_label(exc: BaseException) -> str:
    if isinstance(exc, TimeoutError):
        return "timeout"
    if isinstance(exc, ConnectionResetError):
        return "conn_reset"
    if isinstance(exc, RemoteDisconnected):
        return "remote_disconnected"
    if isinstance(exc, URLError):
        reason = exc.reason
        if isinstance(reason, ConnectionResetError):
            return "conn_reset"
        if isinstance(reason, TimeoutError):
            return "timeout"
        return "url_error"
    return exc.__class__.__name__


def render_index(
    path: Path,
    state: ScanState,
    records: list[FoundRecord],
    stop_enabled: bool = False,
) -> None:
    manual_records = load_manual_records(path.parent / MANUAL_FILE)
    display_records_by_key = {found_key(record): record for record in manual_records}
    display_records_by_key.update({found_key(record): record for record in records})
    display_records = list(display_records_by_key.values())

    rows = []
    for record in sorted(display_records, key=lambda item: (item.photo_number, item.photo_id)):
        rows.append(
            "<tr>"
            f"<td>{record.photo_number}</td>"
            f"<td>{record.photo_id}</td>"
            f"<td>{record.profile_id}</td>"
            f"<td><a href=\"{html.escape(record.url, quote=True)}\">{html.escape(record.url)}</a></td>"
            f"<td>{html.escape(record.content_type or '')}</td>"
            f"<td>{record.content_length or ''}</td>"
            f"<td>{html.escape(record.discovered_at)}</td>"
            "</tr>"
        )

    content = f"""<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>MPSPD found links</title>
</head>
<body>
  <h1>MPSPD found links</h1>
  <p>Updated: {html.escape(state.updated_at or utc_now())}</p>
  <p>Status: {html.escape(state.last_status)}; scanned: {state.scanned}; found: {len(records)}; manual: {len(manual_records)}; displayed: {len(display_records)}; next photo id: {state.next_photo_id}; next photo number: {state.next_photo_number}; stop flag: {"on" if stop_enabled else "off"}</p>
  <p>Raw files: <a href="./{FOUND_FILE}">{FOUND_FILE}</a> <a href="./{MANUAL_FILE}">{MANUAL_FILE}</a> <a href="./{STATE_FILE}">{STATE_FILE}</a></p>
  <table border="1" cellpadding="4" cellspacing="0">
    <thead>
      <tr>
        <th>Photo #</th>
        <th>Photo ID</th>
        <th>Profile ID</th>
        <th>URL</th>
        <th>Type</th>
        <th>Length</th>
        <th>Discovered</th>
      </tr>
    </thead>
    <tbody>
      {''.join(rows)}
    </tbody>
  </table>
</body>
</html>
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


async def probe_url(
    session: Any,
    candidate: Candidate,
    timeout_seconds: float,
    retries: int,
) -> ProbeResult:
    for attempt in range(retries + 1):
        try:
            result = await asyncio.to_thread(probe_once, candidate, timeout_seconds)
            if result.status in {429, 500, 502, 503, 504} and attempt < retries:
                await asyncio.sleep(min(10.0, 0.5 * (2**attempt)))
                continue
            return result
        except Exception as exc:  # noqa: BLE001 - record transient network failures.
            if attempt >= retries:
                return ProbeResult(candidate=candidate, status=None, found=False, error=probe_error_label(exc))
            await asyncio.sleep(min(10.0, 0.5 * (2**attempt)))

    return ProbeResult(candidate=candidate, status=None, found=False, error="unknown probe failure")


def probe_once(candidate: Candidate, timeout_seconds: float) -> ProbeResult:
    head_result = probe_request(candidate, method="HEAD", timeout_seconds=timeout_seconds)
    if head_result.status not in {403, 405}:
        return head_result
    return probe_request(candidate, method="GET", timeout_seconds=timeout_seconds)


def probe_request(candidate: Candidate, method: str, timeout_seconds: float) -> ProbeResult:
    try:
        request = Request(candidate.url, method=method, headers=REQUEST_HEADERS)
        with urlopen(request, timeout=timeout_seconds) as response:
            if method == "GET":
                response.read(1)
            status = response.status
            content_type = response.headers.get("content-type")
            content_length = content_length_from_header(response.headers.get("content-length"))
            return ProbeResult(
                candidate=candidate,
                status=status,
                found=is_image_response(status, content_type),
                content_type=content_type,
                content_length=content_length,
            )
    except HTTPError as exc:
        content_type = exc.headers.get("content-type")
        content_length = content_length_from_header(exc.headers.get("content-length"))
        return ProbeResult(
            candidate=candidate,
            status=exc.code,
            found=is_image_response(exc.code, content_type),
            content_type=content_type,
            content_length=content_length,
        )
    except URLError as exc:
        return ProbeResult(candidate=candidate, status=None, found=False, error=probe_error_label(exc))


async def run_scan(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    found_path = output_dir / FOUND_FILE
    state_path = output_dir / STATE_FILE
    index_path = output_dir / INDEX_FILE
    stop_path = output_dir / STOP_FILE

    state = load_state(state_path)
    if state is None and args.seed_url:
        seed = parse_seed_url(args.seed_url)
        state = ScanState(
            base_url=seed.base_url,
            profile_id=seed.profile_id,
            next_photo_id=seed.photo_id,
            next_photo_number=seed.photo_number,
            increment=args.increment,
        )
    elif state is None:
        state = seed_state_from_local_images(output_dir, increment=args.increment)

    if state is None:
        raise SystemExit("No state exists and no seed URL/local image fixtures were found.")

    if args.seed_url and args.reset:
        seed = parse_seed_url(args.seed_url)
        state = ScanState(
            base_url=seed.base_url,
            profile_id=seed.profile_id,
            next_photo_id=seed.photo_id,
            next_photo_number=seed.photo_number,
            increment=args.increment,
        )

    if args.import_local:
        import_local_images(output_dir, base_url=state.base_url)

    records = load_found_records(found_path)
    manual_records = load_manual_records(output_dir / MANUAL_FILE)
    reconcile_known_anchors(state, records + manual_records)
    state.found = len(records)
    seen = {found_key(record) for record in records}
    start_time = time.monotonic()
    total_candidates = 0
    checkpoint_counter = 0
    next_log_at = max(1, args.log_interval)
    status_counts: Counter[str] = Counter()
    delay_seconds = max(0.0, args.request_delay)
    print(
        "Starting scan: "
        f"profile={state.profile_id} next={state.next_photo_id}/{state.next_photo_number} "
        f"increment={state.increment} concurrency={args.concurrency} "
        f"runtime={args.max_runtime_seconds}s found={len(records)}",
        flush=True,
    )

    session = None
    while True:
            elapsed = time.monotonic() - start_time
            if elapsed >= args.max_runtime_seconds:
                state.last_status = "runtime_limit_reached"
                break
            if args.max_candidates and total_candidates >= args.max_candidates:
                state.last_status = "candidate_limit_reached"
                break
            if stop_path.exists():
                state.last_status = "stop_flag_present"
                break
            if state.increment < 0 and state.next_photo_id <= 0:
                state.next_photo_id = 0
                state.last_status = "photo_id_floor_reached"
                break

            if args.max_candidates:
                batch_size = min(args.concurrency, args.max_candidates - total_candidates)
            else:
                batch_size = args.concurrency
            if batch_size <= 0:
                state.last_status = "candidate_limit_reached"
                break

            candidates = []
            for _ in range(batch_size):
                candidate = candidate_from_state(state)
                candidates.append(candidate)
                advance_state(state)
                total_candidates += 1

            tasks = [
                asyncio.create_task(
                    probe_url(
                        session,
                        candidate,
                        timeout_seconds=args.timeout,
                        retries=args.retries,
                    )
                )
                for candidate in candidates
            ]
            results = []
            found_in_batch = False
            for task in asyncio.as_completed(tasks):
                result = await task
                results.append(result)
                if result.found:
                    found_in_batch = True
                    for pending_task in tasks:
                        if not pending_task.done():
                            pending_task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    break

            for result in results:
                state.scanned += 1
                state.last_url = result.candidate.url
                state.last_probe_status = result.status
                state.last_error = result.error

                if result.found:
                    key = (
                        result.candidate.profile_id,
                        result.candidate.photo_id,
                        result.candidate.photo_number,
                    )
                    if key not in seen:
                        record = FoundRecord(
                            url=result.candidate.url,
                            profile_id=result.candidate.profile_id,
                            photo_id=result.candidate.photo_id,
                            photo_number=result.candidate.photo_number,
                            content_type=result.content_type,
                            content_length=result.content_length,
                            status=result.status or 200,
                            discovered_at=utc_now(),
                        )
                        append_found_record(found_path, record)
                        records.append(record)
                        seen.add(key)
                        state.found = len(records)

                    advance_after_found(state, result.candidate)
                    state.last_found_url = result.candidate.url
                    state.consecutive_errors = 0
                    state.last_error = None
                    state.last_status = "found"
                    print(
                        "FOUND "
                        f"photo_number={result.candidate.photo_number} "
                        f"photo_id={result.candidate.photo_id} "
                        f"status={result.status} "
                        f"next={state.next_photo_id}/{state.next_photo_number} "
                        f"url={result.candidate.url}",
                        flush=True,
                    )
                elif result.status in {429, 500, 502, 503, 504} or result.error:
                    state.consecutive_errors += 1
                    state.last_status = f"transient_error:{result.status or result.error}"
                else:
                    state.last_status = f"miss:{result.status}"

                status_counts[str(result.status or result.error or "unknown")] += 1

            if found_in_batch:
                state.last_status = "found"
                reconcile_known_anchors(state, records + manual_records)

            if state.consecutive_errors >= args.backoff_after:
                delay_seconds = min(args.max_backoff, max(1.0, delay_seconds * 2 or 1.0))
                state.consecutive_errors = 0
            elif delay_seconds > args.request_delay:
                delay_seconds = max(args.request_delay, delay_seconds / 2)

            checkpoint_counter += len(results)
            if checkpoint_counter >= args.checkpoint_interval:
                save_state(state_path, state)
                render_index(index_path, state, records, stop_enabled=stop_path.exists())
                checkpoint_counter = 0

            if state.scanned >= next_log_at:
                elapsed = max(0.001, time.monotonic() - start_time)
                common_statuses = ", ".join(
                    f"{status}:{count}" for status, count in status_counts.most_common(6)
                )
                print(
                    "PROGRESS "
                    f"scanned={state.scanned} found={len(records)} "
                    f"next={state.next_photo_id}/{state.next_photo_number} "
                    f"last={state.last_url} rate={state.scanned / elapsed:.1f}/s "
                    f"statuses=[{common_statuses}]",
                    flush=True,
                )
                next_log_at = state.scanned + max(1, args.log_interval)

            if delay_seconds:
                await asyncio.sleep(delay_seconds)

    state.found = len(records)
    save_state(state_path, state)
    render_index(index_path, state, records, stop_enabled=stop_path.exists())
    print(
        f"Done: {state.last_status}; scanned={state.scanned}; "
        f"found={len(records)}; next={state.next_photo_id}/{state.next_photo_number}"
    )
    return 0


def run_init(args: argparse.Namespace) -> int:
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / STATE_FILE
    index_path = output_dir / INDEX_FILE

    if args.seed_url:
        seed = parse_seed_url(args.seed_url)
        state = ScanState(
            base_url=seed.base_url,
            profile_id=seed.profile_id,
            next_photo_id=seed.photo_id,
            next_photo_number=seed.photo_number,
            increment=args.increment,
        )
    else:
        state = seed_state_from_local_images(output_dir, increment=args.increment)
        if state is None:
            raise SystemExit("No seed URL provided and no local image fixtures were found.")

    save_state(state_path, state)
    records = load_found_records(output_dir / FOUND_FILE)
    render_index(index_path, state, records, stop_enabled=(output_dir / STOP_FILE).exists())
    print(f"Initialized {state_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Resumable MPSPD image-link scanner.")
    parser.add_argument("--version", action="version", version=f"mpspd {VERSION}")
    subparsers = parser.add_subparsers(dest="command")

    init_parser = subparsers.add_parser("init", help="Create initial state/output files.")
    init_parser.add_argument("--seed-url")
    init_parser.add_argument("--increment", type=int, default=DEFAULT_INCREMENT)
    init_parser.add_argument("--output-dir", default="public")
    init_parser.set_defaults(func=run_init)

    scan_parser = subparsers.add_parser("scan", help="Run a bounded resumable scan.")
    scan_parser.add_argument("--seed-url")
    scan_parser.add_argument("--increment", type=int, default=DEFAULT_INCREMENT)
    scan_parser.add_argument("--output-dir", default="public")
    scan_parser.add_argument("--concurrency", type=int, default=DEFAULT_CONCURRENCY)
    scan_parser.add_argument("--max-runtime-seconds", type=int, default=DEFAULT_MAX_RUNTIME_SECONDS)
    scan_parser.add_argument("--max-candidates", type=int, default=0)
    scan_parser.add_argument("--checkpoint-interval", type=int, default=DEFAULT_CHECKPOINT_INTERVAL)
    scan_parser.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_SECONDS)
    scan_parser.add_argument("--retries", type=int, default=2)
    scan_parser.add_argument("--backoff-after", type=int, default=20)
    scan_parser.add_argument("--max-backoff", type=float, default=30.0)
    scan_parser.add_argument("--request-delay", type=float, default=DEFAULT_REQUEST_DELAY_SECONDS)
    scan_parser.add_argument("--log-interval", type=int, default=DEFAULT_LOG_INTERVAL)
    scan_parser.add_argument("--reset", action="store_true")
    scan_parser.add_argument("--import-local", action="store_true")
    scan_parser.set_defaults(func=lambda args: asyncio.run(run_scan(args)))

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 2
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

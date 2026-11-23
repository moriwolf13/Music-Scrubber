"""Explicit ONLINE public-metadata probe; NOT unittest/discovery or audio acceptance.

Usage: .venv/bin/python -I -B tests/provider_metadata_validation.py --online

Copies allowlisted source into a disposable /tmp directory and exercises the
installed lookup/client/parser against MusicBrainz and CAA JSON metadata only.
No AcoustID, audio, application key, music files, ingest, approval, or state reads.
Only /tmp results are written; the caller may retain results.json as evidence.
Network is never enabled merely by importing this module or test discovery.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import urlsplit


SOURCE_FILES = ("lookup.py", "tag.py", "constants.py", "mixie_terminal.py")
MAX_HTTP_CALLS = 40
MAX_SECONDS = 180
# Public sample MBIDs obtained from recorded MusicBrainz search responses.
# Pin samples to avoid repeatedly searching an overloaded service. These are
# metadata samples only, NOT claims about which editions the user's audio owns.
CASES = (
    ("Halestorm / Back From the Dead", "9c4c26ea-6976-4032-beba-cba1684119f8"),
    ("Halestorm / The Strange Case of...", "8c4e64dc-a14d-4958-9baa-8ece77db09d9"),
    ("In This Moment / Witch", "b1bd9c0d-4ca7-406e-a43e-44a2126da796"),
    ("The Offspring / Americana (ESCA 7393)", "c716d82d-6c6e-390c-813b-624728d67991"),
    ("Halestorm & I Prevail / can u see me in the dark?", "e5a354da-c9ef-4981-a6a5-776f16aff600"),
)


def online_child(root):
    root = Path(root).resolve()
    if root.parent != Path("/tmp") or Path(__file__).resolve().parent != root:
        raise RuntimeError("Provider probe must run from its independently verified /tmp source copy.")
    started, requests = time.monotonic(), []
    report = {"kind": "REAL_PUBLIC_PROVIDER_METADATA_ONLY_NOT_AUDIO_ACCEPTANCE",
              "started_utc": datetime.now(timezone.utc).isoformat(),
              "limits": {"http_calls": MAX_HTTP_CALLS, "seconds": MAX_SECONDS,
                         "timeout_seconds": 10, "attempts_per_operation": 1},
              "providers": ["https://musicbrainz.org", "https://coverartarchive.org"],
              "source_sha256": {name: hashlib.sha256((root / name).read_bytes()).hexdigest()
                                for name in SOURCE_FILES},
              "requests": requests, "blocked_requests": [], "operations": [], "cases": [], "errors": []}

    def guard(event, args):
        if event == "open":
            path, mode, flags = args
            if isinstance(path, (str, bytes, os.PathLike)):
                path = Path(os.fsdecode(path)).resolve()
                dependency = str(path).startswith(str(Path(sys.prefix).resolve()) + "/")
                if not dependency and (path == Path("/home/deck/MIXIE-Lab")
                                       or Path("/home/deck/MIXIE-Lab") in path.parents):
                    raise PermissionError("Provider probe cannot read live workspace/music/state.")
                writing = ((isinstance(mode, str) and any(c in mode for c in "wax+"))
                           or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC)))
                if writing and path != root and root not in path.parents:
                    raise PermissionError("Provider probe writes only to its /tmp directory.")
        elif event == "urllib.Request":
            url, data, headers, method = args
            parsed = urlsplit(url)
            if (parsed.scheme != "https" or parsed.hostname not in ("musicbrainz.org", "coverartarchive.org")
                    or method != "GET" or data is not None):
                report["blocked_requests"].append({"url": url, "method": method,
                    "reason": "Provider/redirect outside the strict MusicBrainz/CAA metadata allowlist"})
                raise PermissionError("Only public read-only MusicBrainz/CAA metadata GETs are allowed; redirect was not followed.")
            if len(requests) >= MAX_HTTP_CALLS or time.monotonic() - started >= MAX_SECONDS:
                raise RuntimeError("Public metadata validation budget exhausted.")
            requests.append({"number": len(requests) + 1, "url": url, "method": method,
                             "started_seconds": round(time.monotonic() - started, 3)})

    sys.addaudithook(guard)
    sys.path.insert(0, str(root))
    import lookup
    lookup.musicbrainzngs.set_hostname("musicbrainz.org", use_https=True)
    lookup.musicbrainzngs.set_useragent("MIXIE-public-metadata-validation", "1.0")
    lookup.MUSICBRAINZ_TIMEOUT = 10
    os.environ["MIXIE_MB_INTERVAL_SECONDS"] = "1"
    os.environ["MIXIE_MB_MAX_ATTEMPTS"] = "1"

    def observe(name, function, *args, **kwargs):
        before = len(requests)
        operation = {"name": name, "args": args, "options": kwargs}
        report["operations"].append(operation)
        try:
            value = function(*args, **kwargs)
            operation["outcome"] = "success"
            return value
        except Exception as error:
            failure = lookup.provider_error_evidence("release_tracklist", error)
            operation.update(outcome="error", error=failure)
            raise
        finally:
            operation["http_request_numbers"] = list(range(before + 1, len(requests) + 1))

    def attempt(case, key, function, *args):
        try:
            value = observe("lookup." + function.__name__, function, *args)
            case[key] = value
            return value
        except Exception as error:
            case["errors"].append({"operation": key, **lookup.provider_error_evidence("release_tracklist", error)})
            return None

    try:
        with lookup.musicbrainz_album() as counters:
            for label, release_id in CASES:
                case = {"label": label, "sample_release_id": release_id,
                        "outcome": "unavailable", "errors": []}
                report["cases"].append(case)
                # Independent metadata providers remain observable if one
                # endpoint fails; failures never become negative memberships.
                tracks = attempt(case, "tracklist", lookup.get_release_tracklist, release_id)
                if tracks is not None:
                    case["tracklist_complete"] = tracks.complete
                attempt(case, "release_metadata", lookup.get_release_metadata, release_id)
                chosen = next((row for row in tracks or () if row.get("recording_disambiguation")),
                              tracks[0] if tracks else None)
                if chosen:
                    rid = chosen["recording_id"]
                    case["sample_recording_id"] = rid
                    details = attempt(case, "recording_details", lookup.get_recording_details, rid)
                    if details is not None:
                        case["positive_membership_present"] = release_id in {
                            row["id"] for row in details.get("release-list", [])}
                        case["recording_membership_complete"] = details.get("release-list-complete")
                attempt(case, "caa_front_metadata", lookup.get_release_front_art, release_id)
                if tracks is not None or "release_metadata" in case:
                    case["outcome"] = "partial_metadata" if case["errors"] else "metadata_observed"
            report["musicbrainz_counters"] = dict(counters)
    except Exception as error:
        report["errors"].append({"type": type(error).__name__, "message": str(error)})
    finally:
        # Function objects in search operation args are diagnostic names only.
        for operation in report["operations"]:
            operation["args"] = [getattr(arg, "__name__", arg) for arg in operation["args"]]
        report["http_calls"] = len(requests)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (root / "results.json").write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print("PUBLIC METADATA RESULTS:", root / "results.json", flush=True)
        print(json.dumps({"http_calls": len(requests), "elapsed_seconds": report["elapsed_seconds"],
                          "cases": [{key: case.get(key) for key in (
                              "label", "outcome", "sample_release_id", "sample_recording_id", "tracklist_complete",
                              "positive_membership_present", "recording_membership_complete", "errors")}
                                    for case in report["cases"]]}, indent=2, ensure_ascii=False), flush=True)
    complete = (len(report['cases']) == len(CASES) and not report['errors']
                and not report['blocked_requests'] and all(
                    case.get('tracklist_complete') and case.get('positive_membership_present')
                    and case.get('recording_membership_complete') is True
                    and 'release_metadata' in case and 'caa_front_metadata' in case
                    and not case['errors'] for case in report['cases']))
    return 0 if complete else 1


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--online", action="store_true", help="Explicit consent to bounded public metadata GETs")
    parser.add_argument("--child", help=argparse.SUPPRESS)
    options = parser.parse_args()
    if not options.online:
        parser.error("No network by default; specify --online for public metadata only.")
    if options.child:
        return online_child(options.child)
    source = Path(__file__).resolve().parents[1]
    root = Path(tempfile.mkdtemp(prefix="mixie-public-metadata-", dir="/tmp")).resolve()
    for name in SOURCE_FILES:
        path = source / name
        if path.is_symlink() or not path.is_file():
            raise RuntimeError(f"Allowlisted source must be a regular file: {name}")
        shutil.copyfile(path, root / name)
    shutil.copyfile(Path(__file__).resolve(), root / "provider_metadata_validation.py")
    (root / "home").mkdir()
    print("Disposable public-metadata workspace:", root, flush=True)
    try:
        child = subprocess.run([sys.executable, "-I", "-B", str(root / "provider_metadata_validation.py"),
                                "--online", "--child", str(root)], cwd=root,
                               env={"PATH": os.defpath, "HOME": str(root / "home"),
                                    "TMPDIR": str(root), "PYTHONDONTWRITEBYTECODE": "1"},
                               timeout=MAX_SECONDS + 30)
        return child.returncode
    except subprocess.TimeoutExpired:
        print("Provider validation exceeded its bounded deadline; no audio acceptance was attempted.", flush=True)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())

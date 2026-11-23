"""MIXIE terminal presentation; works without TTY features or GUI packages."""
import os
import shutil
import sys
from pathlib import Path
from collections import defaultdict
import re

COLORS = {
    "success": "38;2;78;219;161", "warning": "38;2;255;198;89",
    "error": "38;2;255;102;125", "accent": "38;2;68;232;242",
    "header": "38;2;179;122;255", "muted": "38;2;160;158;184",
    "gold": "38;2;220;177;80",
}


def color(value, tone, *, stream=None):
    text = str(value)
    if stream is None:
        stream = sys.stdout
    if not getattr(stream, "isatty", lambda: False)() or "NO_COLOR" in os.environ or os.environ.get("TERM") == "dumb":
        return text
    return f"\x1b[{COLORS[tone]}m{text}\x1b[0m"


def _safe(value):
    """Prevent untrusted album titles and file names from injecting terminal controls."""
    return "".join(char if 32 <= ord(char) < 127 or ord(char) >= 160 else "?" for char in str(value))


def header(title, subtitle=None):
    print("\n" + color("═" * 40, "header"))
    print(color("  M.I.X.I.E.  ", "accent") + color(_safe(title), "header"))
    if subtitle:
        print(color("  " + _safe(subtitle), "muted"))
    print(color("═" * 40, "header"))


def _row(audit, metadata, index, *, stream=None):
    details = audit.get("details") or {}
    title = details.get("title") or next(iter(metadata.get("title") or ()), Path(audit["path"]).stem)
    artist = details.get("artist-credit-phrase") or next(iter(metadata.get("artist") or ()), "Unknown artist")
    label = f"{index:02}. {_safe(title)} — {_safe(artist)}"
    columns = max(42, min(110, shutil.get_terminal_size((86, 20)).columns))
    width = columns - 21
    if len(label) > width:
        label = label[:max(1, width - 1)] + "…"
    status = audit.get("status")
    if status == "preliminary_match":
        indicator = color("MATCHED", "success", stream=stream)
    elif status in ("invalid_audio", "inspection_error", "audit_error"):
        indicator = color("ERROR", "error", stream=stream)
    else:
        indicator = color("NEEDS REVIEW", "warning", stream=stream)
    return label.ljust(width) + "  " + indicator


def track_summary(audits, tracks, *, tag_service):
    """Stable, readable album results when no live callback was available."""
    info = {Path(row["path"]).resolve(): row for row in tracks}
    ordered = sorted(audits, key=lambda audit: (
        tag_service.parse_disc_number(info.get(Path(audit["path"]).resolve(), {}).get("disc_number", ()))[0] or 1,
        int(tag_service.parse_track_number(info.get(Path(audit["path"]).resolve(), {}).get("track_number", ()))[0] or 9999),
        str(audit["path"])))
    for index, audit in enumerate(ordered, 1):
        print(_row(audit, info.get(Path(audit["path"]).resolve(), {}), index))


def scan_event(phase, index, total, path, result=None, *, metadata=None, stream=None):
    """Real per-track events, not an estimated percentage or timer animation."""
    out = stream or sys.stdout
    tty = getattr(out, "isatty", lambda: False)() and "NO_COLOR" not in os.environ
    if phase == "start":
        if tty:
            message = f"  Scanning track {index}/{total}: {_safe(Path(path).name)}"
            out.write("\r\x1b[2K" + color(message[:100], "accent", stream=out))
            out.flush()
        elif index == 1:
            print(f"  Scanning {total} tracks, one at a time...", file=out, flush=True)
        return
    if tty:
        out.write("\r\x1b[2K")
    print(_row(result, metadata or {}, index, stream=out), file=out, flush=True)
    # Progress is the proportion of actual completed per-track audits.
    if tty:
        columns = max(10, min(28, shutil.get_terminal_size((86, 20)).columns - 25))
        filled = round(columns * index / total)
        bar = "━" * filled + "─" * (columns - filled)
        out.write(color(f"  ┃{bar}┃ {index}/{total}\r", "gold", stream=out))
        if index == total:
            out.write("\x1b[2K\n")
        out.flush()


def pass_status(number, label):
    """Report an actual process boundary, not invented completion progress."""
    _finish_edition_progress()
    print(color(f"  PASS {number}/4 — {_safe(label)}", "header"), flush=True)


def _raw(row, field):
    values = row.get(field) or ()
    if isinstance(values, (tuple, list)):
        return str(values[0]) if len(values) == 1 else " / ".join(map(str, values)) if values else "[missing]"
    return str(values) if values else "[missing]"


def _change_line(filename, field, previous, current, *, status="PROPOSED"):
    # Both old and new values may originate in untrusted audio tags.
    print(f"    {_safe(filename)} [{_safe(field)}] " +
          color(_safe(previous), "error") + " → " + color(_safe(current), "success") +
          "  " + color(status, "success" if status == "VERIFIED" else "warning"), flush=True)


FIELD_LABELS = {
    'album': 'Album', 'album_artist': 'Album Artist', 'title': 'Title',
    'artist': 'Artist', 'date': 'Date', 'genre': 'Genre',
    'track_number': 'Track Number', 'track_total': 'Track Total',
    'disc_number': 'Disc Number', 'disc_total': 'Disc Total',
}


def _metadata_grouped(changes, *, status, artist=None, album=None):
    """Readable per-track summary; never changes the underlying evidence."""
    groups = defaultdict(list)
    for filename, field, old, new in changes:
        groups[str(filename)].append((field, old, new))
    if artist or album:
        print(color('  ' + _safe(artist or 'Unknown artist') + ' | ' +
                    _safe(album or 'Unknown album'), 'header'))
    shown = 0
    # Limit by TRACK, not by individual fields, to avoid truncated track groups.
    max_tracks = len(groups) if os.environ.get('MIXIE_VERBOSE') == '1' else 6
    for filename, fields in groups.items():
        if shown >= max_tracks:
            break
        shown += 1
        label = str(filename)
        prefix = f'{artist} - {album} - ' if artist and album else None
        if prefix and label.casefold().startswith(prefix.casefold()):
            label = label[len(prefix):]
        match = re.match(r'^(\d{1,2})[ ._-]+(.+)$', label)
        if match:
            label = f'{int(match.group(1)):02}. {match.group(2)}'
        print(color('    ' + _safe(label) + ':', 'accent'))
        for field, old, new in fields:
            print('      ' + _safe(FIELD_LABELS.get(field, field.replace('_', ' ').title())) +
                  ': ' + color(_safe(old), 'error') + ' → ' +
                  color(_safe(new), 'success'))
        print(color('      Status: ' + status,
                    'success' if status == 'VERIFIED' else 'warning'))
    if shown < len(groups):
        hidden = sum(len(fields) for fields in list(groups.values())[shown:])
        print(color(f'  ...and {len(groups)-shown} more tracks ({hidden} changes). '
                    'Set MIXIE_VERBOSE=1 for all.', 'muted'))


def show_proposed_changes(tracks, identities, proposals, edits, *, tag_service=None,
                          album=None, artist=None):
    """Preview differences; never claim proposals are already applied."""
    rows = {str(Path(row["path"]).resolve()): row for row in tracks}
    changes = {}
    for proposal in proposals:
        if not isinstance(proposal, dict):
            continue
        key = str(Path(proposal.get("path", "")).resolve())
        field = proposal.get("field")
        if key in rows and field in ("album", "album_artist", "title", "artist", "date", "genre", "track_number", "track_total", "disc_number", "disc_total"):
            changes[(key, field)] = str(proposal.get("value", ""))
    for path, identity in identities.items():
        if path not in rows:
            continue
        for field in ("title", "artist"):
            if isinstance(identity.get(field), str):
                changes[(path, field)] = identity[field]
    for path in rows:
        fields = dict(edits.get("album") or {})
        fields.update(edits.get("tracks", {}).get(path, {}))
        for field in ("album", "album_artist", "artist", "title"):
            if field in fields:
                changes[(path, field)] = str(fields[field])
    actual = [(path, field, _raw(rows[path], field), value)
              for (path, field), value in changes.items()
              if value and _raw(rows[path], field) != value]
    print(color(f"  Proposed metadata changes: {len(actual)}", "accent"))
    _metadata_grouped([(Path(path).name, field, old, new)
                       for path, field, old, new in actual],
                      status='PROPOSED', artist=artist, album=album)


def collect_verified_changes(tracks, copies, *, tag_service):
    """Read the actual working-copy tags after writes and re-open verification."""
    by_path = {Path(row["path"]).resolve(): row for row in tracks}
    changes = []
    for source, target in copies.items():
        before = by_path.get(Path(source).resolve())
        after = tag_service.read_track_metadata(target)
        if before is None or after is None:
            raise RuntimeError("Cannot compare verified working-copy tags.")
        for field in ("album", "album_artist", "title", "artist", "date", "genre", "track_number", "track_total", "disc_number", "disc_total"):
            old, new = _raw(before, field), _raw(after, field)
            if old != new:
                changes.append((Path(source).name, field, old, new))
    return changes


def show_verified_changes(tracks, copies, *, tag_service):
    """Capture a verified working-copy change snapshot for later display."""
    return collect_verified_changes(tracks, copies, tag_service=tag_service)


def show_confirmed_changes(changes, *, artist=None, album=None):
    """Display only after the caller confirms library publication succeeded."""
    print(color(f"  Metadata changes verified: {len(changes)}", "success"))
    _metadata_grouped(changes, status='VERIFIED', artist=artist, album=album)


_edition_progress_active = False


def _finish_edition_progress():
    global _edition_progress_active
    if _edition_progress_active:
        sys.stdout.write('\n')
        sys.stdout.flush()
        _edition_progress_active = False


def album_stage(label, index=None, total=None):
    global _edition_progress_active
    if index is None or total is None:
        _finish_edition_progress()
        print(color("  " + _safe(label), "accent"), flush=True)
        return
    if total <= 0 or index <= 0 or index > total:
        raise ValueError('Invalid edition progress numbers.')
    stream = sys.stdout
    tty = (getattr(stream, 'isatty', lambda: False)()
           and os.environ.get('TERM') != 'dumb')
    if not tty:
        if index in (1, total) or index % 10 == 0:
            print(f"  {_safe(label)} {index}/{total}", flush=True)
        return
    width = max(10, min(28, shutil.get_terminal_size((94, 20)).columns - 45))
    filled = min(width, width * index // total)
    progress = ('  ' + color('┃' + '█' * filled + '░' * (width-filled) + '┃', 'gold')
                + f' {index}/{total}  ' + color(_safe(label), 'muted'))
    stream.write('\r\x1b[2K' + progress)
    stream.flush()
    _edition_progress_active = True
    if index == total:
        _finish_edition_progress()


def final_status(count, reason=None):
    _finish_edition_progress()
    if reason:
        print(color(f"  Album pending: {_safe(reason)}", "warning"))
    else:
        print(color(f"  Album plan verified: {count} tracks", "success"))


# A single presentation entry point keeps the interactive terminal consistent.
# The upcoming GUI can subscribe to operation events rather than scraping print.
import builtins
from contextlib import contextmanager, redirect_stdout
import re
import tempfile


def themed_print(*values, **kwargs):
    """Style headings, menu entries, and status lines; retain plain redirected logs."""
    if len(values) != 1 or kwargs.get("file") is not None:
        return builtins.print(*values, **kwargs)
    value = str(values[0])
    trimmed = value.lstrip("\n ")
    if trimmed.startswith(("===", "══", "---", "—", "M.I.X.I.E.")):
        tone = "header"
    elif re.match(r"(?:[0-9]+|[A-Z])\. ", trimmed) or trimmed.startswith("0. "):
        tone = "accent"
    elif any(text in trimmed.casefold() for text in ("not ready", "needs review", "missing:", "not imported", "warning:", "deferred:", "blocked:")):
        tone = "warning"
    elif any(text in trimmed.casefold() for text in ("successfully", "verified:", "imported and verified", "applied and verified", "saved.", "complete:")):
        tone = "success"
    else:
        return builtins.print(*values, **kwargs)
    return builtins.print(color(value, tone), **kwargs)


def themed_input(message=""):
    return builtins.input(color(message, "accent"))


@contextmanager
def compact_operation(label, *, root):
    """Save verbose operation output to a private log; show honest phase boundaries.

    Does not replace checks, swallow exceptions, or claim success after failure.
    MIXIE_VERBOSE=1 keeps the original full output for diagnostics.
    """
    if os.environ.get("MIXIE_VERBOSE") == "1":
        print(color("  " + _safe(label) + "...", "accent"), flush=True)
        yield
        return
    folder = Path(root).resolve() / ".mixie_logs"
    if folder.is_symlink() or (folder.exists() and not folder.is_dir()):
        raise ValueError("Unsafe operation log directory; no operation was started.")
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if folder.is_symlink():
        raise ValueError("Unsafe operation log directory.")
    descriptor, filename = tempfile.mkstemp(prefix="operation-", suffix=".log", dir=folder)
    completed = False
    print(color("  " + _safe(label) + "...", "accent"), flush=True)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as log:
            with redirect_stdout(log):
                yield
        completed = True
    finally:
        if completed:
            print(color("  " + _safe(label) + " complete", "success"), flush=True)
        else:
            print(color("  " + _safe(label) + " stopped; see diagnostic log", "warning"), flush=True)
        # Some staging helpers report per-item deferrals instead of raising.
        # Never make those failures disappear from the user's concise summary.
        try:
            with open(filename, encoding="utf-8") as recorded:
                noteworthy = sum(line.lstrip().startswith(("Import deferred:", "Unsupported Inbox item:",
                                                            "Skipping symbolic link:", "Cleanup blocked:"))
                                 for line in recorded)
            if noteworthy:
                print(color(f"  Attention: {noteworthy} item(s) deferred or blocked. Recovery/reconciliation may be required; see details", "warning"), flush=True)
        except OSError:
            print(color("  Diagnostic log unavailable; check operation results carefully", "warning"), flush=True)
        print(color("  Details: " + filename, "muted"), flush=True)

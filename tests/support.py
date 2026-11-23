"""Disposable fixture paths and a fail-closed process-wide test safety boundary."""
from contextlib import ExitStack
from io import BytesIO
from pathlib import Path
import os
import subprocess
import sys
import site
import stat
import tempfile
import time
import unittest
from unittest.mock import patch


class FilesystemGuard:
    def __init__(self, root, protected, dependency_roots=(), *, profile=False):
        self.root = os.path.realpath(root)
        self.protected = tuple(os.path.abspath(value) for value in protected)
        self.dependencies = tuple(os.path.realpath(value) for value in dependency_roots)
        self.profile = profile
        self.stats = {'calls': 0, 'checked_paths': 0, 'seconds': 0.0}

    @staticmethod
    def inside(path, parent):
        return path == parent or path.startswith(parent + os.sep)

    @staticmethod
    def descriptor_path(descriptor, *, directory=False):
        try:
            if directory and not stat.S_ISDIR(os.fstat(descriptor).st_mode):
                raise PermissionError('dir_fd is not a directory.')
            path = os.readlink(f'/proc/self/fd/{descriptor}')
        except OSError as error:
            raise PermissionError('Cannot independently verify file descriptor.') from error
        if path.startswith('/'):
            return path
        if directory or not path.startswith(('pipe:[', 'socket:[')):
            raise PermissionError('Unverifiable file descriptor target.')
        return None

    def absolute_path(self, value, dir_fd=None):
        if isinstance(value, int):
            return self.descriptor_path(value)
        if not isinstance(value, (str, bytes, os.PathLike)):
            return None
        value = os.fsdecode(value)
        if os.path.isabs(value):
            return value
        base = (self.descriptor_path(dir_fd, directory=True)
                if dir_fd not in (None, -1) else os.getcwd())
        if base is None:
            raise PermissionError('Unverifiable directory descriptor.')
        # Do not normpath away '..' before realpath: a symlink changes its meaning.
        return os.path.join(base, value)

    def checked_path(self, value, writing=False, dir_fd=None, *, resolved=None):
        if self.profile:
            self.stats['checked_paths'] += 1
        path = self.absolute_path(value, dir_fd)
        if path is None:
            return
        spelling = os.path.abspath(path)
        dependency = not writing and any(self.inside(spelling, p) for p in self.dependencies)
        if not dependency and any(self.inside(spelling, p) for p in self.protected):
            raise PermissionError(f'Test attempted access to live workspace: {spelling}')
        # Only a single syscall's arguments may share resolutions. Never retain
        # them across operations: rename/replacement can turn a safe path into a link.
        canonical = resolved.get(path) if resolved is not None else None
        if canonical is None:
            canonical = os.path.realpath(path)
            if resolved is not None:
                resolved[path] = canonical
        if not writing and any(self.inside(canonical, p) for p in self.dependencies):
            return
        if any(self.inside(canonical, p) for p in self.protected):
            raise PermissionError(f'Test attempted access to live workspace: {canonical}')
        if writing and not self.inside(canonical, self.root):
            raise PermissionError(f'Test attempted write outside disposable workspace: {canonical}')

    @staticmethod
    def writing(mode, flags):
        return ((isinstance(mode, str) and any(c in mode for c in 'wax+'))
                or (isinstance(flags, int) and bool(flags & (
                    os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))))

    def audit(self, event, args):
        if event in ('socket.connect', 'socket.getaddrinfo', 'socket.gethostbyname',
                     'socket.gethostbyaddr', 'socket.sendto', 'socket.sendmsg', 'urllib.Request'):
            raise PermissionError('Network access is forbidden in regression tests.')
        if event == 'open':
            self.checked_path(args[0], self.writing(args[1], args[2]))
        elif event in ('os.remove', 'os.rmdir'):
            self.checked_path(args[0], True, args[1])
        elif event in ('os.mkdir', 'os.chmod'):
            self.checked_path(args[0], True, args[2])
        elif event in ('os.utime', 'os.chown'):
            self.checked_path(args[0], True, args[3])
        elif event == 'os.truncate':
            self.checked_path(args[0], True)
        elif event == 'os.symlink':
            destination = self.absolute_path(args[1], args[2])
            if destination is None:
                raise PermissionError('Unverifiable symlink destination.')
            resolved = {}
            self.checked_path(destination, True, resolved=resolved)
            target = os.path.join(os.path.dirname(destination), os.fsdecode(args[0]))
            self.checked_path(target, True, resolved=resolved)
        elif event in ('os.rename', 'os.link'):
            resolved = {}
            self.checked_path(args[0], True, args[2], resolved=resolved)
            self.checked_path(args[1], True, args[3], resolved=resolved)
        elif event in ('os.listdir', 'os.scandir', 'os.chdir'):
            self.checked_path(args[0] if args[0] is not None else '.')
        elif event == 'os.posix_spawn':
            # Python 3.14's Popen may use posix_spawn for the allowed ffmpeg too.
            self.audit('subprocess.Popen', (args[0], args[1], None, args[2]))
        elif event in ('os.system', 'os.exec'):
            raise PermissionError('Only bounded synthetic ffmpeg fixture generation is permitted.')
        elif event == 'subprocess.Popen':
            executable, command, cwd, environment = args
            allowed = ('ffmpeg', '/usr/bin/ffmpeg', '/bin/ffmpeg')
            if (not isinstance(command, (tuple, list)) or not command
                    or os.fsdecode(executable) not in allowed
                    or os.fsdecode(command[0]) not in allowed
                    or '-nostdin' not in tuple(os.fsdecode(value) for value in command)):
                raise PermissionError('Only bounded synthetic ffmpeg fixture generation is permitted.')
            if os.fsdecode(executable) == 'ffmpeg':
                if environment is None:
                    search_path = os.environ.get('PATH', os.defpath)
                else:
                    search_path = environment.get('PATH')
                    if search_path is None:
                        search_path = environment.get(b'PATH', os.defpath)
                if search_path is None or os.fsdecode(search_path) != os.defpath:
                    raise PermissionError('ffmpeg must use the isolated executable search path.')
            base = os.getcwd() if cwd is None else self.absolute_path(cwd)
            if base is None:
                raise PermissionError('Unverifiable subprocess working directory.')
            self.checked_path(base)
            for index, argument in enumerate(command[1:], 1):
                argument = os.fsdecode(argument)
                if '://' in argument or argument.startswith(('file:', 'concat:', 'pipe:')):
                    raise PermissionError('ffmpeg protocols are forbidden in regression fixtures.')
                if argument.startswith('/'):
                    self.checked_path(argument)
                elif command[index - 1] == '-i' and not argument.startswith(('anullsrc=', 'sine=')):
                    self.checked_path(os.path.join(base, argument))
            self.checked_path(os.path.join(base, os.fsdecode(command[-1])), True)

    def timed_audit(self, event, args):
        # CPython suppresses tracing inside audit hooks. Explicit timing keeps
        # that otherwise invisible cost separate from open/scandir in cProfile.
        if event not in ('open', 'subprocess.Popen') and not event.startswith('os.'):
            return self.audit(event, args)
        started = time.perf_counter()
        try:
            return self.audit(event, args)
        finally:
            self.stats['calls'] += 1
            self.stats['seconds'] += time.perf_counter() - started


def install_guard(*, profile=False):
    raw = os.environ.get('MIXIE_ISOLATED_TESTS')
    if not raw:
        raise RuntimeError('Use tests/run_isolated.py; never import tests in the live application.')
    root = Path(raw).resolve()
    script = Path.cwd().resolve()
    if root.parent != Path('/tmp') or script != root / 'MIXIE-Lab' / 'Scrubber':
        raise RuntimeError('Test workspace was not independently verified.')
    guard = FilesystemGuard(root, ('/home/deck/MIXIE-Lab', '/home/deck/Music',
                                   '/home/deck/.config/mixie'), site.getsitepackages(), profile=profile)
    original_open = os.open

    def guarded_open(path, flags, mode=0o777, *, dir_fd=None):
        # The CPython "open" audit event omits dir_fd. Verify the actual base
        # here as well; retaining the normal audit check protects other open APIs.
        if dir_fd not in (None, -1) and not os.path.isabs(path):
            guard.checked_path(path, guard.writing(None, flags), dir_fd)
        return original_open(path, flags, mode, dir_fd=dir_fd)

    os.open = guarded_open
    sys.addaudithook(guard.timed_audit if profile else guard.audit)
    return guard.stats if profile else None


class WorkspaceCase(unittest.TestCase):
    def setUp(self):
        if not os.environ.get("MIXIE_ISOLATED_TESTS"):
            raise RuntimeError("Run through tests/run_isolated.py only.")
        import archive
        import sort
        import tag
        self.temporary = tempfile.TemporaryDirectory(prefix="case-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name) / "MIXIE-Lab"
        self.script = self.root / "Scrubber"
        self.stage = self.script / ".staging"
        self.inbox = self.script / "Inbox"
        self.library = self.root / "Music Library"
        self.backup = self.script / ".backups"
        self.home = self.root / "home"
        for folder in (self.script, self.stage, self.inbox, self.library, self.backup, self.home):
            folder.mkdir(parents=True, exist_ok=True)
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for module, values in (
            (tag, {"script_folder": self.script, "staging_folder": self.stage,
                   "library_folder": self.library}),
            (sort, {"script_folder": str(self.script), "drive_folder": str(self.root),
                    "staging_folder": str(self.stage), "inbox_folder": str(self.inbox),
                    "music_library_folder": str(self.library)}),
            (archive, {"backup_folder": self.backup}),
        ):
            for name, value in values.items():
                self.stack.enter_context(patch.object(module, name, value))
        self.stack.enter_context(patch.dict(os.environ, {"HOME": str(self.home),
            "ACOUSTID_CLIENT_KEY": "DISPOSABLE-OFFLINE-TEST-KEY"}))

    def track_path(self, name="Import/Album/01.flac", data=b"disposable fixture"):
        path = self.stage / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path


def metadata_record(path, **changes):
    record = {name: () for name in (
        "artist", "album_artist", "album", "title", "track_number", "track_total",
        "disc_number", "disc_total", "date", "genre")}
    record.update(path=Path(path), has_tags=True, artist=("Artist",),
                  album_artist=("Artist",), album=("Album",), title=(Path(path).stem,),
                  track_number=("1",), disc_number=("1",), date=("2020",), genre=("Rock",),
                  track_total_sources={}, disc_total_sources={})
    record.update(changes)
    return record


def make_cover():
    from PIL import Image
    output = BytesIO()
    Image.new("RGB", (32, 32), "purple").save(output, format="PNG")
    return {"mime": "image/png", "data": output.getvalue(), "source": "synthetic fixture"}


def make_audio(path, **tags):
    """Encode silence, then write tags; never use or copy commercial audio."""
    from mutagen import File
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([
        "/usr/bin/ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
        "-f", "lavfi", "-i", "anullsrc=r=44100:cl=mono", "-t", "0.15",
        "-n", str(path),
    ], check=True, timeout=15, capture_output=True)
    audio = File(path, easy=True)
    if audio.tags is None:
        audio.add_tags()
    for key, value in tags.items():
        audio[key] = [str(value)]
    audio.save()
    return path

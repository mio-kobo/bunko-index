"""Safe extraction and test discovery for Bunko package and toolchain archives.

Everything here treats archive bytes as hostile. Package archives are held to
the Bunko V1 source-only policy: regular, non-executable text files with
cross-platform-safe names. Toolchain archives come from a pinned, checksummed
official release; they may contain binaries but still cannot escape their
destination directory.
"""

import os
from pathlib import Path
import re
import stat
import tarfile
import zipfile

MAX_FILES = 2000
MAX_FILE_BYTES = 8 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
MAX_PATH_CHARS = 200
MAX_DEPTH = 12
TOOLCHAIN_MAX_FILES = 2000
TOOLCHAIN_MAX_FILE_BYTES = 128 * 1024 * 1024
TOOLCHAIN_MAX_TOTAL_BYTES = 512 * 1024 * 1024

SEGMENT = re.compile(r"[A-Za-z0-9._-]{1,100}")
SOURCE_SUFFIXES = (".mio", ".md", ".txt")
LEGAL_NAME = re.compile(r"(?:LICENSE|LICENCE|COPYING|NOTICE|AUTHORS|README)(?:-[A-Za-z0-9.]{1,40})?")
WINDOWS_RESERVED = re.compile(r"(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?", re.IGNORECASE)
# Mio's own test discovery skips these directories, so Bunko does too.
EXCLUDED_TEST_DIRECTORIES = {"build", "dist", "node_modules", "vendor", "cache", "tmp"}
BINARY_MAGIC = (b"\x7fELF", b"MZ", b"\xfe\xed\xfa\xce", b"\xfe\xed\xfa\xcf",
                b"\xce\xfa\xed\xfe", b"\xcf\xfa\xed\xfe", b"\xca\xfe\xba\xbe", b"#!")


class ArchiveError(ValueError):
    """The archive violates Bunko policy. The message names the offending entry."""

    code = "BUNKO_E_ARCHIVE"


def printable(value, limit=120):
    text = "".join(c if c.isprintable() else ascii(c)[1:-1] for c in str(value))
    return text if len(text) <= limit else text[:limit] + "..."


def check_relative_path(name, *, strict):
    """Return the path segments of a safe relative archive path, or raise."""
    if not isinstance(name, str) or not name:
        raise ArchiveError("entry has an empty name")
    if len(name) > MAX_PATH_CHARS:
        raise ArchiveError(f"path is longer than {MAX_PATH_CHARS} characters: {printable(name)}")
    if "\\" in name or "\0" in name or any(ord(c) < 0x20 or ord(c) == 0x7F for c in name):
        raise ArchiveError(f"path contains a backslash or control character: {printable(name)}")
    if name.startswith("/") or re.match(r"[A-Za-z]:", name):
        raise ArchiveError(f"absolute path: {printable(name)}")
    parts = name.rstrip("/").split("/")
    if len(parts) > MAX_DEPTH:
        raise ArchiveError(f"path is nested deeper than {MAX_DEPTH} levels: {printable(name)}")
    for part in parts:
        if part in ("", ".", ".."):
            raise ArchiveError(f"path traversal or empty segment: {printable(name)}")
        if strict:
            if not SEGMENT.fullmatch(part) or part.endswith(".") or WINDOWS_RESERVED.fullmatch(part):
                raise ArchiveError(
                    f"path segment {printable(part)!r} is not portable; use only letters, digits, '.', '_' and '-' "
                    "and avoid Windows reserved names")
    return parts


def check_package_file(name, mode, data):
    base = name.rsplit("/", 1)[-1]
    if not (base.endswith(SOURCE_SUFFIXES) or LEGAL_NAME.fullmatch(base)):
        raise ArchiveError(
            f"{printable(name)}: file type is not allowed in a source-only package "
            f"(allowed: {', '.join(SOURCE_SUFFIXES)} and LICENSE/COPYING/NOTICE/AUTHORS/README)")
    if mode & 0o7111:
        raise ArchiveError(f"{printable(name)}: executable or setuid file mode {mode & 0o7777:04o}")
    if data.startswith(BINARY_MAGIC):
        raise ArchiveError(f"{printable(name)}: executable or script payload")
    if b"\0" in data:
        raise ArchiveError(f"{printable(name)}: binary content (NUL byte)")
    try:
        data.decode("utf-8")
    except UnicodeDecodeError:
        raise ArchiveError(f"{printable(name)}: content is not valid UTF-8") from None


def extract_package(archive, destination):
    """Extract a Bunko source archive into a new directory, enforcing V1 policy.

    Archive paths are relative to the package root: there is no wrapper
    directory to strip. Returns the sorted list of extracted file paths.
    """
    destination = Path(destination)
    if destination.exists():
        raise ArchiveError(f"destination {printable(destination.name)} already exists")
    try:
        stream = tarfile.open(archive, mode="r|gz")
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ArchiveError(f"not a readable .tar.gz archive: {printable(error)}") from None
    seen = {}
    files = []
    total = 0
    pending = []  # (relative path, bytes); nothing touches disk until every entry passed.
    with stream:
        try:
            for member in stream:
                parts = check_relative_path(member.name, strict=True)
                relative = "/".join(parts)
                folded = relative.casefold()
                if folded in seen:
                    raise ArchiveError(f"duplicate or case-colliding entry: {printable(relative)}")
                seen[folded] = relative
                if member.isdir():
                    continue
                if not member.isreg() or member.issparse():
                    kind = "symlink" if member.issym() else "hard link" if member.islnk() else "special file"
                    raise ArchiveError(f"{printable(relative)}: {kind} entries are not allowed")
                if len(files) >= MAX_FILES:
                    raise ArchiveError(f"more than {MAX_FILES} files")
                if member.size > MAX_FILE_BYTES:
                    raise ArchiveError(f"{printable(relative)}: larger than {MAX_FILE_BYTES} bytes")
                total += member.size
                if total > MAX_TOTAL_BYTES:
                    raise ArchiveError(f"extracted size exceeds {MAX_TOTAL_BYTES} bytes")
                data = stream.extractfile(member).read(member.size)
                if len(data) != member.size:
                    raise ArchiveError(f"{printable(relative)}: truncated entry")
                check_package_file(relative, member.mode, data)
                files.append(relative)
                pending.append((relative, data))
        except (tarfile.TarError, OSError, EOFError) as error:
            raise ArchiveError(f"corrupt archive: {printable(error)}") from None
    # A file may not also be a directory prefix of another entry.
    names = set(files)
    for relative in files:
        parent = relative
        while "/" in parent:
            parent = parent.rsplit("/", 1)[0]
            if parent in names:
                raise ArchiveError(f"{printable(parent)} is both a file and a directory")
    if not files:
        raise ArchiveError("archive contains no files")
    for relative, data in pending:
        target = destination.joinpath(*relative.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as out:
            out.write(data)
    return sorted(files)


def extract_toolchain(archive, destination, binary):
    """Extract a pinned official Mio release, stripping its single top directory.

    Returns the path of the compiler binary, made executable. Links, devices,
    traversal and oversized members are rejected, but binaries are expected.
    """
    destination = Path(destination)
    destination.mkdir(parents=True)
    archive = str(archive)
    entries = []  # (path parts below the top directory, reader handle)

    top = set()
    total = 0
    count = 0
    if archive.endswith(".zip"):
        with zipfile.ZipFile(archive) as bundle:
            for info in bundle.infolist():
                if info.is_dir():
                    top.add(check_relative_path(info.filename, strict=False)[0])
                    continue
                mode = info.external_attr >> 16
                if stat.S_IFMT(mode) not in (0, stat.S_IFREG):
                    raise ArchiveError(f"{printable(info.filename)}: links and special files are not allowed")
                parts = check_relative_path(info.filename, strict=False)
                top.add(parts[0])
                count += 1
                total += info.file_size
                if (count > TOOLCHAIN_MAX_FILES or info.file_size > TOOLCHAIN_MAX_FILE_BYTES
                        or total > TOOLCHAIN_MAX_TOTAL_BYTES):
                    raise ArchiveError("toolchain archive exceeds size limits")
                entries.append((parts[1:], info))
            _write_toolchain(entries, destination, lambda info: bundle.read(info), top)
    else:
        with tarfile.open(archive, mode="r|gz") as stream:
            data = {}
            for member in stream:
                parts = check_relative_path(member.name, strict=False)
                top.add(parts[0])
                if member.isdir():
                    continue
                if not member.isreg():
                    raise ArchiveError(f"{printable(member.name)}: links and special files are not allowed")
                count += 1
                total += member.size
                if (count > TOOLCHAIN_MAX_FILES or member.size > TOOLCHAIN_MAX_FILE_BYTES
                        or total > TOOLCHAIN_MAX_TOTAL_BYTES):
                    raise ArchiveError("toolchain archive exceeds size limits")
                key = "/".join(parts[1:])
                data[key] = stream.extractfile(member).read(member.size)
                entries.append((parts[1:], key))
            _write_toolchain(entries, destination, lambda key: data[key], top)
    path = destination.joinpath(*binary.split("/"))
    info = path.lstat()
    if not stat.S_ISREG(info.st_mode):
        raise ArchiveError(f"toolchain does not contain {printable(binary)}")
    path.chmod(0o755)
    return path


def _write_toolchain(entries, destination, read, top):
    if len(top) != 1:
        raise ArchiveError("toolchain archive must have exactly one top-level directory")
    seen = set()
    for parts, handle in entries:
        if not parts:
            raise ArchiveError("toolchain archive has a file at its top level")
        relative = "/".join(parts)
        if relative.casefold() in seen:
            raise ArchiveError(f"duplicate toolchain entry: {printable(relative)}")
        seen.add(relative.casefold())
        target = destination.joinpath(*parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "xb") as out:
            out.write(read(handle))


def discover_tests(root):
    """List `*_test.mio` files below root as sorted POSIX paths, like `mio test ./...`."""
    root = Path(root)
    found = []
    for directory, names, files in os.walk(root, followlinks=False):
        names[:] = sorted(n for n in names if not n.startswith(".") and n not in EXCLUDED_TEST_DIRECTORIES)
        for name in sorted(files):
            if name.endswith("_test.mio"):
                full = Path(directory, name)
                if stat.S_ISREG(full.lstat().st_mode):
                    found.append(full.relative_to(root).as_posix())
    return sorted(found)

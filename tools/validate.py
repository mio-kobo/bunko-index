#!/usr/bin/env python3
"""Validate the Bunko v1 index without downloading or executing packages."""

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import sys
import tomllib


INDEX_HOME = Path(__file__).resolve().parents[1]
SEGMENT = r"[a-z0-9]+(?:-[a-z0-9]+)*"
NAME = re.compile(rf"{SEGMENT}/{SEGMENT}")
VERSION = re.compile(r"(?:0|[1-9][0-9]{0,8})(?:\.(?:0|[1-9][0-9]{0,8})){2}")
GITHUB_REPO = re.compile(r"https://github\.com/([a-z0-9-]+)/([A-Za-z0-9_.-]+)")
ARCHIVE = re.compile(
    r"https://api\.github\.com/repos/([a-z0-9-]+)/([A-Za-z0-9_.-]+)/releases/assets/[1-9][0-9]{0,19}"
)
MAX_RECORD_BYTES = 64 * 1024
OPERATING_SYSTEMS = ("linux", "macos", "windows")
ARCHITECTURES = ("x86_64", "arm64")
TARGET_KEYS = ("runner", "mio_version", "mio_asset", "mio_sha256", "mio_binary")
Release = tuple[str, str]


@dataclass(frozen=True)
class Issue:
    code: str
    path: str
    message: str
    warning: bool = False


@dataclass(frozen=True)
class Package:
    path: str
    dependencies: tuple[Release, ...]
    platforms: frozenset[str] = frozenset()  # "os/arch" targets the release declares


def release_label(release):
    return "@".join(release)


def valid_name(name):
    return bool(NAME.fullmatch(name)) and all(len(part) <= 39 for part in name.split("/"))


def parse_release(value):
    name, _, version = value.partition("@")
    if not valid_name(name) or not VERSION.fullmatch(version):
        raise ValueError(f"expected an exact name@version, got {value!r}")
    return name, version


def path_release(path):
    parts = path.split("/")
    if len(parts) != 4 or parts[0] != "packages" or not parts[3].endswith(".toml"):
        raise ValueError("expected packages/<namespace>/<package>/<version>.toml")
    return parse_release(f"{parts[1]}/{parts[2]}@{parts[3][:-5]}")


def damerau_levenshtein(left, right):
    """Unrestricted edit distance with adjacent transpositions."""
    n, m = len(left), len(right)
    sentinel = n + m
    grid = [[sentinel] * (m + 2) for _ in range(n + 2)]
    for i in range(n + 1):
        grid[i + 1][1] = i
    for j in range(m + 1):
        grid[1][j + 1] = j
    last_row = {}
    for i, a in enumerate(left, 1):
        last_match = 0
        for j, b in enumerate(right, 1):
            row = last_row.get(b, 0)
            column = last_match
            cost = int(a != b)
            if not cost:
                last_match = j
            grid[i + 1][j + 1] = min(
                grid[i][j] + cost,
                grid[i + 1][j] + 1,
                grid[i][j + 1] + 1,
                grid[row][column] + (i - row - 1) + 1 + (j - column - 1),
            )
        last_row[a] = i
    return grid[n + 1][m + 1]


def classify_name(name, publisher, namespaces, patterns, distances):
    namespace = name.split("/")[0]
    if namespace in namespaces:
        if publisher not in namespaces[namespace]["publishers"]:
            return "reject", f"{publisher!r} is not authorized for {namespace}/*"
        return "pass", ""
    if publisher != f"github:{namespace}":
        return "reject", f"publisher must be github:{namespace}"
    for reserved, pattern in patterns.items():
        if pattern.fullmatch(namespace):
            return "manual_review", f"namespace resembles {reserved!r} visually"
    for reserved, distance in distances.items():
        if damerau_levenshtein(namespace, reserved) <= distance:
            return "manual_review", f"namespace is within edit distance {distance} of {reserved!r}"
    return "pass", ""


SCHEMA_KEYWORDS = {
    "$schema", "title", "type", "required", "additionalProperties", "properties",
    "propertyNames", "minProperties", "minLength", "maxLength", "pattern",
    "enum", "items", "minItems", "uniqueItems",
}


def schema_errors(value, schema, field="metadata"):
    """Apply the object, array and string keywords used by the bundled JSON Schema.

    Unsupported keywords raise instead of being ignored, so schema drift cannot
    silently weaken validation.
    """
    unknown = schema.keys() - SCHEMA_KEYWORDS
    if unknown:
        raise ValueError(f"unsupported schema keyword(s): {', '.join(sorted(unknown))}")
    kind = schema["type"]
    if kind == "object":
        if not isinstance(value, dict):
            yield field, "must be a table"
            return
        if len(value) < schema.get("minProperties", 0):
            yield field, "must not be empty"
        properties = schema.get("properties", {})
        for key in schema.get("required", []):
            if key not in value:
                yield f"{field}.{key}", "is required"
        for key, item in value.items():
            label = f"{field}.{key}"
            if key in properties:
                yield from schema_errors(item, properties[key], label)
            elif "additionalProperties" in schema and schema["additionalProperties"] is False:
                yield label, "is not an allowed field"
            else:
                names = schema.get("propertyNames")
                if names:
                    yield from schema_errors(key, names, f"{label} (key)")
                extra = schema.get("additionalProperties")
                if isinstance(extra, dict):
                    yield from schema_errors(item, extra, label)
    elif kind == "array":
        if not isinstance(value, list):
            yield field, "must be an array"
            return
        if len(value) < schema.get("minItems", 0):
            yield field, "must not be empty"
        for index, item in enumerate(value):
            yield from schema_errors(item, schema["items"], f"{field}[{index}]")
        if schema.get("uniqueItems"):
            # Compare by type and value so 1 and true are distinct, as in JSON.
            seen = set()
            for item in value:
                marker = (type(item).__name__, repr(item))
                if marker in seen:
                    yield field, "contains a duplicate entry"
                    break
                seen.add(marker)
    elif kind == "string":
        if not isinstance(value, str):
            yield field, "must be a string"
        elif "enum" in schema and value not in schema["enum"]:
            allowed = ", ".join(schema["enum"])
            yield field, f"must be one of: {allowed}"
        elif not schema.get("minLength", 0) <= len(value) <= schema.get("maxLength", len(value)):
            yield field, "length is outside the allowed range"
        elif "pattern" in schema and not re.search(schema["pattern"], value):
            yield field, "has an invalid format"
    else:
        raise ValueError(f"unsupported schema type: {kind}")


def read_toml(data):
    if len(data) > MAX_RECORD_BYTES:
        raise ValueError(f"file exceeds {MAX_RECORD_BYTES} bytes")
    return tomllib.loads(data.decode("utf-8"))


def git(root, *args):
    result = subprocess.run(
        ["git", "-C", str(root), *args], capture_output=True, check=False,
    )
    if result.returncode:
        raise ValueError(result.stderr.decode("utf-8", "replace").strip())
    return result.stdout


def git_ref(root, ref):
    return git(root, "rev-parse", "--verify", "--end-of-options", f"{ref}^{{commit}}").decode().strip()


def git_entries(root, ref):
    entries = {}
    for record in git(root, "ls-tree", "-rz", ref, "--", "packages").split(b"\0"):
        if record:
            info, path = record.split(b"\t", 1)
            mode, _, oid = info.decode("ascii").split()
            entries[os.fsdecode(path)] = mode, oid
    return entries


def working_entries(root):
    entries = {}
    pending = [root / "packages"]
    while pending:
        path = pending.pop()
        try:
            info = path.lstat()
        except FileNotFoundError:
            if path == root / "packages":
                continue
            raise
        if stat.S_ISDIR(info.st_mode):
            pending.extend(path.iterdir())
        else:
            mode = "unsafe"
            if stat.S_ISREG(info.st_mode):
                mode = "100755" if info.st_mode & 0o111 else "100644"
            entries[path.relative_to(root).as_posix()] = mode, path
    return entries


def load_targets(policies=None):
    """Return the repository-owned map of "os/arch" -> test target settings."""
    document = read_toml(((policies or INDEX_HOME / "policies") / "platform-targets.toml").read_bytes())
    targets = document["targets"]
    for name, rule in targets.items():
        os_name, _, arch = name.partition("/")
        if (os_name not in OPERATING_SYSTEMS or arch not in ARCHITECTURES
                or not all(isinstance(rule.get(key), str) and rule[key] for key in TARGET_KEYS)
                or rule.keys() - set(TARGET_KEYS)):
            raise ValueError(f"invalid platform target {name!r}")
    return targets


def declared_targets(document):
    """Flatten a validated [platforms] table to a sorted tuple of "os/arch"."""
    return tuple(sorted(f"{os_name}/{arch}"
                        for os_name, archs in document["platforms"].items() for arch in archs))


def validate_records(root, entries, base, settings, author_id=None):
    schema, namespaces, patterns, distances, authorized, targets = settings
    packages = {}
    invalid = {}
    issues = []
    seen = {}
    for path, (mode, source) in sorted(entries.items()):
        expected = None
        try:
            expected = path_release(path)
            if mode != "100644":
                raise ValueError("package records must be regular non-executable files")
            if isinstance(source, Path):
                with source.open("rb") as stream:
                    data = stream.read(MAX_RECORD_BYTES + 1)
            else:
                size = int(git(root, "cat-file", "-s", source))
                if size > MAX_RECORD_BYTES:
                    raise ValueError(f"file exceeds {MAX_RECORD_BYTES} bytes")
                data = git(root, "cat-file", "blob", source)
            document = read_toml(data)
        except (OSError, ValueError, RecursionError) as error:
            issues.append(Issue("BUNKO_E_RECORD", path, str(error)))
            if expected is not None:
                invalid[expected] = "target record could not be read"
            continue

        errors = list(schema_errors(document, schema))
        if errors:
            issues.extend(Issue("BUNKO_E_SCHEMA", path, f"{field} {msg}") for field, msg in errors)
            invalid[expected] = "target record fails the metadata schema"
            continue
        key = document["name"], document["version"]
        if key in seen:
            issues.append(Issue("BUNKO_E_DUPLICATE", path, f"{release_label(key)} also in {seen[key]}"))
        seen[key] = path
        if key != expected:
            issues.append(Issue("BUNKO_E_PATH", path, f"metadata identifies {release_label(key)}"))
            invalid[expected] = "target record disagrees with its path"
            continue

        risk, message = classify_name(key[0], document["publisher"], namespaces, patterns, distances)
        namespace = key[0].split("/")[0]
        if author_id is not None and path not in base:
            # A PR cannot authorize itself by editing the proposed allowlist.
            if namespace in authorized or namespace in namespaces:
                rule = authorized.get(namespace, {})
                if (author_id not in rule.get("submitter_ids", [])
                        or document["publisher"] not in rule.get("publishers", [])):
                    risk, message = "reject", "PR author is not authorized by the base branch"
        if risk == "reject":
            issues.append(Issue("BUNKO_E_IDENTITY", path, message))
            invalid[expected] = message
            continue
        if risk == "manual_review" and path not in base:
            issues.append(Issue("BUNKO_W_MANUAL_REVIEW", path, message + "; maintainer review required", True))

        repository = GITHUB_REPO.fullmatch(document["repository"])
        archive = ARCHIVE.fullmatch(document["archive"])
        if (not repository or not archive or repository.groups() != archive.groups()
                or repository[2] in {".", ".."}
                or document["publisher"] != f"github:{repository[1]}"):
            message = "repository must belong to the publisher and archive must name an asset ID in that repository"
            issues.append(Issue("BUNKO_E_SOURCE", path, message))
            invalid[expected] = message
            continue

        mio = document["mio"]
        if tuple(map(int, mio["min"].split("."))) >= tuple(map(int, mio["before"].split("."))):
            message = "mio.min must precede mio.before"
            issues.append(Issue("BUNKO_E_COMPATIBILITY", path, message))
            invalid[expected] = message
            continue

        declared = declared_targets(document)
        unsupported = [target for target in declared if target not in targets]
        if unsupported:
            message = (f"unsupported platform target(s): {', '.join(unsupported)}; "
                       f"V1 targets are {', '.join(sorted(targets))}")
            issues.append(Issue("BUNKO_E_PLATFORM", path, message))
            invalid[expected] = message
            continue

        dependencies = tuple(sorted(document["dependencies"].items()))
        packages[key] = Package(path, dependencies, frozenset(declared))
    return packages, invalid, issues


def check_immutability(root, entries, base):
    for path, (mode, oid) in sorted(base.items()):
        entry = entries.get(path)
        same = entry == (mode, oid)
        if entry and entry[0] == mode and isinstance(entry[1], Path) and mode == "100644":
            # Read at most one extra byte to detect a size change.
            original = git(root, "cat-file", "blob", oid)
            with entry[1].open("rb") as stream:
                same = stream.read(len(original) + 1) == original
        if not same:
            yield Issue("BUNKO_E_IMMUTABLE", path, "published record changed or was removed")


def check_graph(roots, packages, invalid, revoked, known_names):
    """Walk one dependency closure. Keep predecessor links for full diagnostics."""
    parents = {}
    selected = {}
    active = set()
    visited = set()
    issues = []

    def path_to(key):
        chain = []
        while key is not None:
            chain.append(release_label(key))
            key = parents[key]
        return " -> ".join(reversed(chain))

    def incoming_path(key, parent):
        label = release_label(key)
        return f"{path_to(parent)} -> {label}" if parent is not None else label

    def platform_issues(key, parent, parents, packages, source, incoming_path):
        """Every release in a closure must support every target of the root that reached it."""
        top = parent
        while parents[top] is not None:
            top = parents[top]
        missing = sorted(packages[top].platforms - packages[key].platforms)
        if missing:
            declared = ", ".join(sorted(packages[key].platforms))
            yield Issue("BUNKO_E_PLATFORM_DEPENDENCY", source,
                        f"{incoming_path(key, parent)} does not declare {', '.join(missing)} "
                        f"required by {release_label(top)} (declares: {declared})")

    pending = [(root, None, False) for root in reversed(roots)]
    while pending:
        key, parent, leaving = pending.pop()
        if leaving:
            active.remove(key)
            visited.add(key)
            continue

        origin = packages.get(parent if parent is not None else key)
        source = origin.path if origin else ""
        if key in revoked:
            issues.append(Issue("BUNKO_E_REVOKED", source, f"{incoming_path(key, parent)} is revoked ({revoked[key]})"))
            continue
        if key in invalid:
            issues.append(Issue("BUNKO_E_INVALID_DEPENDENCY", source, f"{incoming_path(key, parent)}: {invalid[key]}"))
            continue
        if key not in packages:
            code = "BUNKO_E_MISSING_VERSION" if key[0] in known_names else "BUNKO_E_MISSING_PACKAGE"
            issues.append(Issue(code, source, incoming_path(key, parent)))
            continue
        if key in active:
            issues.append(Issue("BUNKO_E_CYCLE", source, incoming_path(key, parent)))
            continue
        if parent is not None:
            issues.extend(platform_issues(key, parent, parents, packages, source, incoming_path))
        if key in visited:
            continue

        previous = selected.setdefault(key[0], key)
        if previous != key:
            issues.append(Issue("BUNKO_E_VERSION_CONFLICT", source, f"{path_to(previous)}\n{incoming_path(key, parent)}"))
        parents[key] = parent
        active.add(key)
        pending.append((key, parent, True))
        pending.extend((dep, key, False) for dep in reversed(packages[key].dependencies))
    return issues


def settings_for(root, base_ref, author_id):
    policies = INDEX_HOME / "policies"
    schema = json.loads((INDEX_HOME / "schemas/package.schema.json").read_text(encoding="utf-8"))
    namespaces = read_toml((policies / "reserved-names.toml").read_bytes())["namespaces"]
    similarity = read_toml((policies / "similarity-rules.toml").read_bytes())
    patterns = {name: re.compile(pattern) for name, pattern in similarity["visual_patterns"].items()}
    distances = similarity["edit_distance"]
    authorized = namespaces
    if author_id is not None:
        if not base_ref:
            raise ValueError("--author-id requires --base")
        policy_path = "policies/reserved-names.toml"
        if git(root, "ls-tree", base_ref, "--", policy_path):
            authorized = read_toml(git(root, "show", f"{base_ref}:{policy_path}"))["namespaces"]
        else:
            authorized = {}  # A policy must be merged before the first reserved release.
    if not {"mio", "kiso", "bunko"} <= namespaces.keys():
        raise ValueError("mio, kiso and bunko must remain reserved")
    for name, rule in namespaces.items():
        if (not isinstance(rule["publishers"], list)
                or not all(re.fullmatch(rf"github:{SEGMENT}", entry) for entry in rule["publishers"])
                or not isinstance(rule["submitter_ids"], list)
                or not all(type(entry) is int and entry > 0 for entry in rule["submitter_ids"])):
            raise ValueError(f"invalid authorization policy for {name}")
    if not patterns.keys() <= namespaces.keys() or not distances.keys() <= namespaces.keys():
        raise ValueError("similarity targets must be reserved namespaces")
    if not all(type(distance) is int and 0 <= distance <= 2 for distance in distances.values()):
        raise ValueError("edit distances must be integers from zero to two")
    revoked = {}
    for key, advisory in read_toml((policies / "revoked-versions.toml").read_bytes())["revoked"].items():
        if not isinstance(advisory, str) or not re.fullmatch(r"https://[^\s/]+/[^\s]+", advisory):
            raise ValueError(f"revocation for {key!r} requires a public HTTPS advisory URL")
        revoked[parse_release(key)] = advisory
    return (schema, namespaces, patterns, distances, authorized, load_targets(policies)), revoked


def report(issue, github):
    # Prevent untrusted text from becoming terminal escapes or workflow commands.
    def escaped(value):
        return "".join(c if c.isprintable() or c == "\n" else ascii(c)[1:-1] for c in value)

    def annotation(value, property_value=False):
        result = escaped(value).replace("%", "%25").replace("\n", "%0A")
        return result.replace(":", "%3A").replace(",", "%2C") if property_value else result

    level = "warning" if issue.warning else "error"
    if github:
        location = f"file={annotation(issue.path, True)}," if issue.path else ""
        print(f"::{level} {location}title={issue.code}::{annotation(issue.message)}")
    else:
        print(f"{level}: {issue.code} {escaped(issue.path)!r}\n  {escaped(issue.message)}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path.cwd(), help="index checkout")
    parser.add_argument("--base", help="compare against published records at this Git commit")
    parser.add_argument("--candidate", help="read package blobs from this Git commit instead of disk")
    parser.add_argument("--author-id", type=int, help="GitHub PR author's numeric account ID")
    parser.add_argument("--roots", nargs="+", metavar="NAME@VERSION", help="also check a project's combined roots")
    parser.add_argument("--github", action="store_true", help="emit GitHub Actions annotations")
    args = parser.parse_args()
    try:
        base_ref = git_ref(args.root, args.base) if args.base else None
        base = git_entries(args.root, base_ref) if base_ref else {}
        if args.candidate:
            entries = git_entries(args.root, git_ref(args.root, args.candidate))
        else:
            entries = working_entries(args.root)
        settings, revoked = settings_for(args.root, base_ref, args.author_id)
        packages, invalid, issues = validate_records(args.root, entries, base, settings, args.author_id)
        issues.extend(check_immutability(args.root, entries, base))
        for key in sorted(revoked.keys() - packages.keys()):
            issues.append(Issue("BUNKO_E_POLICY", "policies/revoked-versions.toml",
                                f"revocation refers to unknown release {release_label(key)}"))
        known_names = {name for name, _ in packages.keys() | invalid.keys()}
        for key in sorted(packages.keys() - revoked.keys()):
            issues.extend(check_graph([key], packages, invalid, revoked, known_names))
        if args.roots:
            roots = [parse_release(root) for root in args.roots]
            issues.extend(check_graph(roots, packages, invalid, revoked, known_names))
    except (KeyError, OSError, RecursionError, TypeError, ValueError, re.error) as error:
        issues = [Issue("BUNKO_E_VALIDATION", "", str(error))]
        packages = {}
    for issue in dict.fromkeys(issues):
        report(issue, args.github)
    if any(not issue.warning for issue in issues):
        return 1
    print(f"Validated {len(packages)} package version(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())

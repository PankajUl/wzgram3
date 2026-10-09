import ast
import inspect
import json
import re
import sys
import textwrap
from pathlib import Path

import pytest

import pyrogram
from pyrogram.methods import Methods


ROOT = Path(__file__).resolve().parents[1]
COMPILER = ROOT / "compiler" / "docs" / "compiler.py"


# Entries in the categories dicts sit at exactly twelve spaces
LISTED = set(re.findall(r"^\s{12}(\w+)$", COMPILER.read_text(encoding="utf-8"), re.M))


# Client attributes that are internal machinery rather than public API
INTERNAL_METHODS = {
    "authorize",
    "authorize_qr",
    "fetch_peers",
    "get_dc_option",
    "guess_extension",
    "guess_mime_type",
    "handle_download",
    "handle_updates",
    "load_plugins",
    "load_session",
    "media_pool_reaper",
    "get_file",
    "reap_media_sessions",
    "updates_watchdog",
}


def documented_alias_of(name):
    """Whether the attribute is another name for an already documented method.

    get_received_gifts is get_chat_gifts kept for compatibility; an alias needs
    no page of its own.
    """
    target = getattr(pyrogram.Client, name, None)

    return any(
        other != name
        and other in LISTED
        and getattr(pyrogram.Client, other, None) is target
        for other in dir(pyrogram.Client)
    )


def public_types():
    return sorted(
        name for name in dir(pyrogram.types)
        if isinstance(getattr(pyrogram.types, name), type)
        and issubclass(getattr(pyrogram.types, name), pyrogram.types.Object)
        and getattr(pyrogram.types, name) is not pyrogram.types.Object
    )


def public_enums():
    return sorted(pyrogram.enums.__all__)


@pytest.mark.parametrize("name", public_types())
def test_every_exported_type_is_documented(name):
    """A type exported but absent from the categories dict never renders.

    The docs compiler only emits what the dict names, so an exported class is
    silently missing from the site with nothing to notice it.
    """
    assert name in LISTED, (
        f"{name} is exported from pyrogram.types but has no entry in "
        f"compiler/docs/compiler.py, so it will not appear in the docs"
    )


@pytest.mark.parametrize("name", public_enums())
def test_every_exported_enum_is_documented(name):
    assert name in LISTED, (
        f"{name} is in pyrogram.enums.__all__ but has no entry in "
        f"compiler/docs/compiler.py"
    )


def test_every_public_client_method_is_documented():
    undocumented = sorted(
        name for name in dir(pyrogram.Client)
        if not name.startswith("_")
        and callable(getattr(pyrogram.Client, name, None))
        and name not in INTERNAL_METHODS
        and name not in LISTED
        and not documented_alias_of(name)
    )

    assert not undocumented, (
        "public Client methods with no entry in compiler/docs/compiler.py: "
        + ", ".join(undocumented)
    )


def test_the_categories_were_actually_read():
    assert len(LISTED) > 300, (
        "almost nothing was parsed out of the categories dicts, so these checks "
        "would pass whatever is missing"
    )


# Every public client method, against what it says about itself.
#
# The Bot API coverage gate runs this axis too, but only for the hundred methods the
# manifest maps to a spec entry. Two thirds of the surface is never checked, and that
# is where `send_location` and `send_venue` sat for however long with a docstring the
# interpreter could not see.
#
# `data_undocumented_params.json` is a frozen high-water mark, the same idea as the
# manifest's `pending:` list: it may shrink, never grow. Documenting a parameter and
# leaving it listed here fails just as loudly as adding a new undocumented one, so the
# file cannot rot into an exemption.
FROZEN = json.loads(
    (ROOT / "tests" / "data_undocumented_params.json").read_text(encoding="utf-8")
)


IGNORE = {"self", "args", "kwargs"}


# a decorator returns a decorator; saying so in a Returns: block is noise, and all
# twenty-odd of them agree on that
DECORATORS = tuple(n for n in dir(Methods) if n.startswith("on_"))


def public_methods():
    for name in sorted(dir(Methods)):
        if name.startswith("_"):
            continue

        fn = getattr(Methods, name, None)

        if callable(fn):
            yield name, fn


def docstring(fn):
    """The docstring as the file has it, not as the interpreter dedents it."""

    try:
        source = textwrap.dedent(inspect.getsource(fn))
    except OSError:
        return None

    return ast.get_docstring(ast.parse(source).body[0], clean=False)


def documented(doc):
    """The parameters the reST ``Parameters:`` block claims exist.

    ``Other Parameters:`` describes what a progress callback is handed, not what the
    method takes, and it ends the block — a parameter documented below it is
    documented in the wrong place.
    """

    import re

    section = re.compile(r"^\s{4}(\w[\w ]*):\s*$")
    parameter = re.compile(r"^\s{8}(\w+(?:\s*,\s*\w+)*)\s*\(")

    found, inside = set(), False

    for line in (doc or "").splitlines():
        heading = section.match(line)

        if heading:
            inside = heading.group(1) == "Parameters"
            continue

        if inside:
            hit = parameter.match(line)

            if hit:
                found.update(n.strip() for n in hit.group(1).split(","))

    return found
METHODS = list(public_methods())


@pytest.mark.parametrize("name,fn", METHODS, ids=[n for n, _ in METHODS])
def test_every_method_has_a_docstring_the_interpreter_can_see(name, fn):
    """A string that is not the first statement is not a docstring."""

    assert docstring(fn), (
        f"{name} has no docstring, or has one the interpreter cannot see because "
        f"another statement comes first"
    )


@pytest.mark.parametrize("name,fn", METHODS, ids=[n for n, _ in METHODS])
def test_every_method_says_what_it_returns(name, fn):
    doc = docstring(fn) or ""

    if name in DECORATORS:
        pytest.skip("a decorator returns a decorator")

    assert "Returns:" in doc or "Yields:" in doc, f"{name} says nothing about its result"


@pytest.mark.parametrize("name,fn", METHODS, ids=[n for n, _ in METHODS])
def test_no_method_documents_a_parameter_it_does_not_accept(name, fn):
    """This one has no frozen list. A phantom parameter is always a defect."""

    real = {p for p in inspect.signature(fn).parameters if p not in IGNORE}
    phantom = documented(docstring(fn)) - real

    assert not phantom, f"{name} documents {', '.join(sorted(phantom))}, which it does not accept"


@pytest.mark.parametrize("name,fn", METHODS, ids=[n for n, _ in METHODS])
def test_undocumented_parameters_only_shrink(name, fn):
    real = {p for p in inspect.signature(fn).parameters if p not in IGNORE}

    if not real:
        return

    doc = docstring(fn)

    assert doc is not None

    missing = real - documented(doc)
    frozen = set(FROZEN.get(name, ()))

    new = missing - frozen

    assert not new, (
        f"{name} accepts {', '.join(sorted(new))} without documenting "
        f"{'them' if len(new) > 1 else 'it'}"
    )

    closed = frozen - missing

    assert not closed, (
        f"{name} now documents {', '.join(sorted(closed))}; drop "
        f"{'them' if len(closed) > 1 else 'it'} from "
        f"tests/data_undocumented_params.json so the list keeps shrinking"
    )


def test_the_frozen_list_names_real_methods():
    """A rename would otherwise leave an entry that exempts nothing."""

    known = {name for name, _ in METHODS}
    stale = sorted(set(FROZEN) - known)

    assert not stale, f"tests/data_undocumented_params.json names {stale}"


# Examples import ``wzgram``; the library imports ``pyrogram``.
#
# Both names reach the same module, so an example that says ``pyrogram`` still runs
# and nothing here fails at import time. It drifts back one file at a time instead,
# which is why this walks the tree rather than trusting a sweep.
PYROGRAM_IMPORT = re.compile(
    r"(?m)^\s*(?:from pyrogram(?:\.[a-zA-Z_][a-zA-Z0-9_]*)* import\b|import pyrogram\s*$)"
)


# generated, or a generator of library code rather than of an example
SKIPPED = (
    ROOT / "docs" / "build",
    ROOT / "docs" / "source" / "telegram",
    ROOT / "docs" / "source" / "api" / "methods",
    ROOT / "docs" / "source" / "api" / "types",
    ROOT / "docs" / "source" / "api" / "bound-methods",
)


# repr() emits `pyrogram.types.X(...)`, so eval() of one needs that name bound;
# `import wzgram` binds the alias, not the name the repr uses
EXEMPT = {ROOT / "docs" / "source" / "topics" / "serializing.rst"}


def documentation_files():
    files = [
        p
        for p in ROOT.joinpath("docs", "source").rglob("*.rst")
        if not any(skip in p.parents for skip in SKIPPED)
    ]
    files.append(ROOT / "README.md")
    files.extend(ROOT.joinpath("compiler", "docs", "template").glob("*.rst"))

    return sorted(files)


def python_files():
    return sorted(
        p
        for p in ROOT.joinpath("pyrogram").rglob("*.py")
        if "raw" not in p.parts and "__pycache__" not in p.parts
    )


@pytest.mark.parametrize(
    "path", documentation_files(), ids=lambda p: str(p.relative_to(ROOT))
)
def test_documentation_examples_import_wzgram(path):
    if path in EXEMPT:
        pytest.skip("documented exception")

    found = PYROGRAM_IMPORT.findall(path.read_text(encoding="utf-8"))

    assert not found, f"{path.relative_to(ROOT)} still imports pyrogram in an example"


def test_docstring_examples_import_wzgram():
    """Only docstrings. Every other import in the tree is the library's own."""

    offenders = []
    scanned = 0

    for path in python_files():
        source = path.read_text(encoding="utf-8")

        if "pyrogram" not in source:
            continue

        for node in ast.walk(ast.parse(source)):
            if not isinstance(
                node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
            ):
                continue

            doc = ast.get_docstring(node, clean=False)

            if doc is None or "Example:" not in doc:
                continue

            scanned += 1

            if PYROGRAM_IMPORT.search(doc):
                name = getattr(node, "name", "<module>")
                offenders.append(f"{path.relative_to(ROOT)}:{name}")

    assert scanned > 300, f"only found {scanned} examples; the scan stopped working"
    assert not offenders, offenders


def test_the_library_itself_still_imports_pyrogram():
    """The package is pyrogram. A sweep that reaches the source breaks the build."""

    client = (ROOT / "pyrogram" / "client.py").read_text(encoding="utf-8")

    assert re.search(r"(?m)^from pyrogram import ", client), (
        "pyrogram/client.py must import pyrogram, not the alias"
    )

    for template in ROOT.joinpath("compiler", "methods", "templates").glob("*.j2"):
        body = template.read_text(encoding="utf-8")

        assert "from pyrogram import" in body, (
            f"{template.name} generates library code and must emit pyrogram imports"
        )

"""Visualize CPython 3.11's specializing, adaptive interpreter."""
import sys
import types

if sys.version_info < (3, 11) or sys.implementation.name != "cpython":
    raise RuntimeError("Specialist only supports CPython 3.11+!")
CODE = set()


@sys.addaudithook
def audit_imports(event: str, args: "typing.Sequence[object]") -> None:
    """Intercept all exec() calls and grab a reference to the code they execute.

    This is the only way I know of to actually get ahold of module-level code
    objects without modifying the code being run.
    """
    match event, args:
        case "exec", [types.CodeType(co_name="<module>") as code]:
            CODE.add(code)


# pylint: disable = wrong-import-order, wrong-import-position
import argparse
import collections
import colorsys
import contextlib
import dataclasses
import dis
import html
import http.server
import importlib.util
import itertools
import opcode
import pathlib
import runpy
import tempfile
import typing
import webbrowser

FIRST_POSTION = (1, 0)
LAST_POSITION = (sys.maxsize, 0)
SPECIALIZED_INSTRUCTIONS = frozenset(opcode._specialized_instructions)  # type: ignore  # pylint: disable = protected-access


class HTMLWriter:
    """Write HTML for a source code view."""

    def __init__(self, *, blue: bool, dark: bool) -> None:
        self._blue = blue
        self._dark = dark
        background_color, color = ("black", "white") if dark else ("white", "black")
        self._parts = [
            "<!doctype html>",
            "<html>",
            f"<body style='background-color:{background_color};color:{color}'>",
            "<pre>",
        ]

    def add(self, source: str, stats: "Stats") -> None:
        """Add a chunk of code to the output."""
        color = self._color(stats)
        attribute = "color" if self._dark else "background-color"
        source = html.escape(source)
        if color != "#ffffff":
            source = f"<span style='{attribute}:{color}'>{source}</span>"
        self._parts.append(source)

    def emit(self) -> str:
        """Emit the HTML."""
        self._parts.append("</pre>")
        self._parts.append("</body>")
        self._parts.append("</html>")
        out = "".join(self._parts)
        self._parts.clear()
        return out

    def _color(self, stats: "Stats") -> str:
        """Compute an RGB color code for this chunk."""
        quickened = stats.specialized + stats.adaptive
        if not quickened:
            return "#ffffff"
        # Red is 0/3, green is 1/3. This gives a hue along the red-green gradient
        # that reflects the hit rate:
        hue = 1 / 3 * stats.specialized / quickened
        if self._blue:
            # This turns our red-green (0/3 to 1/3) gradient into a red-blue (0/3 to
            # -1/3) gradient:
            hue = -hue
        lightness = max(1 / 2, stats.unquickened / (quickened + stats.unquickened))
        # Always fully saturate the color:
        saturation = 1
        rgb = colorsys.hls_to_rgb(hue, lightness, saturation)
        return f"#{int(255 * rgb[0]):02x}{int(255 * rgb[1]):02x}{int(255 * rgb[2]):02x}"


def is_superinstruction(instruction: dis.Instruction) -> bool:
    """Check if an instruction is a superinstruction."""
    return "__" in instruction.opname


def score_instruction(
    instruction: dis.Instruction, previous: dis.Instruction | None
) -> "Stats":
    """Score an instruction's importance."""
    if instruction.opname in SPECIALIZED_INSTRUCTIONS:
        if instruction.opname.endswith("_ADAPTIVE"):
            return Stats(adaptive=True)
        return Stats(specialized=True)
    if (
        previous is not None
        and is_superinstruction(previous)
        and not instruction.is_jump_target
    ):
        return Stats(specialized=True)
    return Stats(unquickened=True)


@contextlib.contextmanager
def catch_exceptions() -> typing.Generator[list[BaseException], None, None]:
    """Suppress exceptions, and gather them into a list."""
    caught: list[BaseException] = []
    try:
        yield caught
    except BaseException as exception:  # pylint: disable = broad-except
        caught.append(exception)


@contextlib.contextmanager
def patch_sys_argv(argv: typing.Iterable[str]) -> typing.Generator[None, None, None]:
    """Patch sys.argv to simulate a command line."""
    sys_argv = sys.argv[1:]
    sys.argv[1:] = argv
    try:
        yield
    finally:
        sys.argv[1:] = sys_argv


def main_file_for_module(module: str) -> pathlib.Path | None:
    """Get the main file for a module."""
    spec = importlib.util.find_spec(module)
    if spec is None:
        return None
    if spec.submodule_search_locations is not None:
        spec = importlib.util.find_spec(f"{module}.__main__")
        if spec is None:
            return None
    if not spec.has_location:
        return None
    assert spec.origin is not None
    return pathlib.Path(spec.origin)


@dataclasses.dataclass(frozen=True, slots=True)
class Stats:
    """Statistics about a source chunk."""

    specialized: int = 0
    adaptive: int = 0
    unquickened: int = 0

    def __add__(self, other: "Stats") -> "Stats":
        if not isinstance(other, Stats):
            return NotImplemented
        return Stats(
            specialized=self.specialized + other.specialized,
            adaptive=self.adaptive + other.adaptive,
            unquickened=self.unquickened + other.unquickened,
        )

    def __sub__(self, other: "Stats") -> "Stats":
        if not isinstance(other, Stats):
            return NotImplemented
        return Stats(
            specialized=self.specialized - other.specialized,
            adaptive=self.adaptive - other.adaptive,
            unquickened=self.unquickened - other.unquickened,
        )


@dataclasses.dataclass(frozen=True, slots=True)
class SourceChunk:
    """A chunk of source code."""

    start: tuple[int, int]
    stop: tuple[int, int]
    stats: Stats


def walk_code(code: types.CodeType) -> typing.Generator[types.CodeType, None, None]:
    """Walk a code object, yielding all of its sub-code objects."""
    yield code
    for constant in code.co_consts:
        if isinstance(constant, types.CodeType):
            yield from walk_code(constant)


def parse(code: types.CodeType) -> typing.Generator[SourceChunk, None, None]:
    """Parse a code object's source code into SourceChunks."""
    events: collections.defaultdict[tuple[int, int], Stats] = collections.defaultdict(
        Stats
    )
    events[FIRST_POSTION] = Stats()
    events[LAST_POSITION] = Stats()
    previous = None
    for child in walk_code(code):
        # dis has a bug in how position information is computed for CACHEs:
        fixed_positions = list(child.co_positions())
        for instruction in dis.get_instructions(child, adaptive=True):  # type: ignore
            position = fixed_positions[instruction.offset // 2]
            lineno, end_lineno, col_offset, end_col_offset = position
            if (
                lineno is None
                or end_lineno is None
                or col_offset is None
                or end_col_offset is None
            ):
                previous = instruction
                continue
            stats = score_instruction(instruction, previous)
            events[lineno, col_offset] += stats
            events[end_lineno, end_col_offset] -= stats
            previous = instruction
    stats = Stats()
    for (start, event), (stop, _) in itertools.pairwise(sorted(events.items())):
        stats += event
        yield SourceChunk(start, stop, stats)


def get_code_for_path(path: pathlib.Path) -> types.CodeType | None:
    """Get the code object for a file."""
    for code in CODE:
        try:
            if path.samefile(code.co_filename):
                return code
        except FileNotFoundError:
            pass
    return None


def view(path: pathlib.Path, *, blue: bool = False, dark: bool = False) -> None:
    """View a code object's source code."""
    writer = HTMLWriter(blue=blue, dark=dark)
    code = get_code_for_path(path)
    assert code is not None
    parser = parse(code)
    chunk = next(parser)
    group: list[str] = []
    with path.open() as file:
        for lineno, line in enumerate(file, 1):
            for col_offset, character in enumerate(line):
                if chunk.stop == (lineno, col_offset):
                    writer.add("".join(group), chunk.stats)
                    group.clear()
                    chunk = next(parser)
                    assert chunk.start == (lineno, col_offset)
                group.append(character)
    writer.add("".join(group), chunk.stats)
    browse(writer.emit())


def browse(page: str) -> None:
    """Open a web browser to display a page."""

    class RequestHandler(http.server.BaseHTTPRequestHandler):
        """A simple handler for a single web page."""

        def do_GET(self) -> None:  # pylint: disable = invalid-name
            """Serve the given HTML."""
            self.send_response(200)
            self.send_header("content-type", "text/html")
            self.end_headers()
            self.wfile.write(page.encode("utf-8"))

        def log_request(self, *_: object) -> None:
            """Don't log requests."""

    with http.server.HTTPServer(("localhost", 0), RequestHandler) as server:
        webbrowser.open_new_tab(f"http://localhost:{server.server_port}")
        server.handle_request()


class Args(typing.TypedDict):
    """Command line arguments."""

    blue: bool
    dark: bool
    targets: str | None
    command: typing.Sequence[str]
    module: typing.Sequence[str]
    file: typing.Sequence[str]


def parse_args(args: list[str] | None = None) -> Args:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(description=__doc__, add_help=False)
    options = parser.add_argument_group("Options")
    options.add_argument(
        "-b", "--blue", action="store_true", help="Use a red-blue color scheme."
    )
    options.add_argument(
        "-d", "--dark", action="store_true", help="Use a dark color scheme."
    )
    options.add_argument(
        "-h", "--help", action="help", help="Show this help message and exit."
    )
    options.add_argument(
        "-t",
        "--targets",
        metavar="<targets>",
        help="A glob-style pattern indicating target files to analyze.",
    )
    script = parser.add_argument_group(
        "Script (terminates argument list)"
    ).add_mutually_exclusive_group(required=True)
    script.add_argument(
        "-c",
        action="extend",
        help="Equivalent to: python -c ...",
        nargs="...",
        default=[],
        dest="command",
    )
    script.add_argument(
        "-m",
        action="extend",
        help="Equivalent to: python -m ...",
        nargs="...",
        default=[],
        dest="module",
    )
    script.add_argument(
        "file",
        action="extend",
        help="Equivalent to: python <file> ...",
        nargs="?",
        default=[],
        metavar="<file> ...",
        type=lambda s: [s],
    )
    parser.add_argument(
        action="extend", nargs="...", dest="file", help=argparse.SUPPRESS
    )
    return typing.cast(Args, vars(parser.parse_args(args)))


def main() -> None:
    """Run the main program."""
    args = parse_args()
    blue = args["blue"]
    dark = args["dark"]
    targets = args["targets"]
    path: pathlib.Path | None
    with tempfile.TemporaryDirectory() as work:
        match args:
            case {"command": [source, *argv], "module": [], "file": []}:
                path = pathlib.Path(work) / "__main__.py"
                path.write_text(source)
                with patch_sys_argv(argv), catch_exceptions() as caught:
                    runpy.run_path(  # pylint: disable = no-member
                        str(path), run_name="__main__"
                    )
            case {"command": [], "module": [source, *argv], "file": []}:
                with patch_sys_argv(argv), catch_exceptions() as caught:
                    runpy.run_module(  # pylint: disable = no-member
                        source, run_name="__main__"
                    )
                path = main_file_for_module(source)
            case {"command": [], "module": [], "file": [source, *argv]}:
                with patch_sys_argv(argv), catch_exceptions() as caught:
                    runpy.run_path(  # pylint: disable = no-member
                        source, run_name="__main__"
                    )
                path = pathlib.Path(source)
            case _:
                assert False, args
        paths: list[pathlib.Path] = []
        if targets is not None:
            for match in pathlib.Path().glob(targets):
                if get_code_for_path(match) is not None:
                    paths.append(match)
        elif path is not None:
            paths.append(path)
        if not paths:
            print("No source files found!")
        for path in paths:
            view(path, blue=blue, dark=dark)
        if caught:
            raise caught[0] from None


if __name__ == "__main__":
    main()

"""What a language adapter provides. Everything language-specific lives behind this interface; the
rest of ras-anything (fault plans, ranking, gates, bench, report) only moves JSON around.

An adapter is two halves:
  * this class, running in the ras-anything process: finds the project, reads source code, builds the
    command line, the stub and the marked block;
  * an *agent* in the app's own language, loaded into the app's process (and into the replay worker),
    that watches the dependency boundary. The two talk in the JSON described in docs/ARCHITECTURE.md.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from pathlib import Path


def program_stem(program: str) -> str:
    """"python3" from "python3", "/usr/bin/python3" or "C:\\Python\\python.exe"."""
    name = program.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


class LangAdapter(ABC):
    name = ""            # stored in scan.json / meta.json / calls.json, e.g. "python"
    label = ""           # for people, e.g. "Python"
    launchers = ""       # for people: the commands this adapter understands
    example = ""         # a command to show in hints, e.g. "python app.py"
    boundaries = ""      # for people: what is intercepted
    fence = ""           # Markdown code fence of the language
    hardened_name = ""   # the file the coding agent edits, e.g. "hardened.py" (see hardened_file)
    private_env: tuple = ()  # environment variables `attach` sets: never part of a recording
    bug_exceptions: frozenset = frozenset()  # exception types that mean a programming error

    # ---------------------------------------------------------------- commands

    @abstractmethod
    def detect(self, cmd: list[str]) -> bool:
        """Is this command ours? Decided from its first words, like a shell would."""

    @abstractmethod
    def resolve(self, cmd: list[str], cwd: Path) -> list[str]:
        """The command as it will really be started (env runners unwrapped, interpreter found)."""

    @abstractmethod
    def project_root(self, cmd: list[str], cwd: Path) -> Path:
        """The folder whose functions count as the user's own."""

    @abstractmethod
    def attach(self, cmd: list[str], env: dict) -> tuple[list[str], dict]:
        """Command and environment that start the app with the agent loaded. The agent's configuration
        is already in env["RAS_ANYTHING_CFG"]."""

    def prepare(self, cfg: dict, scratch: Path) -> dict:
        """Extra configuration for the agent of one run; files for it go in `scratch` (removed afterwards)."""
        return {}

    @abstractmethod
    def interpreter(self, project: Path, meta: dict) -> str:
        """The executable that replays recorded calls."""

    # ---------------------------------------------------------------- source code

    @abstractmethod
    def describe(self, fn: dict) -> dict | None:
        """Static facts about one function the agent reported ({"file", "line", "name", "module", ...}):
        id, module, qualname, kind, async, line, in_main, signals, static_risk, refused.
        None when it isn't a function of the project's source."""

    @abstractmethod
    def hardenability(self, d: dict) -> str | None:
        """None if the function can be swapped, else the reason it can't (yet)."""

    @abstractmethod
    def source(self, fn: dict, project: Path) -> str:
        """The function's source, as it would stand on its own in the hardened file."""

    @abstractmethod
    def stub(self, fn: dict, project: Path) -> str:
        """The hardened file to start from."""

    def hardened_file(self, fn: dict) -> str:
        """Name of the hardened file of one function (its scan entry or meta.json)."""
        return self.hardened_name

    @abstractmethod
    def callers(self, fn: dict, root: Path, limit: int = 4) -> list[str]:
        """A comment with file:line plus a few lines of code, for places that call the function."""

    @abstractmethod
    def task_texts(self, fn: dict) -> dict:
        """The language-specific paragraphs of TASK.md: kind, timeouts, conflicts, degrade, errors, helpers."""

    # ---------------------------------------------------------------- record / replay

    @abstractmethod
    def record_target(self, fn: dict, project: Path) -> dict:
        """cfg["target"] for the recording agent."""

    @abstractmethod
    def record_meta(self, fn: dict, data: dict, project: Path) -> dict:
        """Extra meta.json fields the replay worker needs (import paths, ...), from the recording run."""

    @abstractmethod
    def refuse_hardened(self, path: Path) -> str | None:
        """Why this hardened file isn't accepted for replay (e.g. it imports the harness), or None."""

    @abstractmethod
    def worker_cmd(self, exe: str, job: Path) -> tuple[list[str], dict]:
        """Command and environment of the replay worker for one job file."""

    # ---------------------------------------------------------------- enable

    @abstractmethod
    def markers(self, fid: str) -> tuple[str, str]:
        """First and last line of the marked block."""

    @abstractmethod
    def block(self, fid: str, meta: dict, copy_rel: str, vres: dict) -> str:
        """The whole marked block, "\\n" line ends."""

    @abstractmethod
    def insertion(self, path: Path, text: str, block: str, eol: str) -> tuple[int, str]:
        """Where in the module's text the block goes, and exactly what is inserted there."""

    def patches(self, path: Path, text: str, meta: dict) -> list[tuple[str, str]]:
        """Changes to the module besides the block, each (line, its replacement), both unique in the module.
        Only what rebinding the function needs; undone by disable."""
        return []

    def valid_patch(self, old: str, new: str) -> bool:
        """Could `patches` have made this change? (disable checks what state.json says)"""
        return False

    @abstractmethod
    def copy_name(self, fid: str, file: Path) -> str:
        """File name of the hardened copy in _ras_hardened/ for a function of module `file`."""

    @abstractmethod
    def runtime_file(self) -> tuple[Path, str]:
        """(runtime helpers to vendor next to the copy, file name there)."""

    # ---------------------------------------------------------------- people

    def doctor(self) -> list[tuple]:
        """(True | None | False, name, detail) lines for `ras-anything doctor`."""
        return []

    def runtime_label(self, data: dict | None) -> str:
        """"Python 3.13.5" for the report, from a run's data."""
        rt = (data or {}).get("runtime") or {}
        return ("%s %s" % (self.label, rt.get("version", ""))).strip()



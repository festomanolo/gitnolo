"""
GitLens-style conflict resolver: parses conflict hunks, shows current vs
incoming side by side, and can synthesize a merge with the configured AI model.
"""

import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from typing import List, Optional, Tuple, Dict, Any

from rich.panel import Panel
from rich.table import Table
from rich.syntax import Syntax
from rich.prompt import Prompt, Confirm
from rich.text import Text
from rich import box

from .gitcore import Repo
from .ollama_client import OllamaClient
from .ui import console

@dataclass
class ConflictHunk:
    file_path: str
    hunk_index: int
    start_line: int
    end_line: int
    ours_label: str
    ours_content: str
    theirs_label: str
    theirs_content: str
    base_content: Optional[str] = None
    context_before: str = ""
    context_after: str = ""
    resolved_content: Optional[str] = None

class ConflictFileParser:
    """Parses git conflict markers within a file and applies resolved hunks."""

    @staticmethod
    def parse_file(file_path: str) -> List[ConflictHunk]:
        """Parses all conflict hunks in a file."""
        if not os.path.isfile(file_path):
            return []

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                lines = f.readlines()
        except Exception:
            return []

        hunks: List[ConflictHunk] = []
        i = 0
        total_lines = len(lines)
        hunk_counter = 1

        while i < total_lines:
            line = lines[i]
            if line.startswith("<<<<<<<"):
                start_line = i + 1
                ours_label = line.lstrip("<").strip() or "HEAD (Current Change)"
                ours_lines = []
                base_lines = []
                theirs_lines = []
                theirs_label = "Incoming Change"
                state = "ours"

                # Capture context before
                ctx_start = max(0, i - 4)
                context_before = "".join(lines[ctx_start:i])

                i += 1
                while i < total_lines:
                    current = lines[i]
                    if current.startswith("|||||||"):
                        state = "base"
                    elif current.startswith("======="):
                        state = "theirs"
                    elif current.startswith(">>>>>>>"):
                        theirs_label = current.lstrip(">").strip() or "Incoming Change"
                        end_line = i + 1
                        break
                    else:
                        if state == "ours":
                            ours_lines.append(current)
                        elif state == "base":
                            base_lines.append(current)
                        elif state == "theirs":
                            theirs_lines.append(current)
                    i += 1

                # Capture context after
                ctx_end = min(total_lines, i + 5)
                context_after = "".join(lines[i+1:ctx_end])

                hunks.append(
                    ConflictHunk(
                        file_path=file_path,
                        hunk_index=hunk_counter,
                        start_line=start_line,
                        end_line=end_line,
                        ours_label=ours_label,
                        ours_content="".join(ours_lines),
                        theirs_label=theirs_label,
                        theirs_content="".join(theirs_lines),
                        base_content="".join(base_lines) if base_lines else None,
                        context_before=context_before,
                        context_after=context_after,
                    )
                )
                hunk_counter += 1
            i += 1

        return hunks

    @staticmethod
    def apply_resolutions(file_path: str, resolved_hunks: List[ConflictHunk]) -> bool:
        """Replaces conflict blocks in file with the resolved content."""
        if not os.path.isfile(file_path) or not resolved_hunks:
            return False

        try:
            with open(file_path, "r", encoding="utf-8", errors="ignore") as f:
                content = f.read()
        except Exception:
            return False

        # Build regex pattern to match each conflict marker block
        pattern = re.compile(
            r"<{7}[^\n]*\n(.*?)(?:\|{7}[^\n]*\n.*?)?={7}[^\n]*\n(.*?)>{7}[^\n]*\n",
            re.DOTALL,
        )

        hunk_iter = iter(resolved_hunks)

        def replacer(match):
            try:
                hunk = next(hunk_iter)
                if hunk.resolved_content is not None:
                    res = hunk.resolved_content
                    if res and not res.endswith("\n"):
                        res += "\n"
                    return res
                return match.group(0)  # Keep unresolved
            except StopIteration:
                return match.group(0)

        new_content = pattern.sub(replacer, content)

        try:
            with open(file_path, "w", encoding="utf-8") as f:
                f.write(new_content)
            return True
        except Exception:
            return False

class ConflictResolver:
    """Coordinates GitLens-style conflict inspection and resolution."""

    def __init__(self, repo: Repo, ollama_client: Optional[OllamaClient] = None):
        self.repo = repo
        self.ollama = ollama_client

    def _render_hunk_card(self, hunk: ConflictHunk, total_hunks: int):
        """Renders GitLens-style visual side-by-side or stacked diff card. Zero emojis."""
        title = Text()
        title.append(f"[Conflict Hunk {hunk.hunk_index} of {total_hunks}]", style="bold bright_yellow")
        title.append(f" {hunk.file_path}", style="bold white")
        title.append(f" (Lines {hunk.start_line}-{hunk.end_line})", style="dim")

        # Guess file syntax
        ext = os.path.splitext(hunk.file_path)[1].lstrip(".") or "txt"

        ours_syntax = Syntax(hunk.ours_content.strip() or "(empty)", ext, theme="monokai")
        theirs_syntax = Syntax(hunk.theirs_content.strip() or "(empty)", ext, theme="monokai")

        ours_panel = Panel(
            ours_syntax,
            title=f"[bold cyan]<<< Current Change (HEAD: {hunk.ours_label})[/bold cyan]",
            border_style="cyan",
            box=box.ROUNDED,
        )

        theirs_panel = Panel(
            theirs_syntax,
            title=f"[bold magenta]>>> Incoming Change ({hunk.theirs_label})[/bold magenta]",
            border_style="magenta",
            box=box.ROUNDED,
        )

        table = Table.grid(padding=1, expand=True)
        table.add_column(ratio=1)
        table.add_row(ours_panel)
        table.add_row(theirs_panel)

        console.print(Panel(table, title=title, border_style="bright_yellow", box=box.HEAVY))

    def _edit_in_editor(self, default_content: str) -> str:
        """Opens $EDITOR for manual edit fallback."""
        editor = os.environ.get("EDITOR", "nano")
        with tempfile.NamedTemporaryFile("w+", suffix=".tmp", delete=False) as tf:
            tf.write(default_content)
            temp_path = tf.name

        try:
            subprocess.run([editor, temp_path], check=True)
            with open(temp_path, "r", encoding="utf-8") as tf:
                return tf.read()
        except Exception:
            return default_content
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)

    def resolve_interactive(self) -> bool:
        """Main interactive conflict resolver loop. Zero emojis."""
        conflicted_files = self.repo.conflicted_files()
        if not conflicted_files:
            console.print(
                Panel(
                    "[bold green][OK] No active merge conflicts found in repository.[/bold green]",
                    border_style="green",
                    box=box.ROUNDED,
                )
            )
            return True

        console.print(
            Panel(
                f"[bold bright_yellow][!] Merge Conflicts Detected in {len(conflicted_files)} File(s):[/bold bright_yellow]\n"
                + "\n".join(f"  * [cyan]{f}[/cyan]" for f in conflicted_files),
                border_style="yellow",
                box=box.ROUNDED,
            )
        )

        resolved_files_count = 0

        for file_path in conflicted_files:
            full_path = os.path.join(self.repo.root, file_path)
            hunks = ConflictFileParser.parse_file(full_path)
            if not hunks:
                console.print(f"[dim]No standard conflict markers in {file_path}, skipping...[/dim]")
                continue

            console.print(f"\n[bold white]Resolving conflicts for:[/bold white] [bold cyan]{file_path}[/bold cyan] ({len(hunks)} hunks)\n")

            for hunk in hunks:
                self._render_hunk_card(hunk, len(hunks))

                console.print("[bold cyan]GitLens Resolution Actions:[/bold cyan]")
                console.print("  [bold cyan][1][/bold cyan] Accept Current Change (Ours / HEAD)")
                console.print("  [bold magenta][2][/bold magenta] Accept Incoming Change (Theirs)")
                console.print("  [bold green][3][/bold green] Accept Both (Current then Incoming)")
                console.print("  [bold yellow][4][/bold yellow] Accept Both (Incoming then Current)")
                console.print("  [bold bright_blue][5][/bold bright_blue] AI smart merge")
                console.print("  [bold white][6][/bold white] Edit manually in editor ($EDITOR)")
                console.print("  [bold dim][s][/bold dim] Skip this hunk")
                console.print("  [bold red][q][/bold red] Abort conflict resolution")

                choice = Prompt.ask("\n[bold cyan]Select action[/bold cyan]", choices=["1", "2", "3", "4", "5", "6", "s", "q"], default="5")

                if choice == "1":
                    hunk.resolved_content = hunk.ours_content
                    console.print("[green][OK] Accepted Current Change.[/green]")
                elif choice == "2":
                    hunk.resolved_content = hunk.theirs_content
                    console.print("[green][OK] Accepted Incoming Change.[/green]")
                elif choice == "3":
                    hunk.resolved_content = hunk.ours_content + hunk.theirs_content
                    console.print("[green][OK] Accepted Both (Current + Incoming).[/green]")
                elif choice == "4":
                    hunk.resolved_content = hunk.theirs_content + hunk.ours_content
                    console.print("[green][OK] Accepted Both (Incoming + Current).[/green]")
                elif choice == "5":
                    # AI Synthesis
                    if not self.ollama:
                        console.print("[red][!] Ollama client not available for AI merge.[/red]")
                        hunk.resolved_content = hunk.ours_content
                        continue

                    with console.status("[bold bright_blue]Synthesizing merge...[/bold bright_blue]", spinner="dots"):
                        res = self.ollama.resolve_conflict_ai(
                            file_path=file_path,
                            ours_code=hunk.ours_content,
                            theirs_code=hunk.theirs_content,
                            context_before=hunk.context_before,
                            context_after=hunk.context_after,
                            base_code=hunk.base_content,
                        )

                    merged_code = res.get("merged_code", "")
                    explanation = res.get("explanation", "")

                    ext = os.path.splitext(file_path)[1].lstrip(".") or "txt"
                    syntax = Syntax(merged_code, ext, theme="monokai", line_numbers=True)

                    console.print(
                        Panel(
                            syntax,
                            title="[bold green]AI Proposed Smart Merge[/bold green]",
                            subtitle=f"[italic dim]{explanation}[/italic dim]",
                            border_style="green",
                            box=box.ROUNDED,
                        )
                    )

                    confirm_ai = Prompt.ask(
                        "[bold cyan]Apply this AI resolution?[/bold cyan]",
                        choices=["y", "n", "e"],
                        default="y",
                    )

                    if confirm_ai == "y":
                        hunk.resolved_content = merged_code
                        console.print("[green][OK] Applied AI Smart Merge resolution.[/green]")
                    elif confirm_ai == "e":
                        hunk.resolved_content = self._edit_in_editor(merged_code)
                        console.print("[green][OK] Applied custom edited resolution.[/green]")
                    else:
                        console.print("[yellow]Resolution skipped.[/yellow]")
                        continue
                elif choice == "6":
                    hunk.resolved_content = self._edit_in_editor(hunk.ours_content + hunk.theirs_content)
                    console.print("[green][OK] Applied custom edited resolution.[/green]")
                elif choice == "s":
                    console.print("[dim]Skipped hunk.[/dim]")
                    continue
                elif choice == "q":
                    console.print("[yellow]Aborted conflict resolution.[/yellow]")
                    return False

            # Apply resolutions to the file
            if ConflictFileParser.apply_resolutions(full_path, hunks):
                console.print(f"[bold green][OK] Successfully resolved all conflict markers in {file_path}.[/bold green]")
                if Confirm.ask(f"Stage resolved file (git add {file_path})?", default=True):
                    self.repo.run("add", "--", file_path)
                resolved_files_count += 1

        # Check remaining conflicts
        remaining = self.repo.conflicted_files()
        if not remaining:
            console.print(
                Panel(
                    "[bold green][OK] All merge conflicts in the repository have been resolved.[/bold green]",
                    border_style="green",
                    box=box.ROUNDED,
                )
            )
            op = self.repo.operation_in_progress()
            if op in ("merge", "rebase", "cherry-pick", "revert"):
                if Confirm.ask(f"Continue the {op} now?", default=True):
                    args = {"merge": ["commit", "--no-edit"], "rebase": ["-c", "core.editor=true", "rebase", "--continue"],
                            "cherry-pick": ["-c", "core.editor=true", "cherry-pick", "--continue"],
                            "revert": ["-c", "core.editor=true", "revert", "--continue"]}[op]
                    code, out, err = self.repo.run_bytes(*args)
                    if code == 0:
                        console.print(f"[ok]{op} completed[/ok]")
                    else:
                        console.print(f"[warn]{err.decode().strip()[:300]}[/warn]")
            return True
        else:
            console.print(f"[yellow][!] Remaining conflicted files:[/yellow] {', '.join(remaining)}")
            return False

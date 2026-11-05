"""A small terminal emulator: it replays what a terminal program printed onto a grid of cells, so the text can be read back the way it looked on screen.

claude-web runs `claude setup-token` in a pseudo-terminal and has to read the token it prints. That command draws a terminal UI which only rewrites the cells that changed since its previous frame and jumps the cursor over the rest, spaces included. So the raw output never holds the token, or even "Paste code here", as one piece of text: a character that happened to be on screen already is simply not sent again. Replaying the output the way a terminal would puts every character back where it belongs.

Only the VT100/xterm sequences that terminal UIs actually use are understood: cursor movement, erasing, inserting and deleting, scrolling, scroll regions, saving the cursor. Colours, modes, hyperlinks and terminal queries don't change any cell, so they're skipped. Anything else is counted in `unhandled` instead of being guessed at.
"""

from __future__ import annotations

import codecs
import unicodedata

ESCAPE = "\x1b"
BELL = "\x07"
STRING_TERMINATOR = ESCAPE + "\\"
TAB_WIDTH = 8
# Plenty for anything a sign-in prints; it only stops a runaway program from growing the history forever.
HISTORY_LIMIT = 5000

# CSI final characters that never change a cell or move the cursor: colours (m), modes (h, l), status and device reports (n, c), window operations (t), cursor style and other settings (q, p), tab stops (g), printing (i), and assorted requests and resets (w, x, y, z, |, }, ~).
_HARMLESS_FINALS = frozenset("mhlnctqpgiwxyz|}~")


def character_width(character: str) -> int:
    """How many cells a character takes: 0 for combining marks and other invisible joiners, 2 for wide East Asian characters and most emoji, 1 for everything else."""
    if unicodedata.combining(character):
        return 0
    if unicodedata.category(character) in ("Mn", "Me", "Cf"):
        return 0
    if unicodedata.east_asian_width(character) in ("W", "F"):
        return 2
    return 1


class TerminalScreen:
    """A grid of character cells with a cursor, fed with the bytes a program wrote to its terminal."""

    def __init__(self, columns: int, rows: int) -> None:
        self.columns = columns
        self.rows = rows
        self.grid = [self._blank_row() for _ in range(rows)]
        # Rows that scrolled off the top, oldest first, so nothing printed earlier is lost.
        self.history: list[list[str]] = []
        self.cursor_row = 0
        self.cursor_column = 0
        # A real terminal doesn't wrap right after it writes in the last column. It waits until the next character arrives, and any cursor movement in between cancels the wrap.
        self.wrap_pending = False
        self.saved_cursor = (0, 0)
        self.scroll_top = 0
        self.scroll_bottom = rows - 1
        self.last_printed = " "
        # Control sequence finals this class doesn't know, so a test can insist there are none.
        self.unhandled: list[str] = []
        self._decoder = codecs.getincrementaldecoder("utf-8")(errors="replace")
        # An escape sequence cut off by the end of a chunk, kept until the rest of it arrives.
        self._partial_sequence = ""

    # Reading the screen

    def lines(self) -> list[str]:
        """Every row, the scrolled-off ones first, without trailing blanks."""
        all_rows = self.history + self.grid
        return ["".join(row).rstrip() for row in all_rows]

    def text(self) -> str:
        """The whole screen as text, one row per line, without the empty rows below the last thing drawn."""
        lines = self.lines()
        while lines and not lines[-1]:
            lines.pop()
        return "\n".join(lines)

    # Feeding output in

    def feed(self, data: bytes) -> None:
        """Apply a chunk of terminal output. Chunks can split a UTF-8 character or an escape sequence anywhere."""
        text = self._partial_sequence + self._decoder.decode(data)
        self._partial_sequence = ""
        index = 0
        length = len(text)
        while index < length:
            character = text[index]
            if character == ESCAPE:
                end = self._escape_sequence(text, index)
                if end is None:
                    self._partial_sequence = text[index:]
                    return
                index = end
                continue
            self._character(character)
            index += 1

    def _character(self, character: str) -> None:
        if character == "\r":
            self.cursor_column = 0
            self.wrap_pending = False
        elif character in "\n\x0b\x0c":
            # A bare line feed only moves down. The pty turns a program's "\n" into "\r\n" before we see it, so real output always carries both.
            self._line_feed()
        elif character == "\b":
            self._move_to_column(self.cursor_column - 1)
        elif character == "\t":
            next_stop = (self.cursor_column // TAB_WIDTH + 1) * TAB_WIDTH
            self._move_to_column(next_stop)
        elif ord(character) < 0x20 or 0x7F <= ord(character) < 0xA0:
            # Bell, shift in/out and the rest of the control characters leave the grid alone.
            return
        else:
            self._print(character)

    def _print(self, character: str) -> None:
        width = character_width(character)
        if width == 0:
            self._combine_with_previous(character)
            return
        if self.wrap_pending:
            self.cursor_column = 0
            self._line_feed()
        # A wide character that doesn't fit in the last column goes to the next line whole, as terminals do.
        if width == 2 and self.cursor_column == self.columns - 1:
            self.grid[self.cursor_row][self.cursor_column] = " "
            self.cursor_column = 0
            self._line_feed()
        row = self.grid[self.cursor_row]
        row[self.cursor_column] = character
        if width == 2:
            # The right half of a wide character holds nothing of its own.
            row[self.cursor_column + 1] = ""
        self.last_printed = character
        next_column = self.cursor_column + width
        if next_column >= self.columns:
            self.cursor_column = self.columns - 1
            self.wrap_pending = True
        else:
            self.cursor_column = next_column

    def _combine_with_previous(self, character: str) -> None:
        if self.wrap_pending:
            column = self.cursor_column
        else:
            column = self.cursor_column - 1
        if column < 0:
            return
        self.grid[self.cursor_row][column] += character

    # Escape sequences

    def _escape_sequence(self, text: str, start: int) -> int | None:
        """Apply the escape sequence that starts at text[start]. Returns the index just past it, or None if the text ends before it does."""
        if start + 1 >= len(text):
            return None
        kind = text[start + 1]
        if kind == "[":
            return self._control_sequence(text, start + 2)
        if kind == "]":
            # Operating system command: titles, hyperlinks, clipboard, queries. Ends at a bell or a string terminator.
            return self._skip_string(text, start + 2, allow_bell=True)
        if kind in "P_^X":
            # Device control, application program command, privacy message, start of string: all end at a string terminator.
            return self._skip_string(text, start + 2, allow_bell=False)
        if kind in "()*+-./#% ":
            # Character set designations and the like are one more character long and change no cell.
            if start + 2 >= len(text):
                return None
            return start + 3
        if kind == "7":
            self._save_cursor()
        elif kind == "8":
            self._restore_cursor()
        elif kind == "D":
            self._line_feed()
        elif kind == "E":
            self.cursor_column = 0
            self._line_feed()
        elif kind == "M":
            self._reverse_line_feed()
        elif kind == "c":
            self._reset()
        # Anything else (keypad modes, a stray string terminator) is two characters long and changes nothing.
        return start + 2

    def _skip_string(self, text: str, start: int, *, allow_bell: bool) -> int | None:
        index = start
        while index < len(text):
            character = text[index]
            if allow_bell and character == BELL:
                return index + 1
            if character == ESCAPE:
                if index + 1 >= len(text):
                    return None
                if text[index + 1] == "\\":
                    return index + 2
                # Any other escape cuts the string short and starts a sequence of its own, as in xterm.
                return index
            index += 1
        return None

    def _control_sequence(self, text: str, start: int) -> int | None:
        """Read a CSI sequence from text[start] (just after ESC [), apply it, and return the index past its final character."""
        index = start
        while index < len(text) and "\x30" <= text[index] <= "\x3f":
            index += 1
        parameters = text[start:index]
        intermediates_start = index
        while index < len(text) and "\x20" <= text[index] <= "\x2f":
            index += 1
        intermediates = text[intermediates_start:index]
        if index >= len(text):
            return None
        final = text[index]
        if not ("\x40" <= final <= "\x7e"):
            # A malformed sequence: drop what was read and let the stray character be handled normally.
            return index
        self._apply_control_sequence(parameters, intermediates, final)
        return index + 1

    def _apply_control_sequence(self, parameters: str, intermediates: str, final: str) -> None:
        # A private marker (?25h, >4m, <u, =...) sets a mode or asks the terminal a question. None of those touch a cell.
        if parameters[:1] in ("?", ">", "<", "="):
            return
        # Intermediate characters mark settings such as the cursor style ("SP q") or a soft reset ("! p").
        if intermediates:
            return
        numbers = _parse_numbers(parameters)
        count = _first(numbers, 1)
        if final == "A":
            self._move_vertically(-count)
        elif final in "Be":
            self._move_vertically(count)
        elif final in "Ca":
            self._move_to_column(self.cursor_column + count)
        elif final == "D":
            self._move_to_column(self.cursor_column - count)
        elif final == "E":
            self._move_vertically(count)
            self.cursor_column = 0
        elif final == "F":
            self._move_vertically(-count)
            self.cursor_column = 0
        elif final in "G`":
            self._move_to_column(count - 1)
        elif final == "d":
            self._move_to_row(count - 1)
        elif final in "Hf":
            row = _first(numbers, 1)
            column = _nth(numbers, 1, 1)
            self._move_to_row(row - 1)
            self._move_to_column(column - 1)
        elif final == "J":
            self._erase_in_display(_first(numbers, 0))
        elif final == "K":
            self._erase_in_line(_first(numbers, 0))
        elif final == "X":
            self._erase_characters(count)
        elif final == "P":
            self._delete_characters(count)
        elif final == "@":
            self._insert_characters(count)
        elif final == "L":
            self._insert_lines(count)
        elif final == "M":
            self._delete_lines(count)
        elif final == "S":
            self._scroll_up(count)
        elif final == "T":
            # With more than one number this is a mouse-tracking request, not a scroll.
            if len(numbers) <= 1:
                self._scroll_down(count)
        elif final == "r":
            self._set_scroll_region(numbers)
        elif final == "s":
            # With numbers this sets left and right margins, which this screen doesn't model.
            if not parameters:
                self._save_cursor()
        elif final == "u":
            self._restore_cursor()
        elif final == "I":
            for _ in range(count):
                self._character("\t")
        elif final == "Z":
            previous_stop = self.cursor_column
            for _ in range(count):
                previous_stop = max(0, (previous_stop - 1) // TAB_WIDTH * TAB_WIDTH)
            self._move_to_column(previous_stop)
        elif final == "b":
            for _ in range(count):
                self._print(self.last_printed)
        elif final in _HARMLESS_FINALS:
            return
        else:
            self.unhandled.append(final)

    # Cursor movement

    def _move_to_column(self, column: int) -> None:
        self.cursor_column = min(max(column, 0), self.columns - 1)
        self.wrap_pending = False

    def _move_to_row(self, row: int) -> None:
        self.cursor_row = min(max(row, 0), self.rows - 1)
        self.wrap_pending = False

    def _move_vertically(self, offset: int) -> None:
        # Inside the scroll region the cursor stops at its margins; outside it, at the edges of the screen.
        if self.scroll_top <= self.cursor_row <= self.scroll_bottom:
            lowest = self.scroll_top
            highest = self.scroll_bottom
        else:
            lowest = 0
            highest = self.rows - 1
        target = self.cursor_row + offset
        self.cursor_row = min(max(target, lowest), highest)
        self.wrap_pending = False

    def _line_feed(self) -> None:
        self.wrap_pending = False
        if self.cursor_row == self.scroll_bottom:
            self._scroll_up(1)
        elif self.cursor_row < self.rows - 1:
            self.cursor_row += 1

    def _reverse_line_feed(self) -> None:
        self.wrap_pending = False
        if self.cursor_row == self.scroll_top:
            self._scroll_down(1)
        elif self.cursor_row > 0:
            self.cursor_row -= 1

    def _save_cursor(self) -> None:
        self.saved_cursor = (self.cursor_row, self.cursor_column)

    def _restore_cursor(self) -> None:
        row, column = self.saved_cursor
        self._move_to_row(row)
        self._move_to_column(column)

    def _set_scroll_region(self, numbers: list[int | None]) -> None:
        top = _first(numbers, 1) - 1
        bottom = _nth(numbers, 1, self.rows) - 1
        bottom = min(bottom, self.rows - 1)
        if top < 0 or top >= bottom:
            return
        self.scroll_top = top
        self.scroll_bottom = bottom
        # Setting the region also sends the cursor home.
        self._move_to_row(0)
        self._move_to_column(0)

    def _reset(self) -> None:
        self.grid = [self._blank_row() for _ in range(self.rows)]
        self.cursor_row = 0
        self.cursor_column = 0
        self.wrap_pending = False
        self.scroll_top = 0
        self.scroll_bottom = self.rows - 1

    # Changing cells and rows

    def _blank_row(self) -> list[str]:
        return [" "] * self.columns

    def _erase_in_line(self, mode: int) -> None:
        row = self.grid[self.cursor_row]
        if mode == 0:
            first, last = self.cursor_column, self.columns
        elif mode == 1:
            first, last = 0, self.cursor_column + 1
        else:
            first, last = 0, self.columns
        for column in range(first, last):
            row[column] = " "

    def _erase_in_display(self, mode: int) -> None:
        if mode == 0:
            self._erase_in_line(0)
            for row in range(self.cursor_row + 1, self.rows):
                self.grid[row] = self._blank_row()
        elif mode == 1:
            self._erase_in_line(1)
            for row in range(0, self.cursor_row):
                self.grid[row] = self._blank_row()
        else:
            self.grid = [self._blank_row() for _ in range(self.rows)]
            # Mode 3 also clears the scrollback, the way xterm does.
            if mode == 3:
                self.history = []

    def _erase_characters(self, count: int) -> None:
        row = self.grid[self.cursor_row]
        last = min(self.cursor_column + count, self.columns)
        for column in range(self.cursor_column, last):
            row[column] = " "

    def _delete_characters(self, count: int) -> None:
        row = self.grid[self.cursor_row]
        count = min(count, self.columns - self.cursor_column)
        del row[self.cursor_column:self.cursor_column + count]
        row.extend([" "] * count)

    def _insert_characters(self, count: int) -> None:
        row = self.grid[self.cursor_row]
        count = min(count, self.columns - self.cursor_column)
        row[self.cursor_column:self.cursor_column] = [" "] * count
        del row[self.columns:]

    def _insert_lines(self, count: int) -> None:
        if not (self.scroll_top <= self.cursor_row <= self.scroll_bottom):
            return
        count = min(count, self.scroll_bottom - self.cursor_row + 1)
        for _ in range(count):
            del self.grid[self.scroll_bottom]
            self.grid.insert(self.cursor_row, self._blank_row())
        self.cursor_column = 0
        self.wrap_pending = False

    def _delete_lines(self, count: int) -> None:
        if not (self.scroll_top <= self.cursor_row <= self.scroll_bottom):
            return
        count = min(count, self.scroll_bottom - self.cursor_row + 1)
        for _ in range(count):
            del self.grid[self.cursor_row]
            self.grid.insert(self.scroll_bottom, self._blank_row())
        self.cursor_column = 0
        self.wrap_pending = False

    def _scroll_up(self, count: int) -> None:
        count = min(count, self.scroll_bottom - self.scroll_top + 1)
        for _ in range(count):
            removed = self.grid.pop(self.scroll_top)
            # Only rows leaving the very top of the screen go into the history, as in a real terminal.
            if self.scroll_top == 0:
                self.history.append(removed)
            self.grid.insert(self.scroll_bottom, self._blank_row())
        if len(self.history) > HISTORY_LIMIT:
            del self.history[:len(self.history) - HISTORY_LIMIT]

    def _scroll_down(self, count: int) -> None:
        count = min(count, self.scroll_bottom - self.scroll_top + 1)
        for _ in range(count):
            del self.grid[self.scroll_bottom]
            self.grid.insert(self.scroll_top, self._blank_row())


def _parse_numbers(parameters: str) -> list[int | None]:
    """The numbers in a CSI parameter string. An empty field means "use the default" and comes back as None; sub-parameters after a colon are ignored."""
    if not parameters:
        return []
    numbers: list[int | None] = []
    for field in parameters.split(";"):
        main_part = field.split(":", 1)[0]
        if main_part.isdigit():
            numbers.append(int(main_part))
        else:
            numbers.append(None)
    return numbers


def _nth(numbers: list[int | None], position: int, default: int) -> int:
    """The number at a position, or the default when it's missing, empty or zero (zero means the default for every sequence that uses this)."""
    if position >= len(numbers):
        return default
    value = numbers[position]
    if not value:
        return default
    return value


def _first(numbers: list[int | None], default: int) -> int:
    """The first number, for sequences where zero is a real value (erase modes) or means the default (counts and positions)."""
    if not numbers or numbers[0] is None:
        return default
    if numbers[0] == 0 and default == 1:
        return 1
    return numbers[0]


def render(data: bytes, columns: int, rows: int) -> str:
    """The text a terminal of this size would show after receiving data, scrolled-off rows included."""
    screen = TerminalScreen(columns, rows)
    screen.feed(data)
    return screen.text()

"""terminal_screen: replaying terminal output onto a grid, the way a terminal shows it."""
from __future__ import annotations

from pathlib import Path

from terminal_screen import TerminalScreen, render

_FIXTURES = Path(__file__).parent / "fixtures"
# `claude setup-token` 2.1.318 on a 220x50 pty, with a wrong code typed after this many bytes.
_REAL_CAPTURE = _FIXTURES / "setup_token_wrong_code_2.1.318.bin"
_REAL_CAPTURE_CODE_TYPED_AT = 2286


def _screen(data: bytes, columns: int = 20, rows: int = 5) -> TerminalScreen:
    screen = TerminalScreen(columns, rows)
    screen.feed(data)
    return screen


def test_plain_text_and_line_breaks() -> None:
    assert render(b"hello\r\nworld", 20, 5) == "hello\nworld"


def test_a_cell_the_program_skips_keeps_what_was_already_there() -> None:
    # The whole bug in one line: the second frame jumps over the 't' because it is already on screen.
    assert render(b"xxxtxx\rabc\x1b[5Gef", 20, 5) == "abctef"


def test_a_bare_line_feed_only_moves_down() -> None:
    # Real pty output always carries \r\n; a bare \n keeps the column, as on a terminal.
    assert render(b"ab\ncd", 20, 5) == "ab\n  cd"


def test_cursor_movement() -> None:
    # Position, up, down, back, forward, column, row: each letter lands where the move before it left the cursor.
    screen = _screen(b"\x1b[3;5HX\x1b[2AY\x1b[1BZ\x1b[3DW\x1b[2CV\x1b[1GU\x1b[3dT")
    assert screen.text() == "     Y\nU   W ZV\n T  X"


def test_movement_stops_at_the_edges() -> None:
    screen = _screen(b"\x1b[99A\x1b[99DA\x1b[99B\x1b[99CB", columns=10, rows=3)
    grid = ["".join(row) for row in screen.grid]
    assert grid[0][0] == "A"
    assert grid[2][9] == "B"


def test_erase_in_line() -> None:
    assert render(b"abcdef\x1b[4G\x1b[K", 20, 3) == "abc"
    assert render(b"abcdef\x1b[4G\x1b[1K", 20, 3) == "    ef"
    assert render(b"abcdef\x1b[4G\x1b[2K", 20, 3) == ""


def test_erase_in_display() -> None:
    assert render(b"one\r\ntwo\r\nthree\x1b[2;2H\x1b[J", 20, 5) == "one\nt"
    assert render(b"one\r\ntwo\r\nthree\x1b[2;2H\x1b[1J", 20, 5) == "\n  o\nthree"
    assert render(b"one\r\ntwo\x1b[2J", 20, 5) == ""


def test_writing_in_the_last_column_waits_before_wrapping() -> None:
    # Five columns: after "abcde" the cursor sits on the 'e'; a carriage return cancels the wrap.
    assert render(b"abcde\rX", 5, 3) == "Xbcde"
    assert render(b"abcdef", 5, 3) == "abcde\nf"
    # A cursor move also cancels it.
    assert render(b"abcde\x1b[1DX", 5, 3) == "abcXe"


def test_rows_scrolled_off_the_top_are_kept() -> None:
    assert render(b"1\r\n2\r\n3\r\n4\r\n5", 5, 3) == "1\n2\n3\n4\n5"


def test_sequences_that_change_no_cell_are_skipped() -> None:
    data = (
        b"\x1b[?25l\x1b[?2004h\x1b[>4m\x1b[<u\x1b[?u\x1b[>0q\x1b[c\x1b(B\x0f"
        b"\x1b[1;32mgreen\x1b[0m "
        b"\x1b]8;id=1;https://example.com\x07link\x1b]8;;\x07 "
        b"\x1b]8;;https://example.com\x1b\\st\x1b]8;;\x1b\\ "
        b"\x1b]7501;?\x07end"
    )
    screen = _screen(data, columns=40)
    assert screen.text() == "green link st end"
    assert screen.unhandled == []


def test_unknown_sequences_are_counted_not_guessed() -> None:
    screen = _screen(b"a\x1b[5Wb")
    assert screen.text() == "ab"
    assert screen.unhandled == ["W"]


def test_chunks_can_split_characters_and_sequences_anywhere() -> None:
    data = "café █▓░ \x1b[1;31mred\x1b[0m\x1b]8;;https://x\x07y\x1b]8;;\x07\r\n\x1b[2Gnext".encode()
    whole = render(data, 30, 5)
    screen = TerminalScreen(30, 5)
    for byte in data:
        screen.feed(bytes([byte]))
    assert screen.text() == whole
    assert whole == "café █▓░ redy\n next"


def test_save_and_restore_the_cursor() -> None:
    assert render(b"ab\x1b7cd\x1b8X", 20, 3) == "abXd"
    assert render(b"ab\x1b[scd\x1b[uX", 20, 3) == "abXd"


def test_wide_characters_take_two_cells() -> None:
    screen = _screen("界ab".encode(), columns=10)
    assert screen.cursor_column == 4
    assert screen.text() == "界ab"


def test_insert_and_delete_characters() -> None:
    assert render(b"abcdef\x1b[3G\x1b[2P", 20, 3) == "abef"
    assert render(b"abcdef\x1b[3G\x1b[2@", 20, 3) == "ab  cdef"
    assert render(b"abcdef\x1b[3G\x1b[2X", 20, 3) == "ab  ef"


def test_insert_and_delete_lines_and_scroll_regions() -> None:
    assert render(b"1\r\n2\r\n3\x1b[2;1H\x1b[L", 5, 4) == "1\n\n2\n3"
    assert render(b"1\r\n2\r\n3\x1b[2;1H\x1b[M", 5, 4) == "1\n3"
    # Scrolling inside a region leaves the rows outside it alone and keeps nothing in the history.
    assert render(b"top\r\na\r\nb\r\nbottom\x1b[2;3r\x1b[3;1H\r\nc", 10, 4) == "top\nb\nc\nbottom"


def test_the_real_setup_token_screen_reads_back_whole() -> None:
    data = _REAL_CAPTURE.read_bytes()
    before_code = TerminalScreen(220, 50)
    before_code.feed(data[:_REAL_CAPTURE_CODE_TYPED_AT])
    shown = before_code.text()
    # On the wire these read "Paste", a cursor jump, "code", and so on: the spaces are never sent.
    assert " Paste code here if prompted >" in shown.splitlines()
    assert " This will guide you through long-lived (1-year) auth token setup for your Claude account." in shown
    assert "&state=LCa3gMLwNDhfEhxP9slqDzaZcPs0YcH2vgl7oI2eg0g" in shown
    after_code = render(data, 220, 50)
    assert (" OAuth error: That code wasn't accepted. It may be incomplete, expired, already used, "
            "or from an earlier sign-in attempt. Sign in again to get a new code.") in after_code.splitlines()
    assert "Paste code" not in after_code  # the error was drawn over the prompt
    whole = TerminalScreen(220, 50)
    whole.feed(data)
    assert whole.unhandled == []

from __future__ import annotations

from scripts.docstring_check import main, problems

SHORT = '"""One line."""\n\n\ndef f() -> None:\n    """Do a thing."""\n'
LONG = 'def f() -> None:\n    """Do a thing.\n\n    a\n    b\n    c\n    d\n    """\n'


def test_short_docstrings_pass():
    assert problems(SHORT) == []


def test_a_docstring_over_five_lines_fails():
    assert problems(LONG) == [(2, "docstring has 6 lines, max 5")]


def test_history_in_a_docstring_fails():
    source = '"""Read the file. Before, this read twice."""\n'
    assert problems(source) == [(1, "history in docstring: 'Before,'")]


def test_history_in_a_comment_fails():
    source = "x = 1  # the cache was removed\ny = 2  # see (#12)\n"
    assert problems(source) == [
        (1, "history in comment: 'was removed'"),
        (2, "history in comment: '(#12)'"),
    ]


def test_a_hex_color_is_not_an_issue_reference():
    assert problems('COLOR = "#7a7a72"  # grey, as #333 on dark\n') == []


def test_main_reports_each_problem(tmp_path, capsys):
    (tmp_path / "good.py").write_text(SHORT)
    (tmp_path / "bad.py").write_text(LONG)
    assert main([str(tmp_path)]) == 1
    assert capsys.readouterr().out == f"{tmp_path / 'bad.py'}:2: docstring has 6 lines, max 5\n"
    assert main([str(tmp_path / "good.py")]) == 0

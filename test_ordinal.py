"""Lecture files are often named <unit>-<topic> ('en-M1-2_ The AI Revolution').
The numbers order the course and must never become labels -- and must never be
read out of a title that merely looks numeric."""
import app


def test_split_ordinal_parses_lecture_filenames():
    assert app._split_ordinal("en-M1-1_ Introduction to AI4ALL Course") == (
        1, 1, "Introduction to AI4ALL Course")
    assert app._split_ordinal("en-M1-2_ The AI Revolution") == (1, 2, "The AI Revolution")
    assert app._split_ordinal("M1-2 The AI Revolution") == (1, 2, "The AI Revolution")
    assert app._split_ordinal("1-2 The AI Revolution") == (1, 2, "The AI Revolution")
    assert app._split_ordinal("Module 2-10: Neural nets") == (2, 10, "Neural nets")
    assert app._split_ordinal("Week 3.4 - Recursion") == (3, 4, "Recursion")


def test_split_ordinal_ignores_titles_that_only_look_numeric():
    # a course code and a date are the two shapes most likely to be misread
    for t in ("MATH 2110Q Multivariable Calculus", "2026-08-30", "Lecture on 1918-19 flu",
              "ENGR 2001 AI Literacy", "Chapter 1-2 review", ""):
        assert app._split_ordinal(t) == (None, None, t.strip()), t


def test_seq_is_zero_padded_so_string_sort_holds():
    # Bases sorts frontmatter as a string: "1-10" would land before "1-2"
    def seq(title):
        u, t, _ = app._split_ordinal(title)
        return f"{u:02d}-{t:02d}"

    assert sorted([seq("M1-10 ten"), seq("M1-2 two")]) == ["01-02", "01-10"]


if __name__ == "__main__":
    test_split_ordinal_parses_lecture_filenames()
    test_split_ordinal_ignores_titles_that_only_look_numeric()
    test_seq_is_zero_padded_so_string_sort_holds()
    print("ok")

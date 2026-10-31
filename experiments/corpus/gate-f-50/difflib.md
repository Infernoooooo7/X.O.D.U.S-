# difflib module

Python Library Documentation: module difflib

NAME
    difflib - Module difflib -- helpers for computing deltas between objects.

MODULE REFERENCE
    https://docs.python.org/3.14/library/difflib.html

    The following documentation is automatically generated from the Python
    source files.  It may be incomplete, incorrect or include features that
    are considered implementation detail and may vary between Python
    implementations.  When in doubt, consult the module reference at the
    location listed above.

DESCRIPTION
    Function get_close_matches(word, possibilities, n=3, cutoff=0.6):
        Use SequenceMatcher to return list of the best "good enough" matches.

    Function context_diff(a, b):
        For two lists of strings, return a delta in context diff format.

    Function ndiff(a, b):
        Return a delta: the difference between `a` and `b` (lists of strings).

    Function restore(delta, which):
        Return one of the two sequences that generated an ndiff delta.

    Function unified_diff(a, b):
        For two lists of strings, return a delta in unified diff format.

    Class SequenceMatcher:
        A flexible class for comparing pairs of sequences of any type.

    Class Differ:
        For producing human-readable deltas from sequences of lines of text.

    Class HtmlDiff:
        For producing HTML side by side comparison with change highlights.

CLASSES
    builtins.object
        Differ
        HtmlDiff
        SequenceMatcher
    builtins.tuple(builtins.object)
        Match

    class Differ(builtins.object)
     |  Differ(linejunk=None, charjunk=None)
     |
     |  Differ is a class for comparing sequences of lines of text, and
     |  producing human-readable differences or deltas.  Differ uses
     |  SequenceMatcher both to compare sequences of lines, and to compare
     |  sequences of characters within similar (near-matching) lines.
     |
     |  Each line of a Differ delta begins with a two-letter code:
     |
     |      '- '    line unique to sequence 1
     |      '+ '    line unique to sequence 2
     |      '  '    line common to both sequences
     |      '? '    line not present in either input sequence
     |
     |  Lines beginning with '? ' attempt to guide the eye to intraline
     |  differences, and were not present in either input sequence.  These lines
     |  can be confusing if the sequences contain tab characters.
     |
     |  Note that Differ makes no claim to produce a *minimal* diff.  To the
     |  contrary, minimal diffs are often counter-intuitive, because they synch
     |  up anywhere possible, sometimes accidental matches 100 pages apart.
     |  Restricting synch points to contiguous matches preserves some notion of
     |  locality, at the occasional cost of producing a longer diff.
     |
     |  Example: Comparing two texts.
     |
     |  First we set up the texts, sequences of individual single-line strings
     |  ending with newlines (such sequences can also be obtained from the
     |  `readlines()` method of file-like objects):
     |
     |  >>> text1 = '''  1. Beautiful is better than ugly.
     |  ...   2. Explicit is better than implicit.
     |  ...   3. Simple is better than complex.
     |  ...   4. Complex is better than complicated.
     |  ... '''.splitlines(keepends=True)
     |  >>> len(text1)
     |  4
     |  >>> text1[0][-1]
     |  '\n'
     |  >>> text2 = '''  1. Beautiful is better than ugly.
     |  ...   3.   Simple is better than complex.
     |  ...   4. Complicated is better than complex.
     |  ...   5. Flat is better than nested.
     |  ... '''.splitlines(keepends=True)
     |
     |  Next we instantiate a Differ object:
     |
     |  >>> d = Differ()
     |
     |  Note that when instantiating a Differ object we may pass functions to
     |  filter out line and character 'junk'.  See Differ.__init__ for details.
     |
     |  Finally, we compare the two:
     |
     |  >>> result = list(d.compare(text1, text2))
     |
     |  'result' is a list of strings, so let's pretty-print it:
     |
     |  >>> from pprint import pprint as _pprint
     |  >>> _pprint(result)
     |  ['    1. Beautiful is better than ugly.\n',
     |   '-   2. Explicit is better than implicit.\n',
     |   '-   3. Simple is better than complex.\n',
     |   '+   3.   Simple is better than complex.\n',
     |   '?     ++\n',
     |   '-   4. Complex is better than complicated.\n',
     |   '?            ^                     ---- ^\n',
     |   '+   4. Complicated is better than complex.\n',
     |   '?           ++++ ^                      ^\n',
     |   '+   5. Flat is better than nested.\n']
     |
     |  As a single multi-line string it looks like this:
     |
     |  >>> print(''.join(result), end="")
     |      1. Beautiful is better than ugly.
     |  -   2. Explicit is better than implicit.
     |  -   3. Simple is better than complex.
     |  +   3.   Simple is better than complex.
     |  ?     ++
     |  -   4. Complex is better than complicated.
     |  ?            ^                     ---- ^
     |  +   4. Complicated is better than complex.
     |  ?           ++++ ^                      ^
     |  +   5. Flat is better than nested.
     |
     |  Methods defined here:
     |
     |  __init__(self, linejunk=None, charjunk=None)
     |      Construct a text differencer, with optional filters.
     |
     |      The two optional keyword parameters are for filter functions:
     |
     |      - `linejunk`: A function that should accept a single string argument,
     |        and return true iff the string is junk. The module-level function
     |        `IS_LINE_JUNK` may be used to filter out lines without visible
     |        characters, except for at most one splat ('#').  It is recommended
     |        to leave linejunk None; the underlying SequenceMatcher class has
     |        an adaptive notion of "noise" lines that's better than any static
     |        definition the author has
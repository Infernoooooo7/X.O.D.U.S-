# fnmatch module

Python Library Documentation: module fnmatch

NAME
    fnmatch - Filename matching with shell patterns.

MODULE REFERENCE
    https://docs.python.org/3.14/library/fnmatch.html

    The following documentation is automatically generated from the Python
    source files.  It may be incomplete, incorrect or include features that
    are considered implementation detail and may vary between Python
    implementations.  When in doubt, consult the module reference at the
    location listed above.

DESCRIPTION
    fnmatch(FILENAME, PATTERN) matches according to the local convention.
    fnmatchcase(FILENAME, PATTERN) always takes case in account.

    The functions operate by translating the pattern into a regular
    expression.  They cache the compiled regular expressions for speed.

    The function translate(PATTERN) returns a regular expression
    corresponding to PATTERN.  (It does not compile it.)

FUNCTIONS
    filter(names, pat)
        Construct a list from those elements of the iterable NAMES that match PAT.

    filterfalse(names, pat)
        Construct a list from those elements of the iterable NAMES that do not match PAT.

    fnmatch(name, pat)
        Test whether FILENAME matches PATTERN.

        Patterns are Unix shell style:

        *       matches everything
        ?       matches any single character
        [seq]   matches any character in seq
        [!seq]  matches any char not in seq

        An initial period in FILENAME is not special.
        Both FILENAME and PATTERN are first case-normalized
        if the operating system requires it.
        If you don't want this, use fnmatchcase(FILENAME, PATTERN).

    fnmatchcase(name, pat)
        Test whether FILENAME matches PATTERN, including case.

        This is a version of fnmatch() which doesn't case-normalize
        its arguments.

    translate(pat)
        Translate a shell PATTERN to a regular expression.

        There is no way to quote meta-characters.

DATA
    __all__ = ['filter', 'filterfalse', 'fnmatch', 'fnmatchcase', 'transla...

FILE
    c:\python314\lib\fnmatch.py


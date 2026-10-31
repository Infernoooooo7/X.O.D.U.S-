# glob module

Python Library Documentation: module glob

NAME
    glob - Filename globbing utility.

MODULE REFERENCE
    https://docs.python.org/3.14/library/glob.html

    The following documentation is automatically generated from the Python
    source files.  It may be incomplete, incorrect or include features that
    are considered implementation detail and may vary between Python
    implementations.  When in doubt, consult the module reference at the
    location listed above.

FUNCTIONS
    escape(pathname)
        Escape all special characters.

    glob(
        pathname,
        *,
        root_dir=None,
        dir_fd=None,
        recursive=False,
        include_hidden=False
    )
        Return a list of paths matching a pathname pattern.

        The pattern may contain simple shell-style wildcards a la
        fnmatch. Unlike fnmatch, filenames starting with a
        dot are special cases that are not matched by '*' and '?'
        patterns by default.

        If `include_hidden` is true, the patterns '*', '?', '**'  will match hidden
        directories.

        If `recursive` is true, the pattern '**' will match any files and
        zero or more directories and subdirectories.

    iglob(
        pathname,
        *,
        root_dir=None,
        dir_fd=None,
        recursive=False,
        include_hidden=False
    )
        Return an iterator which yields the paths matching a pathname pattern.

        The pattern may contain simple shell-style wildcards a la
        fnmatch. However, unlike fnmatch, filenames starting with a
        dot are special cases that are not matched by '*' and '?'
        patterns.

        If recursive is true, the pattern '**' will match any files and
        zero or more directories and subdirectories.

    translate(pat, *, recursive=False, include_hidden=False, seps=None)
        Translate a pathname with shell wildcards to a regular expression.

        If `recursive` is true, the pattern segment '**' will match any number of
        path segments.

        If `include_hidden` is true, wildcards can match path segments beginning
        with a dot ('.').

        If a sequence of separator characters is given to `seps`, they will be
        used to split the pattern into segments and match path separators. If not
        given, os.path.sep and os.path.altsep (where available) are used.

DATA
    __all__ = ['glob', 'iglob', 'escape', 'translate']

FILE
    c:\python314\lib\glob.py


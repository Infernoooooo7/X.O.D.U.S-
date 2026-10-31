# subprocess module

Python Library Documentation: module subprocess

NAME
    subprocess - Subprocesses with accessible I/O streams

MODULE REFERENCE
    https://docs.python.org/3.14/library/subprocess.html

    The following documentation is automatically generated from the Python
    source files.  It may be incomplete, incorrect or include features that
    are considered implementation detail and may vary between Python
    implementations.  When in doubt, consult the module reference at the
    location listed above.

DESCRIPTION
    This module allows you to spawn processes, connect to their
    input/output/error pipes, and obtain their return codes.

    For a complete description of this module see the Python documentation.

    Main API
    ========
    run(...): Runs a command, waits for it to complete, then returns a
              CompletedProcess instance.
    Popen(...): A class for flexibly executing a command in a new process

    Constants
    ---------
    DEVNULL: Special value that indicates that os.devnull should be used
    PIPE:    Special value that indicates a pipe should be created
    STDOUT:  Special value that indicates that stderr should go to stdout


    Older API
    =========
    call(...): Runs a command, waits for it to complete, then returns
        the return code.
    check_call(...): Same as call() but raises CalledProcessError()
        if return code is not 0
    check_output(...): Same as check_call() but returns the contents of
        stdout instead of a return code
    getoutput(...): Runs a command in the shell, waits for it to complete,
        then returns the output
    getstatusoutput(...): Runs a command in the shell, waits for it to complete,
        then returns a (exitcode, output) tuple

CLASSES
    builtins.Exception(builtins.BaseException)
        SubprocessError
            CalledProcessError
            TimeoutExpired
    builtins.object
        CompletedProcess
        Popen
        STARTUPINFO

    class CalledProcessError(SubprocessError)
     |  CalledProcessError(returncode, cmd, output=None, stderr=None)
     |
     |  Raised when run() is called with check=True and the process
     |  returns a non-zero exit status.
     |
     |  Attributes:
     |    cmd, returncode, stdout, stderr, output
     |
     |  Method resolution order:
     |      CalledProcessError
     |      SubprocessError
     |      builtins.Exception
     |      builtins.BaseException
     |      builtins.object
     |
     |  Methods defined here:
     |
     |  __init__(self, returncode, cmd, output=None, stderr=None)
     |      Initialize self.  See help(type(self)) for accurate signature.
     |
     |  __str__(self)
     |      Return str(self).
     |
     |  ----------------------------------------------------------------------
     |  Data descriptors defined here:
     |
     |  stdout
     |      Alias for output attribute, to match stderr
     |
     |  ----------------------------------------------------------------------
     |  Data descriptors inherited from SubprocessError:
     |
     |  __weakref__
     |      list of weak references to the object
     |
     |  ----------------------------------------------------------------------
     |  Static methods inherited from builtins.Exception:
     |
     |  __new__(*args, **kwargs) class method of builtins.Exception
     |      Create and return a new object.  See help(type) for accurate signature.
     |
     |  ----------------------------------------------------------------------
     |  Methods inherited from builtins.BaseException:
     |
     |  __reduce__(self, /)
     |      Helper for pickle.
     |
     |  __repr__(self, /)
     |      Return repr(self).
     |
     |  __setstate__(self, state, /)
     |
     |  add_note(self, note, /)
     |      Add a note to the exception
     |
     |  with_traceback(self, tb, /)
     |      Set self.__traceback__ to tb and return self.
     |
     |  ----------------------------------------------------------------------
     |  Data descriptors inherited from builtins.BaseException:
     |
     |  __cause__
     |
     |  __context__
     |
     |  __dict__
     |
     |  __suppress_context__
     |
     |  __traceback__
     |
     |  args

    class CompletedProcess(builtins.object)
     |  CompletedProcess(args, returncode, stdout=None, stderr=None)
     |
     |  A process that has finished running.
     |
     |  This is returned by run().
     |
     |  Attributes:
     |    args: The list or str args passed to run().
     |    returncode: The exit code of the process, negative for signals.
     |    stdout: The standard output (None if not captured).
     |    stderr: The standard error (None if not captured).
     |
     |  Methods defined here:
     |
     |  __init__(self, args, returncode, stdout=None, stderr=None)
     |      Initialize self.  See help(type(self)) for accurate signature.
     |
     |  __repr__(self)
     |      Return repr(self).
     |
     |  check_returncode(self)
     |      Raise CalledProcessError if the exit code is non-zero.
     |
     |  ----------------------------------------------------------------------
     |  Class methods defined here:
     |
     |  __class_getitem__ = GenericAlias(args, /)
     |      Represent a PEP 585 generic type
     |
     |      E.g. for t = list[int], t.__origin__ is list and t.__args__ is (int,).
     |
     |  ----------------------------------------------------------------------
     |  Data descriptors defined here:
     |
     |  __dict__
     |      dictionary for instance variables
     |
     |  __weakref__
     |      list of weak references to the object

    class Popen(builtins.object)
     |  Popen(
     |      args,
     |      bufsize=-1,
     |      executable=None,
     |      stdin=None,
     |      stdout=None,
     |      stderr=None,
     |      preexec_fn=None,
     |      close_fds=True,
     |      shell=False,
     |      cwd=None,
     |      env=None,
     |   
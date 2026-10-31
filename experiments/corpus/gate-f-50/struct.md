# struct module

Python Library Documentation: module struct

NAME
    struct

MODULE REFERENCE
    https://docs.python.org/3.14/library/struct.html

    The following documentation is automatically generated from the Python
    source files.  It may be incomplete, incorrect or include features that
    are considered implementation detail and may vary between Python
    implementations.  When in doubt, consult the module reference at the
    location listed above.

DESCRIPTION
    Functions to convert between Python values and C structs.
    Python bytes objects are used to hold the data representing the C struct
    and also as format strings (explained below) to describe the layout of data
    in the C struct.

    The optional first format char indicates byte order, size and alignment:
      @: native order, size & alignment (default)
      =: native order, std. size & alignment
      <: little-endian, std. size & alignment
      >: big-endian, std. size & alignment
      !: same as >

    The remaining chars indicate types of args and must match exactly;
    these can be preceded by a decimal repeat count:
      x: pad byte (no data); c:char; b:signed byte; B:unsigned byte;
      ?: _Bool (requires C99; if not available, char is used instead)
      h:short; H:unsigned short; i:int; I:unsigned int;
      l:long; L:unsigned long; f:float; d:double; e:half-float.
    Special cases (preceding decimal count indicates length):
      s:string (array of char); p: pascal string (with count byte).
    Special cases (only available in native format):
      n:ssize_t; N:size_t;
      P:an integer type that is wide enough to hold a pointer.
    Special case (not in native mode unless 'long long' in platform C):
      q:long long; Q:unsigned long long
    Whitespace between formats is ignored.

    The variable struct.error is an exception raised on errors.

CLASSES
    builtins.Exception(builtins.BaseException)
        error
    builtins.object
        _struct.Struct

    class Struct(builtins.object)
     |  Struct(fmt) --> compiled struct object
     |
     |  Methods defined here:
     |
     |  __delattr__(self, name, /)
     |      Implement delattr(self, name).
     |
     |  __getattribute__(self, name, /)
     |      Return getattr(self, name).
     |
     |  __init__(self, /, *args, **kwargs)
     |      Initialize self.  See help(type(self)) for accurate signature.
     |
     |  __repr__(self, /)
     |      Return repr(self).
     |
     |  __setattr__(self, name, value, /)
     |      Implement setattr(self, name, value).
     |
     |  __sizeof__(self, /)
     |      S.__sizeof__() -> size of S in memory, in bytes
     |
     |  iter_unpack(self, buffer, /)
     |      Return an iterator yielding tuples.
     |
     |      Tuples are unpacked from the given bytes source, like a repeated
     |      invocation of unpack_from().
     |
     |      Requires that the bytes length be a multiple of the struct size.
     |
     |  pack(...)
     |      S.pack(v1, v2, ...) -> bytes
     |
     |      Return a bytes object containing values v1, v2, ... packed according
     |      to the format string S.format.  See help(struct) for more on format
     |      strings.
     |
     |  pack_into(...)
     |      S.pack_into(buffer, offset, v1, v2, ...)
     |
     |      Pack the values v1, v2, ... according to the format string S.format
     |      and write the packed bytes into the writable buffer buf starting at
     |      offset.  Note that the offset is a required argument.  See
     |      help(struct) for more on format strings.
     |
     |  unpack(self, buffer, /)
     |      Return a tuple containing unpacked values.
     |
     |      Unpack according to the format string Struct.format. The buffer's size
     |      in bytes must be Struct.size.
     |
     |      See help(struct) for more on format strings.
     |
     |  unpack_from(self, /, buffer, offset=0)
     |      Return a tuple containing unpacked values.
     |
     |      Values are unpacked according to the format string Struct.format.
     |
     |      The buffer's size in bytes, starting at position offset, must be
     |      at least Struct.size.
     |
     |      See help(struct) for more on format strings.
     |
     |  ----------------------------------------------------------------------
     |  Static methods defined here:
     |
     |  __new__(*args, **kwargs)
     |      Create and return a new object.  See help(type) for accurate signature.
     |
     |  ----------------------------------------------------------------------
     |  Data descriptors defined here:
     |
     |  format
     |      struct format string
     |
     |  size
     |      struct size in bytes

    class error(builtins.Exception)
     |  Method resolution order:
     |      error
     |      builtins.Exception
     |      builtins.BaseException
     |      builtins.object
     |
     |  Data descriptors defined here:
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
     |  __init__(self, /, *args, **kwargs)
     |      Initialize self.  See help(type(self)) for accurate signature.
     |
     |  __reduce__(self, /)
     |      Helper for pickle.
     |
     |  __repr__(self, /)
     |      Return repr(self).
     |
     |  __setstate__(self, state, /)
     |
     |  __str__(self, /)
     |      Return str(self).
     |
     |  add_note(self, note, /)
     |      Add a note to the exception
     |
     |  with_traceback(self, tb, /)
     |      Set self.__traceback__ to tb 
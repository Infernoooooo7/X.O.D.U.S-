# calendar module

Python Library Documentation: module calendar

NAME
    calendar - Calendar printing functions

MODULE REFERENCE
    https://docs.python.org/3.14/library/calendar.html

    The following documentation is automatically generated from the Python
    source files.  It may be incomplete, incorrect or include features that
    are considered implementation detail and may vary between Python
    implementations.  When in doubt, consult the module reference at the
    location listed above.

DESCRIPTION
    Note when comparing these calendars to the ones printed by cal(1): By
    default, these calendars have Monday as the first day of the week, and
    Sunday as the last (the European convention). Use setfirstweekday() to
    set the first day of the week (0=Monday, 6=Sunday).

CLASSES
    builtins.IndexError(builtins.LookupError)
        IllegalMonthError(builtins.ValueError, builtins.IndexError)
    builtins.ValueError(builtins.Exception)
        IllegalMonthError(builtins.ValueError, builtins.IndexError)
        IllegalWeekdayError
    builtins.object
        Calendar
            HTMLCalendar
                LocaleHTMLCalendar
            TextCalendar
                LocaleTextCalendar
    enum.IntEnum(builtins.int, enum.ReprEnum)
        Day
        Month

    class Calendar(builtins.object)
     |  Calendar(firstweekday=0)
     |
     |  Base calendar class. This class doesn't do any formatting. It simply
     |  provides data to subclasses.
     |
     |  Methods defined here:
     |
     |  __init__(self, firstweekday=0)
     |      Initialize self.  See help(type(self)) for accurate signature.
     |
     |  getfirstweekday(self)
     |
     |  itermonthdates(self, year, month)
     |      Return an iterator for one month. The iterator will yield datetime.date
     |      values and will always iterate through complete weeks, so it will yield
     |      dates outside the specified month.
     |
     |  itermonthdays(self, year, month)
     |      Like itermonthdates(), but will yield day numbers. For days outside
     |      the specified month the day number is 0.
     |
     |  itermonthdays2(self, year, month)
     |      Like itermonthdates(), but will yield (day number, weekday number)
     |      tuples. For days outside the specified month the day number is 0.
     |
     |  itermonthdays3(self, year, month)
     |      Like itermonthdates(), but will yield (year, month, day) tuples.  Can be
     |      used for dates outside of datetime.date range.
     |
     |  itermonthdays4(self, year, month)
     |      Like itermonthdates(), but will yield (year, month, day, day_of_week) tuples.
     |      Can be used for dates outside of datetime.date range.
     |
     |  iterweekdays(self)
     |      Return an iterator for one week of weekday numbers starting with the
     |      configured first one.
     |
     |  monthdatescalendar(self, year, month)
     |      Return a matrix (list of lists) representing a month's calendar.
     |      Each row represents a week; week entries are datetime.date values.
     |
     |  monthdays2calendar(self, year, month)
     |      Return a matrix representing a month's calendar.
     |      Each row represents a week; week entries are
     |      (day number, weekday number) tuples. Day numbers outside this month
     |      are zero.
     |
     |  monthdayscalendar(self, year, month)
     |      Return a matrix representing a month's calendar.
     |      Each row represents a week; days outside this month are zero.
     |
     |  setfirstweekday(self, firstweekday)
     |
     |  yeardatescalendar(self, year, width=3)
     |      Return the data for the specified year ready for formatting. The return
     |      value is a list of month rows. Each month row contains up to width months.
     |      Each month contains between 4 and 6 weeks and each week contains 1-7
     |      days. Days are datetime.date objects.
     |
     |  yeardays2calendar(self, year, width=3)
     |      Return the data for the specified year ready for formatting (similar to
     |      yeardatescalendar()). Entries in the week lists are
     |      (day number, weekday number) tuples. Day numbers outside this month are
     |      zero.
     |
     |  yeardayscalendar(self, year, width=3)
     |      Return the data for the specified year ready for formatting (similar to
     |      yeardatescalendar()). Entries in the week lists are day numbers.
     |      Day numbers outside this month are zero.
     |
     |  ----------------------------------------------------------------------
     |  Data descriptors defined here:
     |
     |  __dict__
     |      dictionary for instance variables
     |
     |  __weakref__
     |      list of weak references to the object
     |
     |  firstweekday

    class Day(enum.IntEnum)
     |  Day(*values)
     |
     |  # Constants for days
     |
     |  Method resolution order:
     |      Day
     |      enum.IntEnum
     |      builtins.int
     |      enum.ReprEnum
     |      enum.Enum
     |      builtins.object
     |
     |  Methods defined here:
     |
     |  __format__(self, format_spec, /) from builtins.int
     |      Convert to a string according to format_spec.
     |
     |  __new__(cls, value) from enum.Enum
     |      Create and return a new object.  See help(type) for accurate signature.
     |
     |  __repr__ = global_enum_repr(self) from enum
     |      use module.enum_name instead of class.enum_name
     |
     |      the module is the last module in case of a multi-module name
     |
     |  ----------------------------------------------------------------------
     |  Data and other attributes defined here:
     |
     |  FRIDAY = calendar.FRIDAY
     |
     |  MONDAY = calendar.MONDAY
     |
     |  SATURDAY = calendar.SATURDAY
     |
     |  SUNDAY = calendar.SUNDAY
     |
     |  THURSDAY = calendar.THURSDAY
     |
     |  TUESDAY = calendar.TUESDAY
     |
     |  WEDNESDAY = calendar.WEDNESDAY
   
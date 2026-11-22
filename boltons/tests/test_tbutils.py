import json
import sys
from io import StringIO

from boltons.tbutils import (TracebackInfo,
                             ExceptionInfo,
                             print_exception,
                             fix_print_exception,
                             ContextualCallpoint,
                             ContextualExceptionInfo)


def test_exception_info():
    # test ExceptionInfo and TracebackInfo and hooks, via StringIOs
    builtin_exc_hook = sys.excepthook
    fix_print_exception()
    tbi_str = ''

    def test():
        raise ValueError('yay fun')

    fake_stderr1 = StringIO()
    fake_stderr2 = StringIO()
    sys.stderr = fake_stderr1

    try:
        test()
    except:
        exc, _, exc_traceback = sys.exc_info()
        tbi = TracebackInfo.from_traceback(exc_traceback)
        exc_info = ExceptionInfo.from_exc_info(*sys.exc_info())
        exc_info2 = ExceptionInfo.from_current()
        tbi_str = str(tbi)
        print_exception(*sys.exc_info(), file=fake_stderr2)
        new_exc_hook_res = fake_stderr2.getvalue()
        builtin_exc_hook(*sys.exc_info())
        builtin_exc_hook_res = fake_stderr1.getvalue()
    finally:
        sys.stderr = sys.__stderr__

    # Single frame
    single_frame_str = tbi.frames[-1].tb_frame_str()
    assert 'in test' in single_frame_str
    assert 'yay fun' in single_frame_str

    # Traceback info
    assert len(tbi_str.splitlines()) == 5
    assert 'yay fun' in tbi_str

    # Full except hook output
    assert 'ValueError: yay fun' in new_exc_hook_res
    assert "ValueError('yay fun')" in new_exc_hook_res
    assert len(new_exc_hook_res) > len(tbi_str)

    if sys.version_info <= (3, 12):
        # output diverges with Python 3.13+, see https://github.com/mahmoud/boltons/issues/365
        # TLDR tbutils only has minimal handling for anchors (e.g., ~~~~^^)
        assert new_exc_hook_res == builtin_exc_hook_res



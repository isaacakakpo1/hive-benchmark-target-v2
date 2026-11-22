from boltons.excutils import ExceptionCauseMixin


class MyErr(ExceptionCauseMixin, Exception):
    pass



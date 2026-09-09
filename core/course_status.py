"""Unambiguous withdrawal marks printed in the grade field."""

def is_withdrawn_grade(value):
    token = "".join(str(value or "").split()).casefold()
    return token in {"退", "退選", "已退選", "停", "停修", "撤選", "w", "withdrawn"}

def fmt_int(value) -> str:
    return f'{value:,}' if value is not None else '-'


def fmt_idr(value) -> str:
    return f'IDR {value:,}' if value is not None else '-'


def fmt_pct(value) -> str:
    return f'{value:.3f}%' if value is not None else '-'


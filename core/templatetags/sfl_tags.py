from django import template

register = template.Library()


@register.filter
def dict_get(d, key):
    if not d:
        return 0
    return d.get(key, 0)


@register.filter
def money(value):
    try:
        return f"{float(value):,.2f}"
    except (TypeError, ValueError):
        return "0.00"

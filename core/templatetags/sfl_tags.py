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


@register.filter
def split(value, sep=" "):
    return str(value).split(sep)


@register.filter
def get_item(d, key):
    try:
        return d.get(key, "")
    except AttributeError:
        return ""

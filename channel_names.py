MAX_NAME_LENGTH = 40


def clean_name(value):
    return ' '.join(value.split())[:MAX_NAME_LENGTH] if isinstance(value, str) else ''


def normalize_names(values):
    if not isinstance(values, dict):
        return {}
    return {str(ch): name for ch in range(1, 9) if (name := clean_name(values.get(str(ch), '')))}


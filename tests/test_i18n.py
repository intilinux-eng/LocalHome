from localhome.web.i18n import available_languages, load_translations


def _flatten_keys(node, prefix=""):
    keys = set()
    for key, value in node.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            keys |= _flatten_keys(value, path)
        else:
            keys.add(path)
    return keys


def test_available_languages_includes_english_and_italian():
    assert {"en", "it"} <= set(available_languages())


def test_unknown_language_falls_back_to_english():
    assert load_translations("xx") == load_translations("en")


def test_every_locale_has_the_same_keys_as_english():
    en_keys = _flatten_keys(load_translations("en"))
    for language in available_languages():
        if language == "en":
            continue
        assert _flatten_keys(load_translations(language)) == en_keys, language


def test_every_locale_preserves_placeholders_from_english():
    import re

    def placeholders(value):
        return set(re.findall(r"\{(\w+)\}", value)) if isinstance(value, str) else set()

    def flatten_values(node, prefix=""):
        values = {}
        for key, value in node.items():
            path = f"{prefix}.{key}" if prefix else key
            if isinstance(value, dict):
                values.update(flatten_values(value, path))
            else:
                values[path] = value
        return values

    en_values = flatten_values(load_translations("en"))
    for language in available_languages():
        if language == "en":
            continue
        other_values = flatten_values(load_translations(language))
        for path, en_value in en_values.items():
            assert placeholders(other_value := other_values[path]) == placeholders(en_value), (language, path)
